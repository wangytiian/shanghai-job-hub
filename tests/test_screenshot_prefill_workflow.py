"""Recovery review safety; all persistence uses pytest's isolated databases."""

from dataclasses import replace
import json

import pytest
from bs4 import BeautifulSoup

from app.models import DistributionItem, Job, ScreenshotImport
from app.services.distribution import distribution_blockers
from app.services.screenshot_contracts import EvidenceRef, ExtractedField
from app.services.screenshot_extraction import facts_to_dict
from app.services.screenshot_prefill import has_common_recovery_blockers, recover_extraction_chunk
from test_screenshot_confirmation import confirm
from test_screenshot_frontend import context, render
from test_screenshot_workflow import call_save, make_import, valid_data, workflow


def replace_facts(session, record, **changes):
    facts = replace(workflow().read_import_facts(record), **changes)
    record.fields_json = json.dumps(facts_to_dict(facts), ensure_ascii=False)
    session.commit()
    return facts


def unclear_salary(session, record):
    fields = dict(workflow().read_import_facts(record).fields)
    fields["salary"] = ExtractedField("", "unclear", ())
    return replace_facts(session, record, fields=fields)


def test_reextract_invalidates_approved_job_and_existing_copy_without_field_change(session):
    record = make_import(session)
    result = confirm(session, record)
    job = session.get(Job, result.job_id)
    item = session.query(DistributionItem).filter_by(job_id=job.id).first()
    assert not distribution_blockers(job, item)
    previous_revision, previous_version = record.content_revision, job.version
    call_save(session, record, {"reextract": True})
    assert record.content_revision == previous_revision + 1
    assert job.version == previous_version + 1
    assert job.status == "待核验"
    assert job.verification_version == job.confirmed_content_version == 0
    assert item.status == "已失效" and distribution_blockers(job, item)


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_reextract_requires_real_bool(session, value):
    record = make_import(session)
    with pytest.raises(ValueError, match="布尔"):
        call_save(session, record, {"reextract": value})


def test_human_can_explicitly_mark_empty_unclear_field_as_absent_with_ledger(session):
    record = make_import(session)
    unclear_salary(session, record)
    call_save(session, record, {"fields": {"salary": ""}, "resolved_missing_fields": ["salary"]})
    assert workflow().read_import_facts(record).fields["salary"] == ExtractedField("", "missing", ())
    ledger = json.loads(record.evidence_json)
    assert any(item.get("kind") == "human_confirmed_missing" and item.get("field") == "salary" and item.get("content_revision") == record.content_revision for item in ledger)


@pytest.mark.parametrize("names", [["employer_name"], ["unknown"], [1], "salary", ["salary", "salary"]])
def test_missing_resolution_rejects_nonempty_unknown_or_invalid_names(session, names):
    record = make_import(session)
    unclear_salary(session, record)
    with pytest.raises(ValueError):
        call_save(session, record, {"resolved_missing_fields": names})


def test_unchanged_blank_form_does_not_resolve_unclear_even_with_blank_evidence(session):
    record = make_import(session)
    unclear_salary(session, record)
    call_save(session, record, {"fields": {"salary": ""}, "field_evidence": {"salary": ""}})
    assert workflow().read_import_facts(record).fields["salary"].state == "unclear"
    with pytest.raises(ValueError, match="不确定"):
        confirm(session, record)


def test_evidence_text_alone_cannot_clear_empty_unclear_field_without_checkbox(session):
    record = make_import(session)
    unclear_salary(session, record)
    call_save(session, record, {"fields": {"salary": ""}, "field_evidence": {"salary": "岗位职责：报表整理。"}})
    assert workflow().read_import_facts(record).fields["salary"].state == "unclear"


