"""HTTP + real queue handlers + deterministic AI, isolated from operator data."""

from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.auth.service import create_user, issue_session
from app.config import Settings
from app.main import create_app
from app.models import DistributionItem, Job, ScreenshotAsset, ScreenshotImport, ScreenshotPiece, WorkItem
from app.routes.screenshot_imports import FIELD_LABELS
from app.services.jobs import REQUIRED_VERIFICATION_CHECKS
from app.services.screenshot_contracts import OcrResult
from app.services.screenshot_ocr import OcrError
from app.services.screenshot_tasks import SCREENSHOT_KINDS, build_screenshot_handlers
from app.services.screenshot_storage import resolve_storage_path
from app.services.work_queue import enqueue
from app.worker import run_once

FIXTURE_TEXT = """测试案例：示例公司招聘财务实习生
招聘类型：实习；本公告仅有财务实习生一个岗位，无另附材料。
岗位职责：报表整理与凭证核对。
任职要求：本科在读，财会相关专业，应届生可投，每周到岗三天。
工作地点：上海。
报名截止：2099年12月31日。
官方投递邮箱：jobs@example.com。
官方公告链接：https://example.com/jobs
测试案例来源：https://example.com/source
"""


class FakeScreenshotAI:
    """Self-contained test fixture, independent of ignored acceptance outputs."""

    def __init__(self):
        self.ocr_calls, self.text_calls, self.ocr_error = [], [], None

    def ocr_config_snapshot(self, session):
        return {"provider": "bailian", "model": "synthetic-ocr", "row_version": 1, "enabled": True}

    def text_config_snapshot(self, session):
        return {"provider": "bailian", "model": "synthetic-text", "row_version": 1, "enabled": True}

    def is_ocr_provider_ready(self, session):
        return True

    def freeze_ocr_request(self, session):
        def call(image, mime):
            self.ocr_calls.append((len(image), mime))
            if self.ocr_error:
                raise self.ocr_error
            return OcrResult(FIXTURE_TEXT, "synthetic-ocr", "stop", 1, None)
        return call

    def freeze_text_request(self, session):
        def call(prompt):
            self.text_calls.append(prompt)
            values = {"employer_name": "示例公司", "announcement_title": "测试案例：示例公司招聘财务实习生",
                      "job_title": "财务实习生", "notice_type": "新招聘", "job_family": "财会审计", "recruitment_type": "实习",
                      "location_category": "明确上海", "location_detail": "上海", "target_audience": "本科在读，应届生",
                      "direction_tags": "财会", "deadline": "2099-12-31", "official_url": "https://example.com/jobs",
                      "application_method": "email", "application_contact": "jobs@example.com", "posting_scope": "single_role",
                      "attachment_status": "not_required", "student_fit_level": "核心适配", "distribution_recommendation": "进入学生分发审核",
                      "role_summary": "报表整理与凭证核对", "eligibility": "本科在读，财会相关专业，应届生可投，每周到岗三天"}
            # The whole single-page announcement is an exact continuous quote.
            return json.dumps({"fields": {key: {"value": value, "state": "explicit", "quotes": [FIXTURE_TEXT]}
                                           for key, value in values.items()},
                               "roles": [], "common_requirements": [], "warnings": []}, ensure_ascii=False)
        return call


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "uploads"
    app = create_app(settings=Settings(app_env="test", database_url=f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}",
                     secret_backend="file", screenshot_upload_root=str(root), screenshot_executor_enabled=False))
    ai = FakeScreenshotAI()
    app.state.ai_settings_service = ai
    with TestClient(app) as client:
        yield app, client, ai, root


def upload(client, key="e2e-http-upload"):
    image = BytesIO()
    Image.new("RGB", (400, 600), "white").save(image, "PNG")
    response = client.post("/imports/screenshots", data={"submission_key": key, "source_name": "测试案例", "source_url": "https://example.com/source"},
                           files={"images": ("测试案例.png", image.getvalue(), "image/png")}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def process(app, ai, root):
    handlers = build_screenshot_handlers(app.state.session_factory, ai, root)
    return run_once(app.state.session_factory, allowed_kinds=SCREENSHOT_KINDS, handlers=handlers)


def real_form(app, url, **changes):
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).filter_by(public_id=url.rsplit("/", 1)[-1]).one()
        job = session.get(Job, batch.job_id) if batch.job_id else None
        fields = json.loads(batch.fields_json).get("fields", {})
        form = {"expected_row_version": str(batch.row_version), "expected_job_row_version": str(job.row_version) if job else "",
                "expected_job_version": str(job.version) if job else "", "source_name": batch.source_name,
                "source_url": batch.source_url, "title": batch.title, "edited_text": batch.edited_text or batch.ocr_text or batch.supplemental_text,
                "supplemental_text": batch.supplemental_text, "notice_type": "新招聘", "material_complete": "on",
                "quality_score": "87", "confirm_action": "approve_and_generate"}
        # Include the blank inputs too: this is the complete ordinary HTML form.
        form.update({"field_" + name: fields.get(name, {}).get("value", "") for name in FIELD_LABELS})
        form.update({"field_evidence_" + name: "" for name in FIELD_LABELS})
        form.update({key: "on" for key in REQUIRED_VERIFICATION_CHECKS})
        form.update(changes)
        return form


