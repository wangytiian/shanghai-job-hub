"""Guided approval keeps draft edits while approval remains atomic."""
from dataclasses import replace
import json

import pytest

from app.auth.permissions import PermissionDenied
from app.auth.service import create_user
from app.models import DistributionItem, Job, ReviewLog, ScreenshotAsset, ScreenshotPiece
from app.services.distribution import distribution_blockers, load_confirmed_snapshot
from app.services.screenshot_contracts import ExtractedField
from app.services.screenshot_extraction import facts_to_dict
from app.services.concurrency import EditConflict
from sqlalchemy.orm import Session
from test_screenshot_confirmation import confirm as legacy_confirm
from test_screenshot_workflow import make_import, valid_data, workflow


def guided(session, record, data=None, *, action="approve_and_generate", actor=None, **overrides):
    service = workflow()
    assert hasattr(service, "confirm_guided_screenshot_import"), "guided confirmation service is missing"
    job = session.get(Job, record.job_id) if record.job_id else None
    versions = dict(expected_row_version=record.row_version,
        expected_job_row_version=job.row_version if job else None,
        expected_job_version=job.version if job else None)
    versions.update(overrides)
    return service.confirm_guided_screenshot_import(session, record.id,
        {"acknowledge": True} if data is None else data, action=action, actor=actor, **versions)


def assert_blocked(exc):
    assert type(exc.value).__name__ == "ScreenshotReviewValidationError"
    assert isinstance(exc.value.issues, tuple) and exc.value.issues
    assert all(issue.code and issue.path and issue.message and issue.action for issue in exc.value.issues)


def test_single_click_generates_two_outputs_at_zero_without_faking_score(session):
    record = make_import(session)
    result = guided(session, record)
    assert result.state == "publishable" and len(result.distribution_ids) == 2
    job = session.get(Job, result.job_id)
    assert job.quality_score == 0 and job.status == "可发布"
    snapshot = load_confirmed_snapshot(job)
    assert snapshot["review_confirmation"]["job_version"] == job.version
    assert snapshot["review_confirmation"]["actor_id"] is None
    assert all(json.loads(job.verification_checks).values())
    assert distribution_blockers(job) == []


@pytest.mark.parametrize("ack", [None, False, 1, "true", []])
def test_only_explicit_boolean_acknowledgement_can_confirm(session, ack):
    record = make_import(session)
    data = {} if ack is None else {"acknowledge": ack}
    with pytest.raises(ValueError) as exc:
        guided(session, record, data)
    assert_blocked(exc)
    assert any(issue.path == "acknowledge" for issue in exc.value.issues)
    assert session.query(DistributionItem).count() == 0


def test_missing_core_errors_are_returned_together_and_draft_survives(session):
    record = make_import(session, fields={"employer_name": "", "location_detail": "", "official_url": ""})
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "保留人工输入"}})
    assert_blocked(exc)
    assert {issue.path for issue in exc.value.issues} >= {
        "fields.employer_name", "fields.location_detail", "fields.official_url"}
    session.refresh(record)
    assert record.title == "保留人工输入" and record.content_revision == 2
    assert session.query(DistributionItem).count() == 0


def test_render_failure_keeps_saved_edit_but_no_approval_or_certificate(session, monkeypatch):
    record = make_import(session)
    def fail(*args, **kwargs):
        raise RuntimeError("render failed")
    monkeypatch.setattr(workflow(), "create_distribution_items", fail)
    with pytest.raises(RuntimeError, match="render failed") as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "已保存的标题"}})
    assert exc.value.draft_saved is True
    session.refresh(record)
    assert record.title == "已保存的标题" and record.content_revision == 2
    assert record.job_id is None
    assert session.query(Job).count() == session.query(DistributionItem).count() == 0
    assert session.query(ReviewLog).filter(ReviewLog.action.in_(["通过", "结构化完成"])).count() == 0


