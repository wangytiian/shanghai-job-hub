"""Leased screenshot processing with frozen calls and per-stage checkpoints."""

from datetime import datetime, timedelta
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import threading

import httpx
from sqlalchemy import func, select, text, update

from app.models import AiProviderSetting, ScreenshotAsset, ScreenshotImport, ScreenshotPiece, WorkItem
from app.services.ai_settings import CredentialNotConfiguredError, TextProviderNotReadyError
from app.services.screenshot_contracts import (
    EvidenceRef, EXTRACTION_RULE_VERSION, OCR_RULE_VERSION, PREPROCESSING_VERSION,
    SCREENSHOT_HEARTBEAT_SECONDS, SCREENSHOT_LEASE_SECONDS, SCREENSHOT_MAX_ATTEMPTS,
    SCREENSHOT_RETRY_SECONDS, ScreenshotError,
)
from app.services.screenshot_extraction import (
    CHUNK_OVERLAP, facts_from_dict, facts_to_dict,
    merge_extracted_chunks, merge_piece_texts, parse_extraction_chunk, split_text_chunks,
)
from app.services.screenshot_ocr import OcrError
from app.services.screenshot_prefill import (
    COMMON_RECOVERY_WARNING, DIAGNOSTIC_FIELDS, _response_object, build_guided_extraction_prompt, build_repair_prompt,
    merge_repair_field,
    recover_extraction_chunk, screenshot_response_schema,
)
from app.services.screenshot_storage import resolve_storage_path
from app.services.work_queue import ACTIVE_STATUSES, SCREENSHOT_KINDS, _sqlite_begin, enqueue, heartbeat


def config_fingerprint(snapshot: dict) -> str:
    return sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _pieces(session, import_id):
    return session.execute(select(ScreenshotPiece, ScreenshotAsset.position).join(ScreenshotAsset, ScreenshotPiece.asset_id == ScreenshotAsset.id).where(ScreenshotAsset.import_id == import_id).order_by(ScreenshotAsset.position, ScreenshotPiece.piece_index)).all()


def _cache_key(piece, config):
    return config_fingerprint({"pixels": piece.pixel_sha256, "width": piece.width, "height": piece.height, "config": config, "ocr": OCR_RULE_VERSION, "preprocess": PREPROCESSING_VERSION})


def _queue_lock(session):
    _sqlite_begin(session, immediate=True)
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(837246193)"))


def _enqueue_one(session, record, *, kind, target_id, config, requested_by, queue_limit):
    rule = OCR_RULE_VERSION if kind == "screenshot_ocr_piece" else EXTRACTION_RULE_VERSION
    dedupe = f"{kind}:{target_id}:{record.row_version}:{record.content_revision}:{config}:{rule}"
    existing = session.scalar(select(WorkItem).where(WorkItem.dedupe_key == dedupe, WorkItem.status.in_(ACTIVE_STATUSES)))
    if existing is not None:
        return existing
    if session.scalar(select(func.count()).select_from(WorkItem).where(WorkItem.status.in_(ACTIVE_STATUSES))) >= queue_limit:
        raise ScreenshotError("queue_full", "后台任务已满，请稍后重试；已上传材料会保留。")
    return enqueue(session, kind=kind, target_type="screenshot_piece" if kind == "screenshot_ocr_piece" else "screenshot_import", target_id=target_id, expected_row_version=record.row_version, expected_fact_version=record.content_revision, config_revision=config, template_version=rule, dedupe_key=dedupe, requested_by=requested_by)


def enqueue_screenshot_extraction(session, import_id: int, *, ai_service, requested_by=None, queue_limit=50):
    _queue_lock(session)
    record = session.get(ScreenshotImport, import_id)
    if record is None:
        raise ScreenshotError("import_not_found", "截图导入不存在，请刷新后重试。")
    snapshot = ai_service.text_config_snapshot(session)
    item = _enqueue_one(session, record, kind="screenshot_extract_fields", target_id=record.id, config=config_fingerprint(snapshot), requested_by=requested_by, queue_limit=queue_limit)
    if item.status != "running":
        record.status, record.error_code, record.error_message = "queued", "", ""
    session.flush()
    return item


