import json

import pytest

from app.models import DistributionItem, Job, ReviewLog
from app.services.concurrency import EditConflict
from test_screenshot_workflow import call_save, create_job, make_import, valid_data, workflow


def confirm(session, record, data=None, *, action="approve_and_generate", actor=None, **changes):
    job = session.get(Job, record.job_id) if record.job_id else None
    kwargs = {"expected_row_version": record.row_version, "expected_job_row_version": job.row_version if job else None,
              "expected_job_version": job.version if job else None, "actor": actor}
    kwargs.update(changes)
    return workflow().confirm_screenshot_import(session, record.id, data or valid_data(), action=action, **kwargs)


def test_confirm_is_atomic_and_snapshot_survives_reload(session):
    record = make_import(session)
    result = confirm(session, record)
    assert result.state == "publishable" and len(result.distribution_ids) == 2
    job_id = result.job_id
    session.expire_all()
    job = session.get(Job, job_id)
    assert job.status == "可发布" and job.confirmed_content_version == job.version
    assert json.loads(job.confirmed_content_json)["fields"]["application_contact"] == "jobs@example.com"
    assert {item.channel for item in session.query(DistributionItem).all()} == {"微信群", "公众号"}


def test_confirm_distribution_failure_rolls_back_everything(session, monkeypatch):
    record = make_import(session)
    service = workflow()
    def fail(*args, **kwargs):
        raise RuntimeError("render failed")
    monkeypatch.setattr(service, "create_distribution_items", fail)
    with pytest.raises(RuntimeError, match="render failed"):
        confirm(session, record)
    session.refresh(record)
    assert record.job_id is None and record.row_version == 1
    assert session.query(Job).count() == session.query(DistributionItem).count() == session.query(ReviewLog).count() == 0


def test_submit_enters_pending_review_without_generating_distribution(session):
    record = make_import(session)
    result = confirm(session, record, action="submit")
    assert result.state == "pending_review" and result.distribution_ids == ()
    assert session.get(Job, result.job_id).status == "待审核"


def test_incomplete_material_or_unresolved_field_conflict_blocks_approval(session):
    record = make_import(session, material_complete=False)
    with pytest.raises(ValueError, match="完整"):
        confirm(session, record)
    assert session.query(Job).count() == 0


def test_no_year_deadline_needs_explicit_human_resolution(session):
    text = "星辰公司招聘财务实习生，应届生可投。工作地点：上海。报名截止：10月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text, fields={"deadline": "10月31日"})
    with pytest.raises(ValueError, match="日期|年份"):
        confirm(session, record)
    assert session.query(DistributionItem).count() == 0


def test_quality_score_is_not_automatically_raised(session):
    record = make_import(session)
    with pytest.raises(ValueError, match="70|质量"):
        confirm(session, record, valid_data(quality_score=0))
    assert session.query(DistributionItem).count() == 0


def test_stale_confirm_is_rejected_and_no_duplicate_jobs_created(session):
    record = make_import(session)
    version = record.row_version
    result = confirm(session, record)
    with pytest.raises(EditConflict):
        confirm(session, record, expected_row_version=version)
    assert session.query(Job).count() == 1 and session.query(DistributionItem).count() == 2
    assert result.state == "publishable"


def test_b_audience_remains_pending_for_member_then_reviewer_can_confirm(session):
    from app.auth.service import create_user
    member = create_user(session, "member", "A secure password", "普通成员")
    reviewer = create_user(session, "reviewer", "A secure password", "审核员", can_review=True)
    text = "星辰公司招聘财务专员。岗位职责：报表整理。任职要求：本科，经验不限。工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text, fields={"job_title": "财务专员", "recruitment_type": "社会招聘", "target_audience": ""})
    result = confirm(session, record, action="submit", actor=member)
    assert result.state == "pending_verification"
    job = session.get(Job, result.job_id)
    assert job.intake_grade == "B" and job.status == "待核验" and job.verification_version == 0
    call_save(session, record, {"supplemental_text": "官方补充：应届生可投。", "fields": {"target_audience": "应届生"}, "field_evidence": {"target_audience": "官方补充：应届生可投。"}})
    approved = confirm(session, record, valid_data(audience_confirmation={"evidence_url": "https://example.com/jobs", "evidence_excerpt": "官方补充：应届生可投。", "note": "已核对官方补充及完整岗位要求"}), actor=reviewer)
    assert approved.state == "publishable"
    assert "TARGET_AUDIENCE_UNCLEAR" not in session.get(Job, job.id).intake_reason