def test_failed_reapproval_keeps_fact_invalidation_and_old_copy_is_blocked(session, monkeypatch):
    record = make_import(session)
    result = guided(session, record)
    job = session.get(Job, result.job_id)
    old_version = job.version
    def fail(*args, **kwargs):
        raise RuntimeError("render failed")
    monkeypatch.setattr(workflow(), "create_distribution_items", fail)
    with pytest.raises(RuntimeError):
        guided(session, record, {"acknowledge": True, "edits": {"title": "重新核验标题"}})
    session.refresh(job)
    assert job.version > old_version and job.status == "待核验"
    assert job.confirmed_content_version == job.verification_version == 0
    items = session.query(DistributionItem).all()
    assert len(items) == 2 and all(item.status == "已失效" for item in items)
    assert all(distribution_blockers(job, item) for item in items)


def test_guided_optional_absence_and_diagnostics_do_not_pollute_formal_snapshot(session):
    record = make_import(session, fields={"job_family": "", "recruitment_type": "", "direction_tags": "", "location_category": ""})
    facts = workflow().read_import_facts(record)
    fields = {**facts.fields, "salary": ExtractedField("", "unclear", ()),
        "rationale": ExtractedField("AI诊断冲突", "conflict", ())}
    record.fields_json = json.dumps(facts_to_dict(replace(facts, fields=fields)), ensure_ascii=False)
    session.commit()
    result = guided(session, record)
    job = session.get(Job, result.job_id)
    assert job.job_family == job.recruitment_type == job.direction_tags == job.location_category == ""
    snap = load_confirmed_snapshot(job)
    assert "rationale" not in snap["fields"] and "salary" not in snap["fields"]
    assert job.quality_score == 0


def test_blank_deadline_is_acknowledged_but_yearless_deadline_is_blocked(session):
    record = make_import(session, fields={"deadline": ""})
    result = guided(session, record)
    assert session.get(Job, result.job_id).deadline == "公告未明确统一截止时间"
    record = make_import(session, fields={"deadline": "10月31日"})
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    assert any(issue.path == "fields.deadline" for issue in exc.value.issues)


def test_fee_in_full_material_blocks_after_safe_draft_save(session):
    record = make_import(session)
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"supplemental_text": "报名必须缴纳报名费500元。"}})
    assert_blocked(exc)
    assert any(issue.code == "recruitment_fee" for issue in exc.value.issues)
    session.refresh(record)
    assert "500元" in record.supplemental_text
    assert session.query(DistributionItem).count() == 0


def test_unknown_audience_is_a_structured_failure_with_saved_draft(session):
    text = "星辰公司招聘财务专员。岗位职责：报表整理。工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text, fields={"job_title": "财务专员", "target_audience": ""})
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "未知受众草稿"}})
    assert_blocked(exc)
    session.refresh(record)
    assert record.title == "未知受众草稿" and session.query(DistributionItem).count() == 0


def test_guided_member_submits_but_cannot_approve(session):
    member = create_user(session, "guided-member", "A secure password", "普通成员")
    record = make_import(session)
    with pytest.raises(PermissionDenied):
        guided(session, record, actor=member)
    result = guided(session, record, actor=member, action="submit")
    assert result.state == "pending_review" and result.distribution_ids == ()
    job = session.get(Job, result.job_id)
    assert job.status == "待审核" and job.quality_score == 0
    assert load_confirmed_snapshot(job)["review_confirmation"]["actor_id"] == member.id


def test_successful_uploaded_pieces_are_completed_only_by_final_click(session):
    record = make_import(session, material_complete=False)
    asset = ScreenshotAsset(import_id=record.id, storage_key="guided.png", pixel_sha256="guided", status="succeeded")
    session.add(asset)
    session.flush()
    session.add(ScreenshotPiece(asset_id=asset.id, piece_index=0, storage_key="piece.png", status="succeeded", text=record.ocr_text))
    session.commit()
    with pytest.raises(ValueError):
        guided(session, record, {"acknowledge": False})
    session.refresh(record)
    assert record.material_complete is False
    assert guided(session, record).state == "publishable"
    session.refresh(record)
    assert record.material_complete is True