def enqueue_screenshot_import(session, import_id: int, *, ai_service, requested_by=None, retry_failed_only=False, queue_limit=50):
    """Queue missing units atomically; caller commits or rolls back the whole batch."""
    _queue_lock(session)
    record = session.get(ScreenshotImport, import_id)
    if record is None:
        raise ScreenshotError("import_not_found", "截图导入不存在，请刷新后重试。")
    pieces = _pieces(session, import_id)
    config = config_fingerprint(ai_service.ocr_config_snapshot(session))
    pending = [(piece, _cache_key(piece, config)) for piece, _ in pieces if piece.status != "succeeded" or piece.cache_key != _cache_key(piece, config)]
    if pending and not ai_service.is_ocr_provider_ready(session):
        raise OcrError("ocr_not_configured")
    items = []
    for piece, cache_key in pending:
        piece.cache_key = cache_key
        item = _enqueue_one(session, record, kind="screenshot_ocr_piece", target_id=piece.id, config=config, requested_by=requested_by, queue_limit=queue_limit)
        if item.status == "queued":
            piece.status, piece.error_code, piece.error_message = "queued", "", ""
        items.append(item)
    if not pending:
        items.append(enqueue_screenshot_extraction(session, import_id, ai_service=ai_service, requested_by=requested_by, queue_limit=queue_limit))
    if not any(item.status == "running" for item in items):
        record.status, record.error_code, record.error_message = "queued", "", ""
    session.flush()
    return items


def _owned(session, work_id, token):
    return session.scalar(select(WorkItem).where(WorkItem.id == work_id, WorkItem.status == "running", WorkItem.lease_token == token, WorkItem.lease_until > datetime.now()).execution_options(populate_existing=True))


def _guard(session, work_id, token):
    """Acquire the lease row before any target writes; zero rows means rollback."""
    result = session.execute(update(WorkItem).where(WorkItem.id == work_id, WorkItem.status == "running", WorkItem.lease_token == token, WorkItem.lease_until > datetime.now()).values(heartbeat_at=datetime.now()))
    return result.rowcount == 1


def _current(record, item):
    return record is not None and record.row_version == item.expected_row_version and record.content_revision == item.expected_fact_version


def _safe_error(exc):
    if isinstance(exc, ScreenshotError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return OcrError("provider_timeout")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return OcrError("provider_auth" if status in {401, 403} else "provider_rate_limit" if status == 429 else "provider_unavailable" if status >= 500 else "invalid_response")
    if isinstance(exc, httpx.RequestError):
        return OcrError("provider_unavailable")
    if isinstance(exc, (CredentialNotConfiguredError, TextProviderNotReadyError)):
        return OcrError("ocr_not_configured")
    return ScreenshotError("invalid_response", "字段整理结果无法校验，识别原文已保留，请补充或重试。")


def _record_for_item(session, item, *, lock=False):
    if item.kind == "screenshot_extract_fields":
        return session.get(ScreenshotImport, item.target_id, populate_existing=lock, with_for_update=lock), None
    piece = session.get(ScreenshotPiece, item.target_id)
    asset = session.get(ScreenshotAsset, piece.asset_id) if piece else None
    return (session.get(ScreenshotImport, asset.import_id, populate_existing=lock, with_for_update=lock) if asset else None), piece


def _lock_configuration(session):
    # SQLite's lease guard already obtains the writer lock. PostgreSQL also
    # locks configuration rows until this result transaction has committed.
    if session.get_bind().dialect.name == "postgresql":
        session.scalars(select(AiProviderSetting).order_by(AiProviderSetting.id).with_for_update()).all()


def _asset_status(session, asset_id):
    session.flush()
    statuses = session.scalars(select(ScreenshotPiece.status).where(ScreenshotPiece.asset_id == asset_id)).all()
    asset = session.get(ScreenshotAsset, asset_id)
    if asset is not None:
        asset.status = "succeeded" if statuses and all(status == "succeeded" for status in statuses) else "failed" if "failed" in statuses else "recognizing" if "recognizing" in statuses else "queued"


def _fail(factory, work_id, token, exc):
    error = _safe_error(exc)
    with factory() as session:
        if not _guard(session, work_id, token):
            session.rollback()
            return False
        item = _owned(session, work_id, token)
        retry = error.retryable and item.attempts < SCREENSHOT_MAX_ATTEMPTS
        item.status = "queued" if retry else "needs_review"
        item.error_code = error.code
        item.lease_until, item.heartbeat_at, item.lease_token = None, None, ""
        if retry:
            item.available_at = datetime.now() + timedelta(seconds=SCREENSHOT_RETRY_SECONDS)
        record, piece = _record_for_item(session, item, lock=True)
        if piece is not None and _current(record, item):
            piece.status = "queued" if retry else "failed"
            piece.error_code, piece.error_message = error.code, error.message[:500]
            _asset_status(session, piece.asset_id)
        if _current(record, item):
            record.status = "queued" if retry else "needs_attention"
            record.error_code, record.error_message = error.code, error.message[:500]
        session.commit()
        return True


class _LeaseHeartbeat:
    def __init__(self, factory, work_id, token, interval):
        self.factory, self.work_id, self.token, self.interval = factory, work_id, token, interval
        self.stop_event, self.lost = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"screenshot-heartbeat-{work_id}")

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                with self.factory() as session:
                    owned = heartbeat(session, self.work_id, self.token, lease_seconds=SCREENSHOT_LEASE_SECONDS)
                if not owned:
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(timeout=2)