def official_copy_url(app, url):
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).filter_by(public_id=url.rsplit("/", 1)[-1]).one()
        job = session.get(Job, batch.job_id)
        item = session.query(DistributionItem).filter_by(job_id=job.id, channel="公众号").one()
        return f"/distribution/{item.id}/copy-payload?expected_job_version={job.version}&expected_item_row_version={item.row_version}"


def test_upload_worker_review_confirm_copy_then_edit_invalidates_old_copy(web):
    app, client, ai, root = web
    url = upload(client)
    assert process(app, ai, root)  # OCR
    assert process(app, ai, root)  # Text extraction
    assert not process(app, ai, root)
    assert len(ai.ocr_calls) == len(ai.text_calls) == 1
    page = client.get(url)
    assert page.status_code == 200 and "示例公司" in page.text and "每周到岗三天" in page.text
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).one()
        assert batch.status == "ready" and batch.job_id is None
        piece = session.query(ScreenshotPiece).one()
        assert piece.text == FIXTURE_TEXT and piece.status == "succeeded"
        original_text = batch.ocr_text
        baseline_jobs = session.query(Job).count()
    # A normal full browser form, including material/checks but a genuine 0 score.
    low_score = client.post(url + "/confirm", data=real_form(app, url, quality_score="0"))
    assert low_score.status_code == 409 and "70" in low_score.text
    with app.state.session_factory() as session:
        assert session.query(Job).count() == baseline_jobs
        assert session.query(ScreenshotImport).one().job_id is None
    saved = client.post(url + "/save", data=real_form(app, url))
    assert saved.status_code == 200, saved.text
    approved = client.post(url + "/confirm", data=real_form(app, url))
    assert approved.status_code == 200 and "审核通过" in approved.text, approved.text
    copy_url = official_copy_url(app, url)
    copied = client.get(copy_url)
    assert copied.status_code == 200, copied.text
    payload = copied.json()
    assert copied.headers["cache-control"] == "no-store"
    for key in ("html", "plain_text", "group_message"):
        assert "示例公司" in payload[key] and "财务实习生" in payload[key]
        assert "每周到岗三天" in payload[key] and "jobs@example.com" in payload[key]
        assert "2099" in payload[key] and "https://example.com/jobs" in payload[key]
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).one()
        job = session.get(Job, batch.job_id)
        assert job.quality_score == 87 and job.status == "可发布"
        assert job.confirmed_content_version == job.version
        assert session.query(DistributionItem).filter_by(job_id=job.id).count() == 2
        assert batch.ocr_text == original_text
    # Replay stale confirmation and upload key must not produce another Job/batch.
    stale = client.post(url + "/confirm", data=real_form(app, url, expected_row_version="1"))
    assert stale.status_code == 409
    assert upload(client) == url
    with app.state.session_factory() as session:
        assert session.query(ScreenshotImport).count() == 1
        assert session.query(Job).count() == baseline_jobs + 1
    changed = client.post(url + "/save", data=real_form(app, url, edited_text=FIXTURE_TEXT + "\n测试案例补充：需要成绩单。"))
    assert changed.status_code == 200, changed.text
    assert client.get(copy_url).status_code == 409


def test_queue_full_upload_keeps_original_asset_and_uploaded_record(tmp_path):
    root = tmp_path / "uploads"
    app = create_app(settings=Settings(app_env="test", database_url=f"sqlite:///{(tmp_path / 'capacity.db').as_posix()}",
                     secret_backend="file", screenshot_upload_root=str(root), task_queue_limit=1, screenshot_executor_enabled=False))
    app.state.ai_settings_service = FakeScreenshotAI()
    with app.state.session_factory() as session:
        enqueue(session, kind="source_trial", target_type="source", target_id=1, expected_row_version=1, expected_fact_version=1,
                config_revision="", template_version="", dedupe_key="capacity-blocker", requested_by=None)
        session.commit()
    with TestClient(app) as client:
        url = upload(client, "queue-full")
        assert client.get(url).status_code == 200
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).one()
        assert batch.status == "uploaded" and batch.error_code == "queue_full"
        asset = session.query(ScreenshotAsset).one()
        assert resolve_storage_path(root, asset.storage_key).is_file()
        assert session.query(ScreenshotPiece).count() == 1
        assert session.query(WorkItem).filter_by(kind="screenshot_ocr_piece").count() == 0


def test_manual_complete_text_bypasses_permanent_ocr_failure_and_generates(web):
    app, client, ai, root = web
    ai.ocr_error = OcrError("provider_auth")
    url = upload(client, "manual-recovery")
    assert process(app, ai, root)
    assert not process(app, ai, root)
    with app.state.session_factory() as session:
        assert session.query(WorkItem).one().status == "needs_review"
        assert session.query(ScreenshotImport).one().status == "needs_attention"
    # A user should not need an extra save click before recovering OCR with text.
    retry = client.post(url + "/retry", data=real_form(app, url, edited_text=FIXTURE_TEXT), follow_redirects=False)
    assert retry.status_code == 303, retry.text
    with app.state.session_factory() as session:
        record = session.query(ScreenshotImport).one()
        assert record.edited_text == FIXTURE_TEXT
        assert record.material_complete is True
    assert process(app, ai, root)
    assert len(ai.ocr_calls) == 1, "complete human text must not repeat permanent OCR failure"
    assert len(ai.text_calls) == 1
    approved = client.post(url + "/confirm", data=real_form(app, url))
    assert approved.status_code == 200 and "审核通过" in approved.text, approved.text
    assert client.get(official_copy_url(app, url)).status_code == 200


