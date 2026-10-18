from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import create_database
from app.models import ScreenshotAsset, ScreenshotImport, ScreenshotPiece, WorkItem
from app.services.screenshot_contracts import OcrResult
from app.services.screenshot_ocr import OcrError
from app.services.work_queue import claim_next, enqueue, recover_expired_leases
from app.worker import run_once


def task_module():
    return importlib.import_module("app.services.screenshot_tasks")


@pytest.fixture
def task_db(tmp_path):
    factory = create_database(f"sqlite:///{(tmp_path / 'tasks.db').as_posix()}")
    root = tmp_path / "uploads"
    root.mkdir()
    return factory, root


def add_import(factory, root, *, pieces=1, text="", pixel="a"):
    with factory() as session:
        record = ScreenshotImport(submission_key=uuid4().hex, supplemental_text=text)
        session.add(record)
        session.flush()
        asset = ScreenshotAsset(import_id=record.id, position=0, original_filename="test.png", storage_key=f"{uuid4().hex}.png", pixel_sha256=pixel * 64)
        session.add(asset)
        session.flush()
        for index in range(pieces):
            key = f"{uuid4().hex}.png"
            (root / key).write_bytes(b"synthetic image")
            session.add(ScreenshotPiece(asset_id=asset.id, piece_index=index, storage_key=key, pixel_sha256=(pixel + str(index)) * 32, width=2, height=2))
        session.commit()
        return record.id


class FakeAI:
    def __init__(self):
        self.ocr_calls = []
        self.text_calls = []
        self.ocr_callback = None
        self.text_callback = None
        self.revision = 1

    def ocr_config_snapshot(self, session):
        return {"provider": "bailian", "model": "qwen-vl-ocr", "row_version": self.revision, "enabled": True}

    def text_config_snapshot(self, session):
        return {"provider": "bailian", "model": "fake-text", "row_version": self.revision, "enabled": True}

    def is_ocr_provider_ready(self, session):
        return True

    def freeze_ocr_request(self, session):
        def call(image, mime):
            self.ocr_calls.append((image, mime))
            if self.ocr_callback:
                return self.ocr_callback()
            return OcrResult("示例公司招聘财务实习生", "qwen-vl-ocr", "stop", 3, None)
        return call

    def freeze_text_request(self, session):
        def call(prompt):
            self.text_calls.append(prompt)
            return self.text_callback() if self.text_callback else '{"fields":{},"roles":[],"warnings":[]}'
        return call


def queue_import(factory, import_id, ai, **kwargs):
    with factory() as session:
        items = task_module().enqueue_screenshot_import(session, import_id, ai_service=ai, **kwargs)
        session.commit()
        return [item.id for item in items]


def handlers(factory, ai, root, **kwargs):
    return task_module().build_screenshot_handlers(factory, ai, root, **kwargs)


def run_screenshot(factory, ai, root, **kwargs):
    return run_once(factory, allowed_kinds=task_module().SCREENSHOT_KINDS, handlers=handlers(factory, ai, root, **kwargs))