def test_failed_piece_requires_current_complete_manual_body(session):
    record = make_import(session, material_complete=False)
    asset = ScreenshotAsset(import_id=record.id, storage_key="failed.png", pixel_sha256="failed")
    session.add(asset)
    session.flush()
    session.add(ScreenshotPiece(asset_id=asset.id, piece_index=0, storage_key="failed-piece.png", status="failed"))
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    assert any(issue.path == "material_complete" for issue in exc.value.issues)
    result = guided(session, record, {"acknowledge": True,
        "edits": {"edited_text": record.ocr_text, "material_complete": True}})
    assert result.state == "publishable"


def test_stale_guided_submission_cannot_overwrite_saved_draft(session):
    record = make_import(session)
    old_row = record.row_version
    workflow().save_import_edits(session, record.id, {"title": "其他页面的新标题"}, expected_row_version=old_row)
    with pytest.raises(EditConflict) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "过期标题"}}, expected_row_version=old_row)
    assert getattr(exc.value, "draft_saved", False) is False
    session.refresh(record)
    assert record.title == "其他页面的新标题" and record.job_id is None


def test_raw_yearless_deadline_cannot_be_disguised_as_missing(session):
    record = make_import(session, text="星辰公司财务实习生，应届可投。工作地点：上海。报名截止：10月31日。官方投递邮箱：jobs@example.com。", fields={"deadline": ""})
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    assert any(issue.path == "fields.deadline" for issue in exc.value.issues)


@pytest.mark.parametrize("raw_deadline", ["10月31日", "2099年2月30日", "2001年12月31日"])
def test_original_yearless_deadline_survives_edited_body_omission(session, raw_deadline):
    clean = "星辰公司招聘财务实习生，应届可投。工作地点：上海。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=clean, fields={"deadline": ""})
    clean = record.ocr_text
    record.ocr_text += f"\n报名截止：{raw_deadline}。"
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"edited_text": clean}})
    assert_blocked(exc)
    assert any(issue.path == "fields.deadline" for issue in exc.value.issues)


def test_current_official_dated_correction_resolves_original_unknown_year(session):
    record = make_import(session, fields={"deadline": ""})
    record.ocr_text += "\n报名截止：10月31日。"
    session.commit()
    correction = "官方更正：最新报名截止：2099年10月31日。"
    result = guided(session, record, {"acknowledge": True, "edits": {
        "supplemental_text": correction, "fields": {"deadline": "2099年10月31日"},
        "field_evidence": {"deadline": correction}}})
    assert result.state == "publishable"
    assert session.get(Job, result.job_id).deadline == "2099年10月31日"


def test_full_future_scalar_without_year_in_its_evidence_cannot_guess_original_year(session):
    record = make_import(session, text="星辰公司招聘财务实习生，应届生可投。工作地点：上海。官方投递邮箱：jobs@example.com。", fields={"deadline": ""})
    clean = record.ocr_text
    quote = "报名截止：10月31日。"
    record.ocr_text += "\n" + quote
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {
            "edited_text": clean, "fields": {"deadline": "2099-10-31"},
            "field_evidence": {"deadline": "工作地点：上海。"}}})
    assert_blocked(exc)


def test_raw_yearless_deadline_cannot_be_disguised_as_unspecified(session):
    record = make_import(session, text="星辰公司财务实习生，应届可投。工作地点：上海。报名截止：10月31日。官方投递邮箱：jobs@example.com。", fields={"deadline": ""})
    fields = workflow().read_import_facts(record).fields
    # The visible sentinel is a human classification, but it cannot erase a date in the source.
    from app.services.jobs import UNSPECIFIED_DEADLINE
    facts = workflow().read_import_facts(record)
    fields = {**fields, "deadline": ExtractedField(UNSPECIFIED_DEADLINE, "explicit", facts.fields["employer_name"].evidence_refs)}
    record.fields_json = json.dumps(facts_to_dict(replace(facts, fields=fields)), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)