def test_cloud_member_direct_copy_payload_is_forbidden(tmp_path):
    app = create_app("sqlite+pysqlite:///:memory:", settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test",
                     secret_backend="file", screenshot_upload_root=str(tmp_path / "uploads")))
    with app.state.session_factory() as session:
        member = create_user(session, "e2e-member", "A secure password", "测试普通成员", must_change_password=False)
        token, _ = issue_session(session, member)
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set("recruiting_session", token)
        response = client.get("/distribution/999/copy-payload?expected_job_version=1&expected_item_row_version=1")
    assert response.status_code == 403


def test_retry_queue_full_preserves_current_form_edits(web):
    app, client, ai, root = web
    url = upload(client, "retry-capacity")
    with app.state.session_factory() as session:
        # The upload already queued one OCR unit. Fill the remaining capacity.
        for index in range(app.state.settings.task_queue_limit - 1):
            enqueue(session, kind="source_trial", target_type="source", target_id=index + 1,
                    expected_row_version=1, expected_fact_version=1, config_revision="", template_version="",
                    dedupe_key=f"retry-capacity-{index}", requested_by=None)
        session.commit()
        before_version = session.query(ScreenshotImport).one().row_version
    revised = FIXTURE_TEXT + "\n财务实习生补充：须提交成绩单。"
    response = client.post(url + "/retry", data=real_form(app, url, edited_text=revised,
                           title="测试案例：排队满时保存编辑", source_name="测试人工补充来源"), follow_redirects=False)
    assert response.status_code == 409 and "草稿已保存" in response.text, response.text
    assert "后台任务已满" in response.text
    with app.state.session_factory() as session:
        batch = session.query(ScreenshotImport).one()
        assert batch.edited_text == revised and batch.material_complete is True
        assert batch.title == "测试案例：排队满时保存编辑" and batch.source_name == "测试人工补充来源"
        assert batch.row_version > before_version and batch.job_id is None
        assert session.query(WorkItem).count() == app.state.settings.task_queue_limit
        assert session.query(WorkItem).filter_by(kind="screenshot_extract_fields").count() == 0
    refreshed = client.get(url)
    assert "须提交成绩单" in refreshed.text and "排队满时保存编辑" in refreshed.text
    assert ai.ocr_calls == ai.text_calls == []


def test_derive_preserves_current_form_edits_and_original_d_record(web):
    from test_screenshot_workflow import create_job, make_import

    app, client, ai, root = web
    first = "财务实习生：本科在校生，会计专业。"
    second = "高级研究员：硕士，三年以上工作经验。"
    text = f"示例公司招聘。\n{first}\n{second}\n工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    def field(value, quote):
        return {"value": value, "state": "explicit", "quotes": [quote]}
    roles = [{"job_title": field("财务实习生", first), "eligibility": field("本科在校生，会计专业", first)},
             {"job_title": field("高级研究员", second), "eligibility": field("硕士，三年以上工作经验", second)}]
    with app.state.session_factory() as session:
        batch = make_import(session, text=text, roles=roles, fields={"employer_name": "示例公司", "posting_scope": "multi_role_announcement"})
        job = create_job(session, batch)
        assert job.intake_grade == "D"
        original_id, original_job_id = batch.id, job.id
        original_selection = batch.selected_roles_json
        baseline_jobs = session.query(Job).count()
        url = "/imports/screenshots/" + batch.public_id
    correction = "财务实习生补充条件：本科在校生，每周到岗三天。"
    form = real_form(app, url, title="测试案例：选择学生岗位并保留当前更正", supplemental_text=correction,
                     selected_roles="0", role_0_eligibility="本科在校生，每周到岗三天", role_evidence_0_eligibility=correction)
    response = client.post(url + "/derive", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] != url
    with app.state.session_factory() as session:
        original = session.get(ScreenshotImport, original_id)
        child = session.query(ScreenshotImport).filter_by(parent_import_id=original_id).one()
        old_job = session.get(Job, original_job_id)
        assert original.job_id == original_job_id and old_job.intake_grade == "D" and old_job.status == "待核验"
        assert original.selected_roles_json == original_selection
        assert child.job_id is None and json.loads(child.selected_roles_json) == [0]
        assert child.title == "测试案例：选择学生岗位并保留当前更正"
        assert child.supplemental_text == correction
        assert json.loads(child.fields_json)["roles"][0]["eligibility"]["value"] == "本科在校生，每周到岗三天"
        assert session.query(Job).count() == baseline_jobs
    assert client.get(response.headers["location"]).status_code == 200