def test_d_cannot_be_promoted_through_audience_confirmation(session):
    text = "星辰公司招聘财务负责人，三年以上经验。工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text, fields={"job_title": "财务负责人", "target_audience": "社会人员"})
    job = create_job(session, record)
    assert job.intake_grade == "D"
    with pytest.raises(ValueError, match="D|派生"):
        confirm(session, record, valid_data(audience_confirmation={"evidence_url": "https://example.com/jobs", "evidence_excerpt": text, "note": "尝试确认"}))
    session.refresh(job)
    assert job.intake_grade == "D" and job.status == "待核验"


def test_multi_role_student_subset_keeps_individual_conditions_and_common_rules(session):
    service = workflow()
    # Save a multi-role fact set with independent paragraphs and all common rules.
    text = "星辰公司招聘\n财务实习生：本科，应届可投，岗位职责：报表整理。\n财务负责人：硕士，三年以上经验。\n通用条件：报名不得提交虚假资料。\n工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    from app.services.screenshot_extraction import facts_from_dict, facts_to_dict
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    record = make_import(session, text=text, fields={"posting_scope": "multi_role_announcement", "job_title": "财务实习生"})
    facts = service.read_import_facts(record)
    def fld(value, quote):
        start = record.ocr_text.index(quote)
        return ExtractedField(value, "explicit", (EvidenceRef(None, None, start, start + len(quote), quote, "manual"),))
    facts = ScreenshotFacts(facts.fields, (
        {"job_title": fld("财务实习生", "财务实习生：本科，应届可投，岗位职责：报表整理。"), "eligibility": fld("本科，应届可投", "财务实习生：本科，应届可投，岗位职责：报表整理。")},
        {"job_title": fld("财务负责人", "财务负责人：硕士，三年以上经验。"), "eligibility": fld("硕士，三年以上经验", "财务负责人：硕士，三年以上经验。")},
    ), (), 1, (fld("报名不得提交虚假资料", "通用条件：报名不得提交虚假资料。"),))
    record.fields_json = json.dumps(facts_to_dict(facts), ensure_ascii=False)
    record.selected_roles_json = "[0]"
    session.commit()
    result = confirm(session, record, valid_data(role_details_checked=True))
    job = session.get(Job, result.job_id)
    assert job.intake_grade == "A" and job.role_details_status == "checked"
    snapshot = json.loads(job.confirmed_content_json)
    assert snapshot["student_roles_excerpt"] is True
    assert len(snapshot["roles"]) == 1 and "本科" in snapshot["roles"][0]["eligibility"]
    assert snapshot["common_requirements"] == ["报名不得提交虚假资料"]


def test_general_fee_restriction_cannot_be_dropped_when_selecting_student_role(session):
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    quote = "通用条件：收费招聘。"
    record.supplemental_text = quote
    text = record.ocr_text + "\n" + quote
    ref = EvidenceRef(None, None, len(record.ocr_text) + 1, len(text), quote, "manual")
    record.fields_json = json.dumps(facts_to_dict(ScreenshotFacts(facts.fields, ({"job_title": facts.fields["job_title"]},), (), 1, (ExtractedField(quote, "explicit", (ref,)),))), ensure_ascii=False)
    record.selected_roles_json = "[0]"
    session.commit()
    assert create_job(session, record).intake_grade == "D"


def test_explicit_registration_fee_stays_d_even_for_student_announcement(session):
    text = "星辰公司招聘财务实习生，应届生可投。报名要求：必须缴纳报名费500元。工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text)
    assert create_job(session, record).intake_grade == "D"


def test_c_requires_complete_official_evidence_before_audience_promotion(session):
    record = make_import(session)
    job = create_job(session, record)
    job.intake_grade = "C"
    job.intake_reason = "原文信息不足，受众未明确"
    session.commit()
    result = confirm(session, record, valid_data(audience_confirmation={"evidence_url": "https://example.com/jobs", "evidence_excerpt": "应届生可投", "note": "核对完整招聘材料"}))
    assert result.state == "publishable"
    assert job.intake_grade == "B"
    audit = session.query(ReviewLog).filter_by(job_id=job.id, action="截图受众确认").one()
    assert json.loads(audit.note)["old_grade"] == "C"