def test_clearing_a_nonempty_unclear_value_still_requires_explicit_absence_resolution(session):
    record = make_import(session)
    fields = dict(workflow().read_import_facts(record).fields)
    fields["salary"] = ExtractedField("数字待核对", "unclear", ())
    replace_facts(session, record, fields=fields)
    call_save(session, record, {"fields": {"salary": ""}})
    assert workflow().read_import_facts(record).fields["salary"].state == "unclear"


@pytest.mark.parametrize("name", ["employer_name", "job_title", "official_url"])
def test_absence_confirmation_never_bypasses_required_approval_fields(session, name):
    record = make_import(session)
    fields = dict(workflow().read_import_facts(record).fields)
    fields[name] = ExtractedField("", "unclear", ())
    replace_facts(session, record, fields=fields)
    call_save(session, record, {"resolved_missing_fields": [name]})
    with pytest.raises(ValueError):
        confirm(session, record)
    assert session.query(DistributionItem).count() == 0


def test_common_warning_without_placeholder_blocks_confirmation_and_unrelated_edits(session):
    record = make_import(session)
    replace_facts(session, record, warnings=("AI公共条件待核对：公共条件结构无效",), common_requirements=())
    call_save(session, record, {"title": "人工工作区标题"})
    assert has_common_recovery_blockers(workflow().read_import_facts(record))
    with pytest.raises(ValueError, match="公共条件"):
        confirm(session, record)


def test_unchanged_empty_common_form_keeps_blocker_and_can_save_other_edits(session):
    record = make_import(session)
    replace_facts(session, record, warnings=("AI公共条件待核对：第1条",), common_requirements=(ExtractedField("", "unclear", ()),))
    call_save(session, record, {"title": "保留待核对草稿", "common_fields": {"0": ""}})
    assert has_common_recovery_blockers(workflow().read_import_facts(record))
    assert record.title == "保留待核对草稿"


def test_human_current_evidence_correction_resolves_common_warning_but_retains_scope_warning(session):
    record = make_import(session)
    replace_facts(session, record, warnings=("AI公共条件待核对：第1条", "岗位边界不明确，请人工定位"), common_requirements=(ExtractedField("", "unclear", ()),))
    quote = "所有岗位统一官方报名，不收取报名费用。"
    call_save(session, record, {"supplemental_text": quote, "common_fields": {"0": "统一官方报名，不收取报名费用"}, "common_evidence": {"0": quote}})
    facts = workflow().read_import_facts(record)
    assert not has_common_recovery_blockers(facts)
    assert facts.warnings == ("岗位边界不明确，请人工定位",)
    assert facts.common_requirements[0].state == "explicit"
    assert any(item.get("kind") == "human_common_correction" for item in json.loads(record.evidence_json))


def test_partial_common_correction_does_not_clear_other_unresolved_condition(session):
    record = make_import(session)
    replace_facts(session, record, warnings=("AI公共条件待核对：第1条",), common_requirements=(ExtractedField("", "unclear", ()), ExtractedField("", "unclear", ())))
    quote = "所有岗位统一官方报名。"
    call_save(session, record, {"supplemental_text": quote, "common_fields": {"0": quote}, "common_evidence": {"0": quote}})
    assert has_common_recovery_blockers(workflow().read_import_facts(record))


def test_common_correction_cannot_clear_warning_while_another_old_quote_is_stale(session):
    record = make_import(session)
    stale = EvidenceRef(None, None, 0, 8, "旧公共限制无原文", "manual")
    replace_facts(session, record, warnings=("AI公共条件待核对：需核实",), common_requirements=(ExtractedField("", "unclear", ()), ExtractedField("旧公共限制", "explicit", (stale,))))
    quote = "所有岗位统一官方报名。"
    call_save(session, record, {"supplemental_text": quote, "common_fields": {"0": quote}, "common_evidence": {"0": quote}})
    assert has_common_recovery_blockers(workflow().read_import_facts(record))