def test_claim_filters_kinds_and_applies_screenshot_lease_policy(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    with factory() as session:
        other = enqueue(session, kind="source_trial", target_type="source", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="", template_version="", dedupe_key="other", requested_by=None)
        session.commit()
        other_id = other.id
    queue_import(factory, import_id, ai)
    with factory() as session:
        item = claim_next(session, allowed_kinds=task_module().SCREENSHOT_KINDS)
        assert item.kind == "screenshot_ocr_piece"
        assert 175 <= (item.lease_until - datetime.now()).total_seconds() <= 180
        assert session.get(WorkItem, other_id).status == "queued"


def test_database_rejects_duplicate_active_key_but_allows_completed_history(task_db):
    factory, root = task_db
    ai = FakeAI()
    work_id = queue_import(factory, add_import(factory, root), ai)[0]
    with factory() as session:
        item = session.get(WorkItem, work_id)
        values = {column.name: getattr(item, column.name) for column in WorkItem.__table__.columns if column.name != "id"}
        session.add(WorkItem(**values))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.get(WorkItem, work_id).status = "succeeded"
        session.commit()
        values["status"] = "queued"
        session.add(WorkItem(**values))
        session.commit()


def test_queue_is_idempotent_transactional_and_respects_capacity(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=2)
    first = queue_import(factory, import_id, ai)
    assert queue_import(factory, import_id, ai) == first
    another = add_import(factory, root, pixel="b")
    with factory() as session:
        with pytest.raises(Exception) as failure:
            task_module().enqueue_screenshot_import(session, another, ai_service=ai, queue_limit=2)
        assert failure.value.code == "queue_full"
        session.rollback()
        assert session.get(ScreenshotImport, another).status == "uploaded"
    third = add_import(factory, root, pixel="c")
    with factory() as session:
        task_module().enqueue_screenshot_import(session, third, ai_service=ai)
        session.rollback()
    with factory() as session:
        assert session.get(ScreenshotImport, third).status == "uploaded"
        assert session.query(WorkItem).count() == 2


def test_full_ocr_then_field_flow_persists_results_and_own_evidence(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=2)
    queue_import(factory, import_id, ai)
    assert run_screenshot(factory, ai, root)
    assert run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.query(WorkItem).filter_by(kind="screenshot_extract_fields", status="queued").count() == 1
    assert run_screenshot(factory, ai, root)
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        assert record.status == "ready"
        assert "示例公司" in record.ocr_text
        assert json.loads(record.fields_json)["schema_version"] == 1
        refs = json.loads(record.evidence_json)
        assert {ref["piece_id"] for ref in refs} == {piece.id for piece in session.query(ScreenshotPiece).all()}
        assert all(item.status == "succeeded" for item in session.query(WorkItem).all())


@pytest.mark.parametrize(("code", "expected"), [("provider_auth", "needs_review"), ("ocr_truncated", "needs_review"), ("provider_timeout", "queued"), ("provider_rate_limit", "queued")])
def test_classified_failures_retry_only_transient_and_end_loading(task_db, code, expected):
    factory, root = task_db
    ai = FakeAI()
    ai.ocr_callback = lambda: (_ for _ in ()).throw(OcrError(code))
    import_id = add_import(factory, root)
    work_id = queue_import(factory, import_id, ai)[0]
    run_screenshot(factory, ai, root)
    with factory() as session:
        item = session.get(WorkItem, work_id)
        assert item.status == expected
        assert item.error_code == code
        if expected == "queued":
            assert 25 <= (item.available_at - datetime.now()).total_seconds() <= 30
        assert session.get(ScreenshotImport, import_id).status in {"queued", "needs_attention"}


def test_transient_ocr_has_only_two_total_attempts_and_manual_retry_keeps_success(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=2)
    ids = queue_import(factory, import_id, ai)
    run_screenshot(factory, ai, root)
    ai.ocr_callback = lambda: (_ for _ in ()).throw(OcrError("provider_timeout"))
    run_screenshot(factory, ai, root)
    with factory() as session:
        failed = session.get(WorkItem, ids[1])
        failed.available_at = datetime.now() - timedelta(seconds=1)
        session.commit()
    run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.get(WorkItem, ids[1]).status == "needs_review"
        assert session.get(WorkItem, ids[1]).attempts == 2
        assert session.query(ScreenshotPiece).filter_by(status="succeeded").count() == 1
    ai.ocr_callback = None
    retry_ids = queue_import(factory, import_id, ai, retry_failed_only=True)
    assert len(retry_ids) == 1
    run_screenshot(factory, ai, root)
    assert len(ai.ocr_calls) == 4


def test_late_ocr_keeps_raw_result_but_never_overwrites_manual_input(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)

    def edit_while_calling():
        with factory() as session:
            record = session.get(ScreenshotImport, import_id)
            record.edited_text = "人工更正内容"
            record.fields_json = '{"human":true}'
            record.content_revision += 1
            record.row_version += 1
            session.commit()
        return OcrResult("旧识别原文", "qwen-vl-ocr", "stop", 1, None)

    ai.ocr_callback = edit_while_calling
    queue_import(factory, import_id, ai)
    run_screenshot(factory, ai, root)
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        assert record.edited_text == "人工更正内容"
        assert record.fields_json == '{"human":true}'
        assert session.query(ScreenshotPiece).one().text == "旧识别原文"
        assert session.query(WorkItem).filter_by(kind="screenshot_extract_fields").count() == 0


def test_lease_loss_discards_result_and_heartbeat_failure_cannot_commit(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    work_id = queue_import(factory, import_id, ai)[0]

    def lose_lease():
        with factory() as session:
            session.get(WorkItem, work_id).lease_token = "replacement-owner"
            session.commit()
        return OcrResult("过期worker结果", "qwen-vl-ocr", "stop", 1, None)

    ai.ocr_callback = lose_lease
    run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.query(ScreenshotPiece).one().text == ""
        assert session.get(WorkItem, work_id).status == "running"


def test_configuration_change_while_calling_blocks_result_commit(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)

    def change_config():
        ai.revision += 1
        return OcrResult("旧配置结果", "qwen-vl-ocr", "stop", 1, None)

    ai.ocr_callback = change_config
    work_id = queue_import(factory, import_id, ai)[0]
    run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.query(ScreenshotPiece).one().text == ""
        assert session.get(WorkItem, work_id).status == "needs_review"
        assert session.get(WorkItem, work_id).error_code == "stale_configuration"


def test_cross_batch_success_cache_is_copied_to_current_piece_with_new_refs(task_db):
    factory, root = task_db
    ai = FakeAI()
    first = add_import(factory, root)
    queue_import(factory, first, ai)
    while run_screenshot(factory, ai, root):
        pass
    second = add_import(factory, root)
    queue_import(factory, second, ai)
    while run_screenshot(factory, ai, root):
        pass
    assert len(ai.ocr_calls) == 1
    with factory() as session:
        pieces = session.query(ScreenshotPiece).order_by(ScreenshotPiece.id).all()
        assert pieces[0].id != pieces[1].id
        assert pieces[0].cache_key == pieces[1].cache_key
        assert pieces[0].text == pieces[1].text
        refs = json.loads(session.get(ScreenshotImport, second).evidence_json)
        assert {ref["piece_id"] for ref in refs} == {pieces[1].id}


def test_fifth_chunk_failure_preserves_four_and_retry_calls_only_missing_chunk(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, text="甲" * 24000, pieces=0)
    with factory() as session:
        session.get(ScreenshotImport, import_id).material_complete = True
        session.commit()
    queue_import(factory, import_id, ai)

    def fail_fifth():
        if len(ai.text_calls) == 5:
            raise httpx.ReadTimeout("synthetic timeout")
        return '{"fields":{},"roles":[],"warnings":[]}'

    ai.text_callback = fail_fifth
    run_screenshot(factory, ai, root)
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        assert len(json.loads(record.extraction_chunks_json)["chunks"]) == 4
        item = session.query(WorkItem).one()
        assert item.status == "queued"
        item.available_at = datetime.now() - timedelta(seconds=1)
        session.commit()
    run_screenshot(factory, ai, root)
    assert len(ai.text_calls) == 6
    with factory() as session:
        assert session.get(ScreenshotImport, import_id).status == "ready"
        assert len(json.loads(session.get(ScreenshotImport, import_id).extraction_chunks_json)["chunks"]) == 5


def test_recovery_uses_screenshot_attempt_limit_and_can_filter_other_tasks(task_db):
    factory, root = task_db
    ai = FakeAI()
    work_id = queue_import(factory, add_import(factory, root), ai)[0]
    with factory() as session:
        item = claim_next(session)
        item.attempts = 2
        item.lease_until = datetime.now() - timedelta(seconds=1)
        other = enqueue(session, kind="source_trial", target_type="source", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="", template_version="", dedupe_key="other-expired", requested_by=None)
        other.status = "running"
        other.lease_until = datetime.now() - timedelta(seconds=1)
        session.commit()
        assert recover_expired_leases(session, allowed_kinds=task_module().SCREENSHOT_KINDS) == 1
        assert session.get(WorkItem, work_id).status == "needs_review"
        assert session.get(WorkItem, other.id).status == "running"


def test_independent_heartbeat_runs_while_model_waits(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    work_id = queue_import(factory, import_id, ai)[0]
    observed = []

    def observe_heartbeat():
        with factory() as session:
            before = session.get(WorkItem, work_id).heartbeat_at
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.02)
            with factory() as session:
                after = session.get(WorkItem, work_id).heartbeat_at
            if after > before:
                observed.append(True)
                break
        return OcrResult("原文", "qwen-vl-ocr", "stop", 1, None)

    ai.ocr_callback = observe_heartbeat
    run_screenshot(factory, ai, root, heartbeat_seconds=0.02)
    assert observed == [True]


def test_local_runtime_only_consumes_screenshots_and_stops_cleanly(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    with factory() as session:
        item = enqueue(session, kind="source_trial", target_type="source", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="", template_version="", dedupe_key="untouched-source-trial", requested_by=None)
        other_id = item.id
        session.commit()
    queue_import(factory, import_id, ai)
    runtime = importlib.import_module("app.services.screenshot_runtime")
    settings = Settings(app_env="local", database_url="sqlite://", screenshot_upload_root=str(root))
    executor = runtime.start_screenshot_executor(session_factory=factory, settings=settings, ai_service=ai, poll_seconds=0.02)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with factory() as session:
            if session.get(ScreenshotImport, import_id).status == "ready":
                break
        time.sleep(0.02)
    runtime.stop_screenshot_executor(executor)
    assert not executor.thread.is_alive()
    with factory() as session:
        assert session.get(ScreenshotImport, import_id).status == "ready"
        assert session.get(WorkItem, other_id).status == "queued"
    assert runtime.start_screenshot_executor(session_factory=factory, settings=Settings(app_env="test", database_url="sqlite://"), ai_service=ai) is None


def test_duplicate_click_keeps_running_import_progress_and_batch_is_serial(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=2)
    ids = queue_import(factory, import_id, ai)
    with factory() as session:
        item = claim_next(session, allowed_kinds=task_module().SCREENSHOT_KINDS)
        assert item.id == ids[0]
        session.get(ScreenshotImport, import_id).status = "recognizing"
        session.commit()
    queue_import(factory, import_id, ai)
    with factory() as session:
        assert session.get(ScreenshotImport, import_id).status == "recognizing"
        assert claim_next(session, allowed_kinds=task_module().SCREENSHOT_KINDS) is None


def test_stale_queued_task_does_not_mark_new_revision_piece_failed(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    queue_import(factory, import_id, ai)
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        record.content_revision += 1
        record.row_version += 1
        record.edited_text = "新材料"
        session.commit()
    queue_import(factory, import_id, ai)
    run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.query(ScreenshotPiece).one().status == "queued"
        assert session.get(ScreenshotImport, import_id).edited_text == "新材料"
    assert ai.ocr_calls == []


def test_successful_raw_ocr_is_preserved_when_merged_text_exceeds_limit(task_db):
    factory, root = task_db
    ai = FakeAI()
    ai.ocr_callback = lambda: OcrResult("原文" * 13000, "qwen-vl-ocr", "stop", 1, None)
    import_id = add_import(factory, root)
    work_id = queue_import(factory, import_id, ai)[0]
    run_screenshot(factory, ai, root)
    with factory() as session:
        assert session.query(ScreenshotPiece).one().text == "原文" * 13000
        assert session.query(ScreenshotPiece).one().status == "succeeded"
        assert session.get(WorkItem, work_id).status == "succeeded"
        assert session.get(ScreenshotImport, import_id).status == "needs_attention"


def test_field_result_late_after_manual_save_keeps_checkpoint_only(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=0, text="原文")
    with factory() as session:
        session.get(ScreenshotImport, import_id).material_complete = True
        session.commit()
    queue_import(factory, import_id, ai)

    def save_while_calling():
        with factory() as session:
            record = session.get(ScreenshotImport, import_id)
            record.row_version += 1
            record.content_revision += 1
            record.edited_text = "人工新正文"
            record.fields_json = '{"manual":true}'
            session.commit()
        return '{"fields":{},"roles":[],"warnings":[]}'

    ai.text_callback = save_while_calling
    run_screenshot(factory, ai, root)
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        assert record.edited_text == "人工新正文"
        assert record.fields_json == '{"manual":true}'
        assert len(json.loads(record.extraction_chunks_json)["chunks"]) == 1
        assert session.query(WorkItem).one().error_code == "stale_target"


def test_heartbeat_exception_prevents_any_result_commit(task_db, monkeypatch):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root)
    work_id = queue_import(factory, import_id, ai)[0]
    reached = threading.Event()

    def fail_heartbeat(*args, **kwargs):
        reached.set()
        raise RuntimeError("synthetic database outage")

    monkeypatch.setattr(task_module(), "heartbeat", fail_heartbeat)

    def wait_for_failed_heartbeat():
        assert reached.wait(1)
        time.sleep(0.02)
        return OcrResult("不能提交", "qwen-vl-ocr", "stop", 1, None)

    ai.ocr_callback = wait_for_failed_heartbeat
    run_screenshot(factory, ai, root, heartbeat_seconds=0.01)
    with factory() as session:
        assert session.query(ScreenshotPiece).one().text == ""
        assert session.get(WorkItem, work_id).status == "running"


def test_parallel_enqueue_clicks_produce_same_active_units(task_db):
    factory, root = task_db
    ai = FakeAI()
    import_id = add_import(factory, root, pieces=2)
    result, failures = [], []
    gate = threading.Barrier(2)

    def enqueue_click():
        try:
            gate.wait(timeout=2)
            result.append(queue_import(factory, import_id, ai))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=enqueue_click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert not failures
    assert len(result) == 2 and result[0] == result[1]
    with factory() as session:
        assert session.query(WorkItem).count() == 2