def _merge_source(session, record):
    pieces = _pieces(session, record.id)
    raw, refs = merge_piece_texts([{"id": piece.id, "asset_id": piece.asset_id, "asset_order": order, "piece_index": piece.piece_index, "ocr_text": piece.text} for piece, order in pieces if piece.status == "succeeded"])
    complete = bool(pieces) and all(piece.status == "succeeded" for piece, _ in pieces)
    if record.edited_text.strip():
        source = record.edited_text
        refs = [EvidenceRef(None, None, 0, len(source), source, "manual")]
    else:
        source = raw
    if record.supplemental_text.strip():
        if source:
            source += "\n"
        start = len(source)
        source += record.supplemental_text
        refs.append(EvidenceRef(None, None, start, len(source), record.supplemental_text, "manual"))
    if not source.strip() or len(source) > 24000:
        raise ScreenshotError("text_limit", "可编辑正文需有内容且不超过 24000 字，请补正文或拆批。")
    return raw, source, refs, complete or record.material_complete


def _succeed(item, result):
    item.status, item.error_code = "succeeded", ""
    item.result_json = json.dumps(result, ensure_ascii=False)
    item.lease_until, item.heartbeat_at = None, None


def _handle_ocr(factory, ai, root, work_id, token, beat):
    with factory() as session:
        item = _owned(session, work_id, token)
        if item is None:
            return False
        record, piece = _record_for_item(session, item)
        if piece is None or record is None:
            raise ScreenshotError("target_not_found", "截图分片不存在，请刷新后重试。")
        if not _current(record, item):
            raise ScreenshotError("stale_target", "材料已被修改，请重新整理当前版本。")
        config = config_fingerprint(ai.ocr_config_snapshot(session))
        if config != item.config_revision:
            raise ScreenshotError("stale_configuration", "模型配置已改变，请重新发起识别。")
        cache = session.scalar(select(ScreenshotPiece).where(ScreenshotPiece.cache_key == piece.cache_key, ScreenshotPiece.status == "succeeded", ScreenshotPiece.id != piece.id, ScreenshotPiece.text != "").limit(1))
        cached = None if cache is None else {"text": cache.text, "model": cache.model, "elapsed_ms": cache.elapsed_ms, "usage_json": cache.usage_json}
        key, mime, expected_revision = piece.storage_key, piece.mime_type, item.expected_fact_version
        call = None if cached is not None else ai.freeze_ocr_request(session)
        # This transaction contains only progress writes, and ends before HTTP.
        if not _guard(session, work_id, token):
            session.rollback()
            return False
        record, piece = _record_for_item(session, item, lock=True)
        if not _current(record, item):
            session.rollback()
            raise ScreenshotError("stale_target", "材料已被修改，请重新整理当前版本。")
        piece.status, record.status = "recognizing", "recognizing"
        _asset_status(session, piece.asset_id)
        session.commit()
    result = None if cached is not None else call(resolve_storage_path(Path(root), key).read_bytes(), mime)
    if beat.lost.is_set():
        return False
    with factory() as session:
        if not _guard(session, work_id, token):
            session.rollback()
            return False
        item = _owned(session, work_id, token)
        record, piece = _record_for_item(session, item, lock=True)
        _lock_configuration(session)
        if config_fingerprint(ai.ocr_config_snapshot(session)) != config:
            session.rollback()
            raise ScreenshotError("stale_configuration", "模型配置已改变，请重新发起识别。")
        piece.text = cached["text"] if cached is not None else result.text
        piece.model = cached["model"] if cached is not None else result.model
        piece.elapsed_ms = cached["elapsed_ms"] if cached is not None else result.elapsed_ms
        piece.usage_json = cached["usage_json"] if cached is not None else json.dumps(result.usage, ensure_ascii=False)
        piece.status, piece.error_code, piece.error_message = "succeeded", "", ""
        piece.recognized_revision = expected_revision
        _asset_status(session, piece.asset_id)
        _succeed(item, {"piece_id": piece.id, "cached": cached is not None, "stale_input": not _current(record, item)})
        session.flush()
        if _current(record, item):
            try:
                all_pieces = _pieces(session, record.id)
                record.ocr_text, refs = merge_piece_texts([{"id": current.id, "asset_id": current.asset_id, "asset_order": position, "piece_index": current.piece_index, "ocr_text": current.text} for current, position in all_pieces if current.status == "succeeded"])
                record.evidence_json = json.dumps([ref.to_dict() for ref in refs], ensure_ascii=False)
                if all(current.status == "succeeded" for current, _ in all_pieces):
                    enqueue_screenshot_extraction(session, record.id, ai_service=ai, requested_by=item.requested_by)
                elif any(current.status == "failed" for current, _ in all_pieces):
                    record.status = "needs_attention"
                else:
                    record.status = "recognizing"
            except (ValueError, ScreenshotError) as exc:
                # OCR is already successful. A merge/queue limit must never
                # discard paid-for raw text or turn it into a failed OCR call.
                error = exc if isinstance(exc, ScreenshotError) else ScreenshotError("text_limit", "合并原文超过 24000 字，请拆批；逐片原文已保留。")
                record.status = "needs_attention"
                record.error_code, record.error_message = error.code, error.message[:500]
        session.commit()
    return True