def test_recovered_invalid_role_warning_requires_scope_confirmation(session):
    record = make_import(session)
    recovered = recover_extraction_chunk(json.dumps({"roles": [{"job_title": {"value": "伪造", "state": "explicit", "quotes": ["无此原文"]}}]}), record.ocr_text)
    replace_facts(session, record, warnings=recovered.warnings)
    with pytest.raises(ValueError, match="边界|范围"):
        confirm(session, record)


def test_template_prefill_summary_missing_checkbox_and_ocr_retry_label():
    data = context()
    from app.services.screenshot_contracts import ScreenshotFacts
    data["facts"] = ScreenshotFacts({"salary": ExtractedField("", "unclear", ()), "employer_name": ExtractedField("示例公司", "explicit", ()), "confidence": ExtractedField("", "unclear", ())}, (), (), 2)
    data["field_labels"] = {"salary": "薪资", "employer_name": "招聘单位"}
    data["prefill_summary"] = {"filled": 1, "unresolved": 1, "role_unresolved": 0, "common_unresolved": 0}
    data["current_models"] = {"ocr": "当前图片模型", "text": "当前文本模型"}
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    assert "AI自动预填" in soup.get_text() and "待核对项目" in soup.get_text()
    assert "当前图片模型" in soup.get_text() and "当前文本模型" in soup.get_text()
    assert "当前配置" in soup.get_text()
    assert "AI重新提取字段" in soup.get_text()
    checkbox = soup.select_one('input[name="resolved_missing_fields"][value="salary"]')
    assert checkbox and not checkbox.has_attr("checked")
    assert soup.select_one('input[name="resolved_missing_fields"][value="confidence"]')
    assert not soup.select_one('input[name="resolved_missing_fields"][value="employer_name"]')
    assert "已核对原文未提供该项" in soup.get_text()


def test_route_current_model_names_summary_and_missing_resolution(tmp_path):
    from fastapi.testclient import TestClient
    from test_screenshot_web import screenshot_app
    app = screenshot_app(tmp_path)
    class SafeSnapshot:
        def text_config_snapshot(self, session):
            return {"model": "text-current", "base_url": "https://secret-host.invalid", "key": "secret-key"}
        def ocr_config_snapshot(self, session):
            return {"model": "ocr-current", "api_key": "secret-key"}
    app.state.ai_settings_service = SafeSnapshot()
    with app.state.session_factory() as session:
        record = make_import(session)
        unclear_salary(session, record)
        facts = replace_facts(session, record, warnings=("AI公共条件待核对：公共条件结构无效",))
        filled = sum(bool(field.value) for field in facts.fields.values())
        url, version = "/imports/screenshots/" + record.public_id, record.row_version
    with TestClient(app) as client:
        page = client.get(url)
        assert page.status_code == 200
        assert "text-current" in page.text and "ocr-current" in page.text
        assert "secret-key" not in page.text and "secret-host" not in page.text
        assert page.context["prefill_summary"] == {"filled": filled, "unresolved": 1, "role_unresolved": 0, "common_unresolved": 1}
        # The real route now uses guided review: factual notice type stays
        # editable, while AI diagnostics are readonly references, not inputs.
        soup = BeautifulSoup(page.text, "html.parser")
        assert soup.select_one('details[data-prefill-extra] [name="field_notice_type"]')
        diagnostics = soup.select_one('details[data-guided-diagnostics]')
        assert diagnostics and not diagnostics.has_attr("open")
        for name in ("evidence", "confidence", "rationale", "uncertainties"):
            assert not soup.select_one('[name="field_' + name + '"]')
        assert not diagnostics.select("[name]")
        response = client.post(url + "/save", data={"expected_row_version": str(version), "resolved_missing_fields": "salary", "field_salary": "", "material_complete": "on"})
        assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        record = session.query(ScreenshotImport).one()
        assert workflow().read_import_facts(record).fields["salary"].state == "missing"


