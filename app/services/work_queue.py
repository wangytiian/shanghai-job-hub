"""A database-backed queue shared by the web process and one worker."""

from datetime import datetime, timedelta
import json
import secrets

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ScreenshotAsset, ScreenshotImport, ScreenshotPiece, TaskRun, WorkItem
from app.services.screenshot_contracts import SCREENSHOT_LEASE_SECONDS, SCREENSHOT_MAX_ATTEMPTS


ACTIVE_STATUSES = {"queued", "running"}
SCREENSHOT_KINDS = frozenset({"screenshot_ocr_piece", "screenshot_extract_fields"})


def _sqlite_begin(session: Session, *, immediate: bool = False) -> None:
    """Make SAVEPOINTs transactional with sqlite3's legacy transaction mode."""
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def enqueue(
    session: Session,
    *,
    kind: str,
    target_type: str,
    target_id: int,
    expected_row_version: int,
    expected_fact_version: int,
    config_revision: str,
    template_version: str,
    dedupe_key: str,
    requested_by: int | None,
    batch_id: int | None = None,
) -> WorkItem:
    existing = session.scalar(
        select(WorkItem).where(WorkItem.dedupe_key == dedupe_key, WorkItem.status.in_(ACTIVE_STATUSES))
    )
    if existing is not None:
        return existing
    item = WorkItem(
        batch_id=batch_id,
        kind=kind,
        target_type=target_type,
        target_id=target_id,
        expected_row_version=expected_row_version,
        expected_fact_version=expected_fact_version,
        config_revision=config_revision,
        template_version=template_version,
        dedupe_key=dedupe_key,
        requested_by=requested_by,
    )
    _sqlite_begin(session)
    try:
        with session.begin_nested():
            session.add(item)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(WorkItem).where(WorkItem.dedupe_key == dedupe_key, WorkItem.status.in_(ACTIVE_STATUSES)))
        if existing is None:
            raise
        return existing
    return item