def test_claimed_audience_quote_not_in_material_cannot_upgrade_c(session):
    record = make_import(session)
    job = create_job(session, record)
    job.intake_grade, job.intake_reason = "C", "原文信息不足"
    session.commit()
    with pytest.raises(ValueError, match="摘录"):
        confirm(session, record, valid_data(audience_confirmation={"evidence_url": "https://example.com/jobs", "evidence_excerpt": "不存在的应届声明", "note": "核对"}))
    session.refresh(job)
    assert job.intake_grade == "C"


def test_normal_ui_manual_categories_links_and_checks_can_complete(session):
    record = make_import(session, fields={"official_url": "", "job_family": "", "student_fit_level": "", "distribution_recommendation": "", "application_method": "", "location_category": ""})
    call_save(session, record, {"source_url": "https://example.com/public-source", "fields": {
        "official_url": "https://example.com/new-official", "job_family": "财会审计", "student_fit_level": "核心适配",
        "distribution_recommendation": "进入学生分发审核", "application_method": "email", "location_category": "明确上海"}})
    result = confirm(session, record)
    assert result.state == "publishable"
    job = session.get(Job, result.job_id)
    assert job.official_url == "https://example.com/new-official"
    manual_evidence = json.loads(record.evidence_json)
    assert any(item.get("field") == "official_url" and item.get("kind") == "human_provided_url" for item in manual_evidence)


def test_changed_material_invalidates_prior_b_audience_confirmation(session):
    record = make_import(session)
    job = create_job(session, record)
    job.intake_grade = "B"
    job.intake_reason = "审核员依据官方证据确认学生受众，仍须完整核验与审核"
    session.commit()
    call_save(session, record, {"supplemental_text": "补充：投递材料增加成绩单。"})
    assert "TARGET_AUDIENCE_UNCLEAR" in job.intake_reason
    assert confirm(session, record, action="submit").state == "pending_verification"


def test_missing_deadline_only_uses_unstated_sentinel_after_human_confirmation(session):
    record = make_import(session, fields={"deadline": ""})
    with pytest.raises(ValueError, match="截止"):
        confirm(session, record)
    result = confirm(session, record, valid_data(deadline_unspecified_confirmed=True))
    job = session.get(Job, result.job_id)
    assert job.deadline == "公告未明确统一截止时间"
    snapshot = json.loads(job.confirmed_content_json)
    assert snapshot["fields"]["deadline_state"] == "missing"


@pytest.mark.parametrize("changes", [{"lifecycle_status": "已撤销"}, {"status": "已截止"}, {"status": "淘汰"}])
def test_withdrawn_expired_or_rejected_job_cannot_confirm(session, changes):
    record = make_import(session)
    job = create_job(session, record)
    for key, value in changes.items():
        setattr(job, key, value)
    session.commit()
    with pytest.raises(ValueError):
        confirm(session, record)
    assert session.query(DistributionItem).count() == 0