def _chunks_cache(raw):
    try:
        cache = json.loads(raw)
    except (ValueError, TypeError):
        cache = {}
    return cache if isinstance(cache, dict) and cache.get("schema_version") == 1 and isinstance(cache.get("chunks"), dict) else {"schema_version": 1, "chunks": {}}


_PREFILL_SCOPE_TERMS = ("岗位边界", "归属", "多个公司", "多公司", "拆批", "多单位", "跨公司", "单位冲突", "无法区分")


def _bounded_prefill_warnings(warnings):
    warnings = list(dict.fromkeys(warnings))
    if len(warnings) <= 100:
        return tuple(warnings)
    common = [warning for warning in warnings if warning.startswith(COMMON_RECOVERY_WARNING)]
    scope = [warning for warning in warnings if any(term in warning for term in _PREFILL_SCOPE_TERMS)]
    # Reserve one slot for each safety category before filling the remaining
    # slots, so many common warnings cannot crowd out the scope blocker.
    priorities = list(dict.fromkeys(common[:1] + scope[:1] + common + scope + warnings))
    return tuple(priorities[:99] + ["AI待核对项目较多，请逐项对照原文；未显示的疑点仍须人工核实"])


def _recover_prefill(content, chunk, refs, offset, revision):
    facts = recover_extraction_chunk(content, chunk, refs, text_offset=offset, content_revision=revision)
    # Recovery may generate extra diagnostics beyond a model's 100 warnings.
    # Restore raw safety warnings before applying the screenshot-specific cap.
    raw_warnings = _response_object(content).get("warnings", [])
    if not isinstance(raw_warnings, list) or len(raw_warnings) > 100:
        raw_warnings = []
    valid_warnings = [warning for warning in raw_warnings if isinstance(warning, str) and len(warning) <= 1000]
    return replace(facts, warnings=_bounded_prefill_warnings((*facts.warnings, *valid_warnings)))