def claim_next(session: Session, *, lease_seconds: int = 120, allowed_kinds=None) -> WorkItem | None:
    if allowed_kinds is not None and not allowed_kinds:
        return None
    _sqlite_begin(session, immediate=True)
    now = datetime.now()
    query = select(WorkItem).where(WorkItem.status == "queued", WorkItem.available_at <= now).order_by(WorkItem.id)
    if allowed_kinds is not None:
        query = query.where(WorkItem.kind.in_(allowed_kinds))
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    item = None
    for candidate in session.scalars(query.limit(100)):
        if candidate.kind == "screenshot_ocr_piece":
            import_id = session.scalar(select(ScreenshotAsset.import_id).join(ScreenshotPiece, ScreenshotPiece.asset_id == ScreenshotAsset.id).where(ScreenshotPiece.id == candidate.target_id))
            if import_id is not None:
                lock = select(ScreenshotImport).where(ScreenshotImport.id == import_id)
                if session.get_bind().dialect.name == "postgresql":
                    lock = lock.with_for_update(skip_locked=True)
                if session.scalar(lock) is None:
                    continue
                running = session.scalar(select(WorkItem.id).join(ScreenshotPiece, WorkItem.target_id == ScreenshotPiece.id).join(ScreenshotAsset, ScreenshotPiece.asset_id == ScreenshotAsset.id).where(WorkItem.kind == "screenshot_ocr_piece", WorkItem.status == "running", ScreenshotAsset.import_id == import_id).limit(1))
                if running is not None:
                    continue
        item = candidate
        break
    if item is None:
        return None
    token = secrets.token_urlsafe(24)
    result = session.execute(
        update(WorkItem)
        .where(WorkItem.id == item.id, WorkItem.status == "queued")
        .values(
            status="running",
            attempts=WorkItem.attempts + 1,
            lease_token=token,
            heartbeat_at=now,
            lease_until=now + timedelta(seconds=SCREENSHOT_LEASE_SECONDS if item.kind in SCREENSHOT_KINDS else lease_seconds),
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return session.get(WorkItem, item.id)


def complete(session: Session, work_item_id: int, lease_token: str, result: dict[str, object]) -> bool:
    item = session.get(WorkItem, work_item_id)
    batch_id = item.batch_id if item else None
    now = datetime.now()
    updated = session.execute(
        update(WorkItem)
        .where(
            WorkItem.id == work_item_id,
            WorkItem.status == "running",
            WorkItem.lease_token == lease_token,
            WorkItem.lease_until > now,
        )
        .values(status="succeeded", result_json=json.dumps(result, ensure_ascii=False), lease_until=None)
    )
    session.commit()
    if updated.rowcount == 1 and batch_id is not None:
        refresh_batch_status(session, batch_id)
    return updated.rowcount == 1


def heartbeat(session: Session, work_item_id: int, lease_token: str, *, lease_seconds: int = 120) -> bool:
    """Renew a lease only when this worker still owns the running item."""
    now = datetime.now()
    kind = session.scalar(select(WorkItem.kind).where(WorkItem.id == work_item_id))
    if kind in SCREENSHOT_KINDS:
        lease_seconds = SCREENSHOT_LEASE_SECONDS
    updated = session.execute(
        update(WorkItem)
        .where(
            WorkItem.id == work_item_id,
            WorkItem.status == "running",
            WorkItem.lease_token == lease_token,
            WorkItem.lease_until > now,
        )
        .values(heartbeat_at=now, lease_until=now + timedelta(seconds=lease_seconds))
    )
    session.commit()
    return updated.rowcount == 1


def _retry_delay_seconds(attempts: int) -> int:
    return 30 if attempts <= 1 else 120


def retry_or_needs_review(
    session: Session,
    work_item_id: int,
    lease_token: str,
    error_code: str,
    *,
    max_attempts: int = 3,
) -> bool:
    """Retry a transient owned task with bounded backoff, then make it visible."""
    item = session.get(WorkItem, work_item_id)
    if item is None or item.status != "running" or item.lease_token != lease_token:
        return False
    if item.kind in SCREENSHOT_KINDS:
        max_attempts = SCREENSHOT_MAX_ATTEMPTS
    if item.attempts >= max_attempts:
        return needs_review(session, work_item_id, lease_token, error_code)
    now = datetime.now()
    updated = session.execute(
        update(WorkItem)
        .where(WorkItem.id == work_item_id, WorkItem.status == "running", WorkItem.lease_token == lease_token)
        .values(
            status="queued",
            available_at=now + timedelta(seconds=_retry_delay_seconds(item.attempts)),
            error_code=error_code,
            lease_token="",
            lease_until=None,
            heartbeat_at=None,
        )
    )
    session.commit()
    if updated.rowcount == 1 and item.batch_id is not None:
        refresh_batch_status(session, item.batch_id)
    return updated.rowcount == 1


def recover_expired_leases(session: Session, *, max_attempts: int = 3, allowed_kinds=None) -> int:
    """Recover work left by a terminated worker without relying on process memory."""
    _sqlite_begin(session, immediate=True)
    now = datetime.now()
    query = select(WorkItem).where(WorkItem.status == "running", WorkItem.lease_until.is_not(None), WorkItem.lease_until <= now)
    if allowed_kinds is not None:
        query = query.where(WorkItem.kind.in_(allowed_kinds))
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    expired = session.scalars(query).all()
    for item in expired:
        limit = SCREENSHOT_MAX_ATTEMPTS if item.kind in SCREENSHOT_KINDS else max_attempts
        if item.attempts >= limit:
            item.status = "needs_review"
        else:
            item.status = "queued"
            item.available_at = now + timedelta(seconds=_retry_delay_seconds(item.attempts))
        item.error_code = "lease_expired"
        item.lease_token = ""
        item.lease_until = None
        item.heartbeat_at = None
        if item.kind in SCREENSHOT_KINDS:
            if item.kind == "screenshot_ocr_piece":
                piece = session.get(ScreenshotPiece, item.target_id)
                if piece is not None:
                    piece.status = "failed" if item.status == "needs_review" else "queued"
                    piece.error_code = "lease_expired"
                    asset = session.get(ScreenshotAsset, piece.asset_id)
                    record = session.get(ScreenshotImport, asset.import_id) if asset else None
                else:
                    record = None
            else:
                record = session.get(ScreenshotImport, item.target_id)
            if record is not None and record.content_revision == item.expected_fact_version and record.row_version == item.expected_row_version:
                record.status = "needs_attention" if item.status == "needs_review" else "queued"
                record.error_code = "lease_expired"
                record.error_message = "识别任务中断，请重试未完成的部分。"
    if expired:
        session.commit()
        for batch_id in {item.batch_id for item in expired if item.batch_id is not None}:
            refresh_batch_status(session, batch_id)
    return len(expired)


def needs_review(session: Session, work_item_id: int, lease_token: str, error_code: str) -> bool:
    item = session.get(WorkItem, work_item_id)
    batch_id = item.batch_id if item else None
    updated = session.execute(
        update(WorkItem)
        .where(
            WorkItem.id == work_item_id,
            WorkItem.status == "running",
            WorkItem.lease_token == lease_token,
        )
        .values(status="needs_review", error_code=error_code, lease_until=None)
    )
    session.commit()
    if updated.rowcount == 1 and batch_id is not None:
        refresh_batch_status(session, batch_id)
    return updated.rowcount == 1


def refresh_batch_status(session: Session, batch_id: int) -> None:
    """Summarize child work without exposing model output or secrets in TaskRun."""
    task = session.get(TaskRun, batch_id)
    if task is None:
        return
    items = session.scalars(select(WorkItem).where(WorkItem.batch_id == batch_id)).all()
    counts = {status: sum(item.status == status for item in items) for status in ACTIVE_STATUSES | {"succeeded", "needs_review"}}
    active_count = counts["queued"] + counts["running"]
    if active_count:
        task.status = "处理中"
    elif counts["needs_review"] and counts["succeeded"]:
        task.status = "部分完成"
    elif counts["needs_review"]:
        task.status = "需人工处理"
    else:
        task.status = "完成"
    task.message = (
        f"子任务：排队 {counts['queued']}，处理中 {counts['running']}，已完成 {counts['succeeded']}，"
        f"需人工处理 {counts['needs_review']}。"
    )
    session.commit()