def test_audit_failure_rolls_back_b_confirmation_and_distribution(session, monkeypatch):
    record = make_import(session)
    service = workflow()
    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")
    monkeypatch.setattr(service, "record_event", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        confirm(session, record)
    session.refresh(record)
    assert record.job_id is None and session.query(Job).count() == 0


def test_fact_edit_while_pending_review_does_not_require_separate_return(session):
    record = make_import(session)
    result = confirm(session, record, action="submit")
    job = session.get(Job, result.job_id)
    old_version = job.version
    call_save(session, record, {"title": "星辰公司岗位更新"})
    assert job.status == "待核验" and job.version > old_version and job.verification_version == 0
    approved = confirm(session, record)
    assert approved.state == "publishable"


def test_fee_in_complete_supplement_blocks_even_if_ai_common_omitted_it(session):
    from app.services.screenshot_contracts import ScreenshotFacts
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    record.fields_json = json.dumps(facts_to_dict(ScreenshotFacts(facts.fields, ({"job_title": facts.fields["job_title"]},), (), 1)), ensure_ascii=False)
    record.supplemental_text = "通用条件：所有应聘者必须缴纳报名费500元。"
    session.commit()
    with pytest.raises(ValueError, match="D|缴费"):
        confirm(session, record)
    assert session.query(DistributionItem).count() == 0


@pytest.mark.parametrize("restriction", ["通用条件：收费招聘。", "通用条件：须办理培训贷。"], ids=["paid-recruitment", "training-loan"])
def test_existing_intake_prohibitions_in_full_material_block_when_roles_omit_common(session, restriction):
    from app.services.screenshot_contracts import ScreenshotFacts
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    record.fields_json = json.dumps(facts_to_dict(ScreenshotFacts(facts.fields, ({"job_title": facts.fields["job_title"]},), (), 1)), ensure_ascii=False)
    record.supplemental_text = restriction
    session.commit()
    with pytest.raises(ValueError, match="D"):
        confirm(session, record)
    assert session.query(DistributionItem).count() == 0


def test_unresolved_role_boundary_warning_blocks_and_manual_scope_resolution_is_audited(session):
    from dataclasses import replace
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    record.fields_json = json.dumps(facts_to_dict(replace(facts, warnings=("岗位边界不明确，请人工定位",))), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError, match="范围|边界"):
        confirm(session, record)
    result = confirm(session, record, valid_data(scope_confirmation={"single_employer": True, "role_boundaries": True,
        "employer_name": "星辰公司", "evidence_excerpt": record.ocr_text, "note": "核对原图，此批仅含星辰公司的财务实习岗位"}))
    assert result.state == "publishable"
    assert workflow().read_import_facts(record).warnings == ("岗位边界不明确，请人工定位",)
    assert any(item.get("kind") == "human_scope_confirmation" for item in json.loads(record.evidence_json))


def test_multiple_explicit_employers_require_partial_evidence_of_selected_unit(session):
    record = make_import(session, supplemental_text="招聘单位：星辰公司\n财务实习生：应届可投。\n招聘单位：另一家公司\n研究岗：硕士。")
    with pytest.raises(ValueError, match="单位|范围"):
        confirm(session, record)
    with pytest.raises(ValueError, match="单位|范围"):
        confirm(session, record, valid_data(scope_confirmation={"single_employer": True, "role_boundaries": True,
            "employer_name": "星辰公司", "evidence_excerpt": workflow().effective_import_text(record), "note": "全部确认"}))
    result = confirm(session, record, valid_data(scope_confirmation={"single_employer": True, "role_boundaries": True,
        "employer_name": "星辰公司", "evidence_excerpt": "招聘单位：星辰公司\n财务实习生：应届可投。", "note": "只选择该单位财务实习岗位，另一公司另批处理"}))
    assert result.state == "publishable"
    assert json.loads(session.get(Job, result.job_id).confirmed_content_json)["student_roles_excerpt"] is True


def test_common_condition_conflict_can_be_corrected_on_page_without_deleting_it(session):
    from dataclasses import replace
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session, supplemental_text="通用条件：所有应聘者报名无需缴费。")
    facts = workflow().read_import_facts(record)
    quote = "通用条件：所有应聘者报名无需缴费。"
    start = len(record.ocr_text) + 1
    record.fields_json = json.dumps(facts_to_dict(replace(facts, common_requirements=(ExtractedField("报名无需缴费 或 待确认", "conflict", (EvidenceRef(None, None, start, start + len(quote), quote, "manual"),)),))), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError, match="冲突"):
        confirm(session, record)
    call_save(session, record, {"common_fields": {"0": "所有应聘者报名无需缴费"}, "common_evidence": {"0": quote}})
    result = confirm(session, record)
    assert result.state == "publishable"
    snapshot = json.loads(session.get(Job, result.job_id).confirmed_content_json)
    assert snapshot["common_requirements"] == ["所有应聘者报名无需缴费"]


def test_common_condition_cannot_be_deleted_or_replaced_by_role_specific_quote(session):
    from dataclasses import replace
    from app.services.screenshot_contracts import ExtractedField
    from app.services.screenshot_extraction import facts_to_dict
    record = make_import(session)
    facts = workflow().read_import_facts(record)
    record.fields_json = json.dumps(facts_to_dict(replace(facts, common_requirements=(ExtractedField("公共条件待核实", "unclear", ()),))), ensure_ascii=False)
    session.commit()
    with pytest.raises(ValueError, match="公共|通用"):
        call_save(session, record, {"common_fields": {"0": ""}})
    with pytest.raises(ValueError, match="公共|通用"):
        call_save(session, record, {"common_fields": {"0": "本科"}, "common_evidence": {"0": "任职要求：本科，应届生可投。"}})