def test_valid_scope_proof_resolves_only_the_corresponding_scope_warning(session):
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    record.fields_json = json.dumps(facts_to_dict(replace(facts, warnings=("岗位边界不明确，请人工定位",))), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    result = guided(session, record, {"acknowledge": True, "scope_confirmation": {
        "single_employer": True, "role_boundaries": True, "employer_name": "星辰公司",
        "evidence_excerpt": record.ocr_text, "note": "核对当前原图岗位范围"}})
    assert result.state == "publishable"


def test_guided_audience_promotion_still_needs_current_official_proof(session):
    from test_screenshot_workflow import create_job
    record = make_import(session)
    job = create_job(session, record)
    job.intake_grade, job.intake_reason = "C", "原文受众信息不足"
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    result = guided(session, record, {"acknowledge": True, "audience_confirmation": {
        "evidence_url": "https://example.com/jobs", "evidence_excerpt": "应届生可投",
        "note": "核对完整官方材料"}})
    assert result.state == "publishable" and job.intake_grade == "B"


@pytest.mark.parametrize("grade,reason", [("C", "原文受众信息不足"), ("B", "受众待确认[TARGET_AUDIENCE_UNCLEAR]")])
def test_readiness_reports_existing_c_alongside_other_missing_facts(session, grade, reason):
    from test_screenshot_workflow import create_job
    record = make_import(session)
    job = create_job(session, record)
    job.intake_grade, job.intake_reason = grade, reason
    session.commit()
    issues = workflow().get_guided_review_issues(session, record)
    assert any(issue.code == "audience_confirmation_required" for issue in issues)
    assert job.intake_grade == grade and job.verification_version == 0
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"fields": {"location_detail": ""}}})
    assert_blocked(exc)
    assert {issue.code for issue in exc.value.issues} >= {"required", "audience_confirmation_required"}


def test_selected_role_cannot_drop_original_global_senior_restriction(session):
    from app.services.screenshot_contracts import EvidenceRef
    record = make_import(session)
    role_quote = "财务实习生。岗位职责：报表整理。任职要求：本科，应届生可投。"
    record.ocr_text += "\n所有岗位要求：三年以上工作经验。"
    facts = workflow().read_import_facts(record)
    start = record.ocr_text.index(role_quote)
    title = ExtractedField("财务实习生", "explicit", (
        EvidenceRef(None, None, start, start + len(role_quote), role_quote, "manual"),))
    record.fields_json = json.dumps(facts_to_dict(replace(facts,
        roles=({"job_title": title},), common_requirements=())), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)
    assert any(issue.code in {"global_restriction", "screening_rejected"} for issue in exc.value.issues)


def test_source_fee_cannot_be_hidden_by_replacing_edited_body(session):
    record = make_import(session)
    clean_text = record.ocr_text
    record.ocr_text += "\n报名必须缴纳报名费500元。"
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"edited_text": clean_text}})
    assert_blocked(exc)


def test_original_senior_requirement_cannot_be_hidden_by_replacing_body(session):
    record = make_import(session)
    clean_text = record.ocr_text
    record.ocr_text += "\n任职要求：三年以上工作经验。"
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"edited_text": clean_text}})
    assert_blocked(exc)


def test_readiness_exposes_screening_blocker_without_mutating_import_or_job(session):
    record = make_import(session, supplemental_text="任职要求：三年以上工作经验。")
    before = record.row_version, record.content_revision, record.material_complete
    issues, _, _, _ = workflow().guided_review_readiness(session, record)
    assert any(issue.code == "screening_rejected" for issue in issues)
    assert (record.row_version, record.content_revision, record.material_complete) == before
    assert record.job_id is None and session.query(Job).count() == 0


def test_selected_role_issue_paths_keep_original_editable_index(session):
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    title = facts.fields["job_title"]
    conflict = ExtractedField("本科或硕士", "conflict", title.evidence_refs)
    record.fields_json = json.dumps(facts_to_dict(replace(facts, roles=(
        {"job_title": title}, {"job_title": title, "eligibility": conflict}))), ensure_ascii=False)
    record.selected_roles_json = "[1]"
    session.commit()
    issues = workflow().get_guided_review_issues(session, record)
    assert any(issue.path == "roles.1.eligibility" for issue in issues)
    assert not any(issue.path == "roles.0.eligibility" for issue in issues)