def _merge_prefill_chunks(chunks, *, complete_text, content_revision, material_complete=True):
    chunks = tuple(chunks)
    # Explanations are not alternate business facts, including across chunks.
    diagnostics = {}
    for chunk in chunks:
        for name in DIAGNOSTIC_FIELDS:
            field = chunk.fields[name]
            for ref in field.evidence_refs:
                if complete_text[ref.text_start:ref.text_end] != ref.quote:
                    raise ValueError("汇总引用与当前完整原文不一致")
            if field.value and field.state == "explicit":
                diagnostics[name] = field
    business_chunks = tuple(replace(chunk, fields={name: field for name, field in chunk.fields.items()
                                                  if name not in DIAGNOSTIC_FIELDS}) for chunk in chunks)
    facts = merge_extracted_chunks(business_chunks, complete_text=complete_text,
                                   content_revision=content_revision, material_complete=material_complete)
    fields = dict(facts.fields)
    fields.update(diagnostics)
    for name, field in fields.items():
        if not field.value and any(chunk.fields[name].state == "unclear" for chunk in chunks):
            fields[name] = replace(field, state="unclear")
    roles = []
    for role in facts.roles:
        role = dict(role)
        candidates = [candidate for chunk in chunks for candidate in chunk.roles
                      if candidate["job_title"].value == role["job_title"].value]
        for name, field in role.items():
            if not field.value and any(candidate.get(name) and candidate[name].state == "unclear" for candidate in candidates):
                role[name] = replace(field, state="unclear")
        roles.append(role)
    common = tuple(replace(field, state="unclear") if not field.value and any(
        not candidate.value and candidate.state == "unclear" for chunk in chunks for candidate in chunk.common_requirements
    ) else field for field in facts.common_requirements)
    return replace(facts, fields=fields, roles=tuple(roles), common_requirements=common,
                   warnings=_bounded_prefill_warnings(facts.warnings))


def _checked_prefill(content, chunk, refs, offset, revision):
    facts = parse_extraction_chunk(content, chunk, refs, text_offset=offset, content_revision=revision)
    for field in facts.common_requirements:
        if field.value and not any(field.value in ref.quote for ref in field.evidence_refs):
            raise ValueError("公共条件须照录原文，不得改写或反转限制")
    conservative = _recover_prefill(content, chunk, refs, offset, revision)
    if facts.roles != conservative.roles:
        raise ValueError("岗位边界不明确，独立条件须保留被舍弃标题的边界后重新核对")
    return replace(facts, warnings=_bounded_prefill_warnings(facts.warnings))


def _merge_checked_repair(initial, final, content, source, revision):
    supplied = _response_object(content).get("fields", {})
    fields, resolved = dict(initial.fields), []
    for name in supplied if isinstance(supplied, dict) else ():
        if name not in fields:
            continue
        fields[name] = merge_repair_field(name, initial.fields[name], final.fields[name])
        if initial.fields[name].state == "unclear" and not initial.fields[name].value:
            if final.fields[name].state not in {"unclear", "conflict"}:
                resolved.append(name)

    roles = [dict(role) for role in initial.roles]
    for repaired in final.roles:
        title = repaired["job_title"]
        matched = next((role for role in roles if role["job_title"].value == title.value), None)
        if matched is None:
            matched = next((role for role in roles if role["job_title"].evidence_refs == title.evidence_refs), None)
        if matched is None:
            roles.append(dict(repaired))
            continue
        for name, field in repaired.items():
            matched[name] = merge_repair_field(name, matched[name], field) if name in matched else field

    # Common restrictions remain separate literal facts. Invalid or omitted
    # restrictions and role-boundary warnings retain their review markers.
    common = list(initial.common_requirements)
    for field in final.common_requirements:
        if field not in common:
            common.append(field)
    warnings = tuple(warning for warning in (*initial.warnings, *final.warnings)
                     if not any(warning.startswith(f"AI字段 {name} 待核对") for name in resolved))
    merged = replace(initial, fields=fields, roles=tuple(roles), common_requirements=tuple(common),
                     warnings=_bounded_prefill_warnings(warnings))
    return _merge_prefill_chunks((merged,), complete_text=source, content_revision=revision)