def test_retry_route_queue_full_invalidates_approval_before_queueing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.services.screenshot_contracts import ScreenshotError
    from test_screenshot_web import screenshot_app
    app = screenshot_app(tmp_path)
    app.state.ai_settings_service = object()  # Older injected fake without snapshots.
    with app.state.session_factory() as session:
        record = make_import(session)
        result = confirm(session, record)
        job = session.get(Job, result.job_id)
        item = session.query(DistributionItem).filter_by(job_id=job.id, channel="公众号").one()
        url = "/imports/screenshots/" + record.public_id
        copy_url = f"/distribution/{item.id}/copy-payload?expected_job_version={job.version}&expected_item_row_version={item.row_version}"
        form = {"expected_row_version": record.row_version, "expected_job_row_version": job.row_version, "expected_job_version": job.version, "material_complete": "on"}
    called = []
    def queue_full(session, import_id, **kwargs):
        batch = session.get(ScreenshotImport, import_id)
        bound = session.get(Job, batch.job_id)
        called.append(bound.confirmed_content_version)
        assert bound.status == "待核验" and bound.confirmed_content_version == 0
        raise ScreenshotError("queue_full", "队列已满")
    monkeypatch.setattr("app.services.screenshot_tasks.enqueue_screenshot_import", queue_full)
    monkeypatch.setattr("app.services.screenshot_tasks.enqueue_screenshot_extraction", queue_full)
    with TestClient(app) as client:
        assert client.get(copy_url).status_code == 200
        response = client.post(url + "/retry", data=form)
        assert response.status_code == 409
        assert "草稿已保存" in response.text
        assert client.get(copy_url).status_code == 409
    assert called == [0]


def role_prefill_import(session):
    own = "财务实习生：本科，应届生可投。工作地点：上海。"
    other = "研究助理：硕士。工作地点：北京。"
    text = "星辰公司招聘\n" + own + "\n" + other + "\n报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    def model_field(value, quote):
        return {"value": value, "state": "explicit", "quotes": [quote]}
    empty = {"value": "", "state": "unclear", "quotes": []}
    record = make_import(session, text=text, roles=[
        {"job_title": model_field("财务实习生", own), "target_audience": empty, "location_detail": empty},
        {"job_title": model_field("研究助理", other)},
    ])
    return record, own, other


def test_role_audience_and_location_have_separate_value_and_evidence_controls(session):
    record, own, other = role_prefill_import(session)
    data = context()
    data["facts"] = workflow().read_import_facts(record)
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    for name in ("target_audience", "location_detail"):
        assert soup.select_one('[name="role_0_' + name + '"]')
        assert soup.select_one('[name="role_evidence_0_' + name + '"]')
    assert "适合人群" in soup.get_text() and "该岗工作地点" in soup.get_text()


@pytest.mark.parametrize("name,value", [("target_audience", "应届生"), ("location_detail", "上海")])
@pytest.mark.parametrize("cross_role", [False, True], ids=["own-evidence", "other-role-evidence"])
def test_route_role_audience_and_location_require_their_own_current_evidence(tmp_path, name, value, cross_role):
    from fastapi.testclient import TestClient
    from test_screenshot_web import screenshot_app
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record, own, other = role_prefill_import(session)
        url = "/imports/screenshots/" + record.public_id
        form = {"expected_row_version": record.row_version, "material_complete": "on",
                "role_0_" + name: value, "role_evidence_0_" + name: other if cross_role else own}
    with TestClient(app) as client:
        response = client.post(url + "/save", data=form)
        assert response.status_code == (409 if cross_role else 200), response.text
        if cross_role:
            assert "不能串用" in response.text
    with app.state.session_factory() as session:
        result = workflow().read_import_facts(session.query(ScreenshotImport).one()).roles[0][name]
        assert result.state == ("unclear" if cross_role else "explicit")
        assert result.value == ("" if cross_role else value)
        if not cross_role:
            assert result.evidence_refs[0].quote == own