def test_current_evidence_refs_must_match_selected_role_source(session):
    from app.services.screenshot_contracts import EvidenceRef
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    forged = ExtractedField("财务实习生", "explicit", (EvidenceRef(None, None, 0, 5, "无此原文！", "manual"),))
    record.fields_json = json.dumps(facts_to_dict(replace(facts,
        roles=({"job_title": forged},), fields={**facts.fields,
        "posting_scope": ExtractedField("multi_role_announcement", "explicit", ())})), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError) as exc:
        guided(session, record)
    assert_blocked(exc)


def test_corrupt_current_certificate_blocks_even_with_legacy_passing_score(session):
    record = make_import(session)
    result = guided(session, record)
    job = session.get(Job, result.job_id)
    snap = json.loads(job.confirmed_content_json)
    snap["review_confirmation"]["facts_sha256"] = "0" * 64
    job.confirmed_content_json = json.dumps(snap)
    job.quality_score = 85
    session.commit()
    assert distribution_blockers(job)
    with pytest.raises(ValueError):
        load_confirmed_snapshot(job)


def test_legacy_zero_score_still_rejected_and_old_atomicity_is_preserved(session):
    record = make_import(session)
    with pytest.raises(ValueError, match="70|质量"):
        legacy_confirm(session, record, valid_data(quality_score=0, edits={"title": "原子回滚"}))
    session.refresh(record)
    assert record.title != "原子回滚" and record.job_id is None


@pytest.mark.parametrize("guided_mode", [True, False])
@pytest.mark.parametrize("original_crlf,stored_browser_draft", [(False, False), (False, True), (True, False)])
def test_form_crlf_only_changes_preserve_canonical_body_field_and_evidence(session, guided_mode, original_crlf, stored_browser_draft):
    source = "星辰公司招聘财务实习生，应届生可投。\n岗位职责：报表整理\n协助对账。\n工作地点：上海。\n报名截止：2099年12月31日。\n官方投递邮箱：jobs@example.com。"
    if original_crlf:
        source = source.replace("\n", "\r\n")
    record = make_import(session, text=source)
    facts = workflow().read_import_facts(record)
    refs = facts.fields["employer_name"].evidence_refs
    summary = ExtractedField("报表整理\n协助对账", "explicit", refs)
    record.fields_json = json.dumps(facts_to_dict(replace(facts,
        fields={**facts.fields, "role_summary": summary})), ensure_ascii=False)
    session.commit()
    original = record.ocr_text
    browser_text = original.replace("\r\n", "\n").replace("\n", "\r\n")
    if stored_browser_draft:
        record.edited_text = browser_text
        session.commit()
    edits = {"edited_text": browser_text,
        "fields": {"role_summary": summary.value.replace("\n", "\r\n")},
        "field_evidence": {"role_summary": refs[0].quote.replace("\r\n", "\n").replace("\n", "\r\n")}}
    if guided_mode:
        result = guided(session, record, {"acknowledge": True, "edits": edits})
    else:
        result = legacy_confirm(session, record, valid_data(edits=edits))
    assert result.state == "publishable"
    assert record.ocr_text == original and workflow().effective_import_text(record) == original
    assert workflow().read_import_facts(record).fields["role_summary"] == summary
    assert load_confirmed_snapshot(session.get(Job, result.job_id))["fields"]["role_summary"] == summary.value