def _extract_prefill(call, chunk, source, refs, offset, revision):
    """One repair, then preserve only evidence-validated partial facts."""
    prompt = build_guided_extraction_prompt(chunk)
    first = call(prompt)
    try:
        return _checked_prefill(first, chunk, refs, offset, revision), {"calls":1,"repaired":False,"partial":False}
    except ValueError as exc:
        reason = str(exc)[:300]
    try:
        initial = _recover_prefill(first, chunk, refs, offset, revision)
    except ValueError:
        initial = None
    try:
        second = call(build_repair_prompt(chunk, first, reason))
    except Exception:
        if initial is None:
            raise
        facts = replace(initial, warnings=_bounded_prefill_warnings((*initial.warnings,"AI自动修复未完成，已保留通过验证的字段，请核对疑点或重新提取。")))
        return facts, {"calls":2,"repaired":False,"partial":True,"validation_error":reason,"repair_error":"provider_request_failed"}
    try:
        checked = _checked_prefill(second, chunk, refs, offset, revision)
    except ValueError as exc:
        repair_reason = str(exc)[:300]
    else:
        facts = checked if initial is None else _merge_checked_repair(initial, checked, second, source, revision)
        values = [*facts.fields.values(), *(field for role in facts.roles for field in role.values()), *facts.common_requirements]
        unresolved = any(field.state in {"unclear", "conflict"} for field in values) or any(
            warning.startswith("AI") and "待核对" in warning or any(term in warning for term in _PREFILL_SCOPE_TERMS)
            for warning in facts.warnings
        )
        diagnostic = {"calls":2,"repaired":not unresolved,"partial":unresolved,"validation_error":reason}
        if unresolved:
            diagnostic["repair_validation_error"] = "修复响应未明确处理此前待核对字段或边界"
        return facts, diagnostic
    try:
        final = _recover_prefill(second, chunk, refs, offset, revision)
    except ValueError:
        final = None
    if initial is None and final is None:
        raise ScreenshotError("extraction_json_invalid", "AI返回的字段结构无法解析，自动修复仍未完成；识别原文已保留，请点击AI重新提取字段。")
    if initial is not None and final is not None:
        facts = _merge_checked_repair(initial, final, second, source, revision)
    else:
        facts = final if final is not None else initial
    return facts, {"calls":2,"repaired":False,"partial":True,"validation_error":reason,"repair_validation_error":repair_reason}