def test_selected_effective_location_audience_match_formal_headers_and_preserve_common(session):
    from app.services.screenshot_contracts import EvidenceRef
    text = "星辰公司招聘。\n财务实习生：应届生可投，工作地点：上海。\n设计实习生：本科在读可投，工作地点：北京。\n通用条件：所有岗位均须遵守诚信要求。\n报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text)
    facts = workflow().read_import_facts(record)
    def fld(value, quote):
        start = record.ocr_text.index(quote)
        return ExtractedField(value, "explicit", (EvidenceRef(None, None, start, start + len(quote), quote, "manual"),))
    roles = tuple({"job_title": fld(title, quote), "location_detail": fld(location, quote),
        "target_audience": fld(audience, quote)} for title, location, audience, quote in (
        ("财务实习生", "上海", "应届生", "财务实习生：应届生可投，工作地点：上海。"),
        ("设计实习生", "北京", "本科在读", "设计实习生：本科在读可投，工作地点：北京。")))
    common = fld("所有岗位均须遵守诚信要求", "通用条件：所有岗位均须遵守诚信要求。")
    record.fields_json = json.dumps(facts_to_dict(replace(facts, roles=roles, common_requirements=(common,))), ensure_ascii=False)
    record.selected_roles_json = "[1]"
    session.commit()
    _, effective, _, _ = workflow().guided_review_readiness(session, record)
    assert effective.fields["location_detail"].value == "北京"
    assert effective.fields["target_audience"].value == "本科在读"
    assert effective.common_requirements == (common,)
    result = guided(session, record)
    job = session.get(Job, result.job_id)
    snapshot = load_confirmed_snapshot(job)
    assert (job.location_detail, job.target_audience) == ("北京", "本科在读")
    assert snapshot["fields"]["location_detail"] == "北京"
    assert snapshot["fields"]["target_audience"] == "本科在读"
    for item in session.query(DistributionItem).all():
        assert "北京" in item.content and "本科在读" in item.content and "所有岗位均须遵守诚信要求" in item.content
        assert "地点：上海" not in item.content and "适合人群：应届生" not in item.content
    assert workflow().read_import_facts(record).fields["location_detail"].value == "上海"


def test_selected_effective_fields_fall_back_for_each_role_and_keep_global_constraints(session):
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    title = facts.fields["job_title"]
    role = {"job_title": title}
    record.fields_json = json.dumps(facts_to_dict(replace(facts, roles=(role,))), ensure_ascii=False)
    session.commit()
    effective = workflow().guided_selected_import_facts(record)
    assert effective.fields["location_detail"] == facts.fields["location_detail"]
    assert effective.fields["target_audience"] == facts.fields["target_audience"]
    assert effective.common_requirements == facts.common_requirements


def test_post_save_conflict_versions_identify_own_saved_draft_not_other_writer(session, monkeypatch):
    record = make_import(session)
    service = workflow()
    original_reserve = service._reserve
    calls = 0
    own_row = record.row_version + 1
    def interleave(current, import_id, *versions):
        nonlocal calls
        calls += 1
        if calls == 2:
            with Session(session.get_bind()) as other:
                other_record = other.get(type(record), record.id)
                service.save_import_edits(other, record.id, {"title": "OTHER WRITER"}, expected_row_version=other_record.row_version)
        return original_reserve(current, import_id, *versions)
    monkeypatch.setattr(service, "_reserve", interleave)
    with pytest.raises(EditConflict) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "MY EDIT"}})
    assert exc.value.draft_saved is True
    assert exc.value.own_saved_versions == {"expected_row_version": own_row, "content_revision": 2,
        "expected_job_row_version": None, "expected_job_version": None}
    session.refresh(record)
    assert record.title == "OTHER WRITER" and record.row_version == own_row + 1
    with pytest.raises(EditConflict):
        guided(session, record, {"acknowledge": True, "edits": {"title": "MY EDIT"}},
            expected_row_version=exc.value.own_saved_versions["expected_row_version"])


def test_own_save_versions_are_captured_before_commit_expiration_and_competing_write(session, monkeypatch):
    record = make_import(session)
    own_row = record.row_version + 1
    original_commit = session.commit
    first = True
    def interleave_commit():
        nonlocal first
        original_commit()
        if first:
            first = False
            with Session(session.get_bind()) as other:
                other_record = other.get(type(record), record.id)
                workflow().save_import_edits(other, record.id, {"title": "OTHER WRITER"}, expected_row_version=other_record.row_version)
            session.expire_all()
    monkeypatch.setattr(session, "commit", interleave_commit)
    with pytest.raises(EditConflict) as exc:
        guided(session, record, {"acknowledge": True, "edits": {"title": "MY EDIT"}})
    assert exc.value.own_saved_versions["expected_row_version"] == own_row
    assert exc.value.own_saved_versions["content_revision"] == 2
    session.refresh(record)
    assert record.title == "OTHER WRITER" and record.content_revision == 3