def _handle_extraction(factory, ai, work_id, token, beat):
    with factory() as session:
        item = _owned(session, work_id, token)
        if item is None:
            return False
        record = session.get(ScreenshotImport, item.target_id)
        if not _current(record, item):
            raise ScreenshotError("stale_target", "材料已被修改，请重新整理当前版本。")
        config = config_fingerprint(ai.text_config_snapshot(session))
        if config != item.config_revision:
            raise ScreenshotError("stale_configuration", "文本模型配置已改变，请重新整理。")
        raw, source, refs, material_complete = _merge_source(session, record)
        if not material_complete:
            raise ScreenshotError("incomplete_material", "仍有图片未成功识别，请重试或补充正文并确认材料完整。")
        structured_freeze = getattr(ai, "freeze_screenshot_text_request", None)
        call = structured_freeze(session, screenshot_response_schema()) if callable(structured_freeze) else ai.freeze_text_request(session)
        import_id, revision = record.id, record.content_revision
        cache = _chunks_cache(record.extraction_chunks_json)
        if not _guard(session, work_id, token):
            session.rollback()
            return False
        record = session.get(ScreenshotImport, import_id, populate_existing=True, with_for_update=True)
        if not _current(record, item):
            session.rollback()
            raise ScreenshotError("stale_target", "材料已被修改，请重新整理当前版本。")
        record.status = "extracting"
        record.ocr_text = raw
        record.evidence_json = json.dumps([ref.to_dict() for ref in refs], ensure_ascii=False)
        session.commit()
    chunks, search_start = [], 0
    for chunk in split_text_chunks(source):
        offset = source.find(chunk, search_start)
        search_start = offset + max(1, len(chunk) - CHUNK_OVERLAP)
        chunk_key = config_fingerprint({"import_id": import_id, "content_revision": revision, "text": sha256(chunk.encode("utf-8")).hexdigest(), "offset": offset, "config": config, "rule": EXTRACTION_RULE_VERSION})
        entry = cache["chunks"].get(chunk_key)
        if entry is not None and not entry.get("prefill",{}).get("partial",False):
            facts = facts_from_dict(entry["facts"])
        else:
            facts, diagnostic = _extract_prefill(call, chunk, source, refs, offset, revision)
            if beat.lost.is_set():
                return False
            with factory() as session:
                if not _guard(session, work_id, token):
                    session.rollback()
                    return False
                current = session.get(ScreenshotImport, import_id, populate_existing=True, with_for_update=True)
                _lock_configuration(session)
                if config_fingerprint(ai.text_config_snapshot(session)) != config:
                    session.rollback()
                    raise ScreenshotError("stale_configuration", "文本模型配置已改变，请重新整理。")
                current_cache = _chunks_cache(current.extraction_chunks_json)
                entry = {"content_revision": revision, "text_hash": sha256(chunk.encode("utf-8")).hexdigest(), "offset": offset, "config": config, "rule": EXTRACTION_RULE_VERSION, "facts": facts_to_dict(facts),"prefill":diagnostic}
                current_cache["chunks"][chunk_key] = entry
                current.extraction_chunks_json = json.dumps(current_cache, ensure_ascii=False)
                session.commit()
                cache["chunks"][chunk_key] = entry
        chunks.append(facts)
        with factory() as session:
            item = _owned(session, work_id, token)
            if item is None or beat.lost.is_set():
                return False
            if not _current(session.get(ScreenshotImport, import_id), item):
                raise ScreenshotError("stale_target", "材料已被修改，旧分段结果已保留；请重新整理当前版本。")
    facts = _merge_prefill_chunks(chunks, complete_text=source, content_revision=revision, material_complete=material_complete)
    if beat.lost.is_set():
        return False
    with factory() as session:
        if not _guard(session, work_id, token):
            session.rollback()
            return False
        item = _owned(session, work_id, token)
        record = session.get(ScreenshotImport, import_id, populate_existing=True, with_for_update=True)
        _lock_configuration(session)
        if not _current(record, item):
            session.rollback()
            raise ScreenshotError("stale_target", "材料已被修改，请重新整理当前版本。")
        if config_fingerprint(ai.text_config_snapshot(session)) != config:
            session.rollback()
            raise ScreenshotError("stale_configuration", "文本模型配置已改变，请重新整理。")
        record.fields_json = json.dumps(facts_to_dict(facts), ensure_ascii=False)
        conflicts = [name for name, field in facts.fields.items() if field.state == "conflict"]
        conflicts.extend(f"roles.{index}.{name}" for index, role in enumerate(facts.roles) for name, field in role.items() if field.state == "conflict")
        record.conflicts_json = json.dumps(conflicts, ensure_ascii=False)
        partial = any(entry.get("prefill",{}).get("partial",False) for entry in cache["chunks"].values()
                      if entry.get("content_revision")==revision and entry.get("rule")==EXTRACTION_RULE_VERSION and entry.get("config")==config)
        unclear = any(field.state=="unclear" for field in facts.fields.values())
        record.status = "needs_attention" if conflicts or facts.warnings or unclear else "ready"
        filled = sum(bool(field.value) for field in facts.fields.values())
        record.error_code = "partial_prefill" if partial else ""
        record.error_message = f"AI已预填{filled}项，仍有字段或原文依据需要核对；有效内容已保留，可修改疑点或重新提取。" if partial else ""
        _succeed(item, {"import_id": import_id, "content_revision": revision, "chunks": len(chunks)})
        session.commit()
    return True


def build_screenshot_handlers(session_factory, ai_service, upload_root, *, heartbeat_seconds=SCREENSHOT_HEARTBEAT_SECONDS):
    def execute(kind, _session, item):
        with _LeaseHeartbeat(session_factory, item.id, item.lease_token, heartbeat_seconds) as beat:
            try:
                if kind == "screenshot_ocr_piece":
                    return _handle_ocr(session_factory, ai_service, upload_root, item.id, item.lease_token, beat)
                return _handle_extraction(session_factory, ai_service, item.id, item.lease_token, beat)
            except Exception as exc:
                if beat.lost.is_set():
                    return False
                return _fail(session_factory, item.id, item.lease_token, exc)
    return {kind: (lambda session, item, kind=kind: execute(kind, session, item)) for kind in SCREENSHOT_KINDS}
