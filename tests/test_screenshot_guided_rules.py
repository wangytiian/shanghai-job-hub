"""Pure readiness rules: no fixtures, database or provider calls."""
from dataclasses import replace
import importlib
from types import SimpleNamespace

import pytest

from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts


def service():
    assert importlib.util.find_spec("app.services.screenshot_review") is not None, "readiness rules are missing"
    return importlib.import_module("app.services.screenshot_review")


def field(value, state="explicit"):
    return ExtractedField(value, state, (EvidenceRef(None, None, 0, 2, "原文", "manual"),))


def ready(**changes):
    values = dict(employer_name="星辰公司", job_title="财务实习生", location_detail="上海",
        target_audience="本科应届生", deadline="2099-12-31", official_url="https://example.com/job",
        application_method="email", application_contact="jobs@example.com", posting_scope="single_role",
        attachment_status="not_required", student_fit_level="核心适配", notice_type="新招聘",
        distribution_recommendation="进入学生分发审核")
    values.update(changes)
    facts = ScreenshotFacts({name: value if isinstance(value, ExtractedField) else field(value) for name, value in values.items()}, (), (), 1)
    return SimpleNamespace(source_url="https://example.com/source", material_complete=True,
        ocr_text="星辰公司招聘财务实习生，本科应届生可投。", edited_text="", supplemental_text=""), facts


def errors(record, facts):
    return [issue for issue in service().collect_review_issues(record, facts) if issue.severity == "error"]


def test_reports_all_missing_core_facts_with_actionable_stable_paths():
    record, facts = ready(employer_name="", location_detail="", official_url="")
    record.source_url = ""
    issues = errors(record, facts)
    assert {i.path for i in issues} >= {"fields.employer_name", "fields.location_detail", "fields.official_url", "source_url"}
    assert all(i.code and i.message and i.action for i in issues)
    assert issues[0].to_dict() == vars(issues[0])


def test_ready_page_does_not_demand_final_acknowledgement():
    record, facts = ready()
    assert errors(record, facts) == []
    assert service().collect_review_issues(record, facts, acknowledgement=True) == ()


def test_malformed_facts_report_an_error_instead_of_throwing():
    record, _ = ready()
    issue, = service().collect_review_issues(record, None)
    assert issue.severity == "error" and issue.action


def test_diagnostic_conflicts_and_empty_optional_values_are_nonblocking():
    record, facts = ready(confidence=field("低", "conflict"), evidence=field("", "unclear"),
        rationale=field("相互矛盾", "conflict"), uncertainties=field("无法识别", "unclear"),
        salary=field("", "unclear"), role_summary=field("", "missing"), direction_tags=field("", "missing"))
    assert errors(record, facts) == []
    projected = service().publication_facts(facts)
    assert not {"confidence", "evidence", "rationale", "uncertainties", "salary", "role_summary", "direction_tags"} & projected.fields.keys()
    assert projected.fields["target_audience"].evidence_refs == facts.fields["target_audience"].evidence_refs


@pytest.mark.parametrize("name,value,state", [("salary", "5000/8000", "conflict"), ("eligibility", "", "unclear"),
    ("posting_scope", "single_role", "conflict"), ("fees", "500元", "conflict"), ("role_summary", "甲或乙", "unclear")])
def test_candidate_conflicts_and_critical_uncertainty_are_never_hidden(name, value, state):
    record, facts = ready(**{name: field(value, state)})
    assert any(i.path == f"fields.{name}" for i in errors(record, facts))
    assert name in service().publication_facts(facts).fields


def test_common_and_role_eligibility_conflicts_have_index_paths():
    record, facts = ready()
    facts = replace(facts, roles=({"job_title": field("财务实习生"), "eligibility": field("本科或硕士", "conflict")},),
        common_requirements=(field("", "unclear"),))
    assert {i.path for i in errors(record, facts)} >= {"roles.0.eligibility", "common_requirements.0"}
    assert service().publication_facts(facts).common_requirements == facts.common_requirements


@pytest.mark.parametrize("email", ["", "wrong", "a@@example.com", "a@localhost", "a@exa mple.com", ".a@example.com", "a..b@example.com"])
def test_email_method_requires_real_email(email):
    record, facts = ready(application_contact=email)
    assert any(i.path == "fields.application_contact" for i in errors(record, facts))


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://", "https://user:pass@example.com", "https://example.com:abc", "https://ex ample.com"])
def test_unsafe_or_malformed_urls_are_rejected(url):
    record, facts = ready(official_url=url)
    record.source_url = url
    assert {i.path for i in errors(record, facts)} >= {"fields.official_url", "source_url"}


@pytest.mark.parametrize("deadline,blocked", [("公告未明确统一截止时间", False), ("2099年12月31日", False),
    ("10月31日", True), ("", True), ("2099-02-30", True), ("2001-01-01", True)])
def test_deadlines_are_explicit_valid_and_current(deadline, blocked):
    record, facts = ready(deadline=deadline)
    assert any(i.path == "fields.deadline" for i in errors(record, facts)) is blocked


def test_original_text_and_material_completeness_are_independent_blockers():
    record, facts = ready()
    record.material_complete, record.ocr_text = False, " "
    assert {i.path for i in errors(record, facts)} >= {"material_complete", "edited_text"}


def test_bool_revision_is_not_an_integer_revision():
    record, facts = ready()
    assert errors(record, replace(facts, content_revision=True))


@pytest.mark.parametrize("audience", ["仅限硕士研究生", "博士应届生", "硕士及以上应届毕业生"])
def test_graduate_only_recruitment_cannot_claim_core_undergraduate_fit(audience):
    record, facts = ready(target_audience=audience)
    assert any(i.path == "fields.student_fit_level" for i in errors(record, facts))


def test_unrelated_company_background_does_not_exclude_undergraduates():
    record, facts = ready()
    record.ocr_text += "公司拥有硕士博士专家团队。"
    assert errors(record, facts) == []


def test_selected_role_graduate_requirement_is_checked_without_unrelated_roles():
    record, facts = ready()
    facts = replace(facts, roles=({"job_title": field("研究助理"), "eligibility": field("仅限博士")},))
    assert any(i.path == "fields.student_fit_level" for i in errors(record, facts))


def test_fee_and_unresolved_scope_are_critical():
    record, facts = ready()
    record.ocr_text += "报名必须缴纳报名费500元。"
    facts = replace(facts, warnings=("多单位岗位边界无法区分，请拆批",))
    assert {i.code for i in errors(record, facts)} >= {"recruitment_fee", "scope_unresolved"}


def test_no_fee_negation_is_preserved_and_not_blocked():
    record, facts = ready()
    record.ocr_text += "报名不收取费用，无需缴费。"
    assert errors(record, facts) == []


def test_target_line_graduate_requirement_is_critical_but_biography_is_not():
    record, facts = ready()
    record.ocr_text += "招聘对象：仅限硕士及以上应届生。"
    assert any(i.code == "graduate_only" for i in errors(record, facts))


def test_original_expiry_cannot_be_hidden_by_future_structured_date():
    record, facts = ready()
    record.ocr_text += "报名截止：2001年12月31日。"
    assert any(i.code == "deadline_expired" for i in errors(record, facts))


@pytest.mark.parametrize("eligibility", ["硕士优先，有相关实习经历者优先", "不限硕士学历", "不要求硕士学历", "博士学历优先", "学历不限，硕士研究生优先"])
def test_graduate_preferences_and_negation_do_not_exclude_undergraduates(eligibility):
    record, facts = ready(eligibility=eligibility)
    assert errors(record, facts) == []


def test_mandatory_graduate_threshold_is_not_cancelled_by_different_preference():
    record, facts = ready(eligibility="须具有硕士学历，博士优先")
    assert any(i.code == "graduate_only" for i in errors(record, facts))


def test_selected_undergraduate_role_ignores_unselected_graduate_qualification_lines():
    record, facts = ready(eligibility="仅限博士")
    record.ocr_text += "\n岗位一：财务实习生；学历要求：本科。\n岗位二：研究员；学历要求：仅限博士。"
    facts = replace(facts, roles=({"job_title": field("财务实习生"), "eligibility": field("本科")},))
    assert errors(record, facts) == []


def test_common_graduate_requirement_still_blocks_selected_undergraduate_role():
    record, facts = ready()
    facts = replace(facts, roles=({"job_title": field("财务实习生"), "eligibility": field("本科")},),
        common_requirements=(field("所有岗位须为硕士研究生及以上学历"),))
    assert any(i.code == "graduate_only" for i in errors(record, facts))


def test_raw_common_graduate_requirement_cannot_be_lost_on_role_selection():
    record, facts = ready()
    record.ocr_text += "\n公共条件：所有岗位必须具有硕士及以上学历。"
    facts = replace(facts, roles=({"job_title": field("财务实习生"), "eligibility": field("本科")},))
    assert any(i.code == "graduate_only" for i in errors(record, facts))


@pytest.mark.parametrize("text", [
    "最新报名截止：2099年12月31日。\n原报名截止：2001年12月31日，现已延长。",
    "原报名截止：2001年12月31日。现报名截止延长至2099年12月31日。",
    "报名截止时间由2001年12月31日延长至2099年12月31日。",
    "报名截止：2099年12月31日。原报名截止：2001年12月31日。",
    "报名开始：2001年12月31日。报名截止：2099年12月31日。",
    "报名时间自2001年12月31日起。报名截止：2099年12月31日。",
    "申请材料须包含2001年12月31日前取得的资格证书。报名截止：2099年12月31日。",
    "报名时间：2001年12月31日至2099年12月31日。",
])
def test_current_future_deadline_allows_extensions_opening_and_unrelated_material_dates(text):
    record, facts = ready()
    record.ocr_text += "\n" + text
    assert errors(record, facts) == []


def test_conflicting_current_deadlines_need_resolution_without_claiming_expiry():
    record, facts = ready()
    record.ocr_text += "\n报名截止：2001年12月31日。报名截止：2099年12月31日。"
    issues = errors(record, facts)
    assert any(i.code == "deadline_conflict" for i in issues)
    assert not any(i.code == "deadline_expired" for i in issues)


def test_expired_latest_extension_is_still_expired():
    record, facts = ready(deadline="2002-12-31")
    record.ocr_text += "\n原报名截止：2001年12月31日。现延长报名截止：2002年12月31日。"
    assert any(i.code == "deadline_expired" for i in errors(record, facts))


def test_raw_yearless_deadline_cannot_be_replaced_with_invented_year():
    record, facts = ready()
    record.ocr_text += "\n报名截止：10月31日。"
    assert any(i.code == "deadline_unresolved" for i in errors(record, facts))


def test_future_qualification_date_cannot_extend_expired_current_deadline():
    record, facts = ready()
    record.ocr_text += "\n报名截止：2001年12月31日，申请材料需提供2099年12月31日前取得的证书。"
    assert any(i.code == "deadline_expired" for i in errors(record, facts))


def test_yearless_application_window_cannot_be_given_an_invented_year():
    record, facts = ready()
    record.ocr_text += "\n报名时间：10月1日至10月31日。"
    assert any(i.code == "deadline_unresolved" for i in errors(record, facts))


@pytest.mark.parametrize("name", ["location_category", "recruitment_type"])
@pytest.mark.parametrize("state", ["missing", "unclear"])
def test_empty_internal_classifications_are_optional_and_projected_out(name, state):
    record, facts = ready(**{name: field("", state)})
    assert errors(record, facts) == []
    assert name not in service().publication_facts(facts).fields
    assert service().publication_facts(facts).fields["location_detail"].value == "上海"


@pytest.mark.parametrize("name,value", [("location_category", "明确上海或其他地区"), ("recruitment_type", "实习或校招")])
def test_conflicting_internal_classification_candidates_remain_visible_and_blocking(name, value):
    record, facts = ready(**{name: field(value, "conflict")})
    assert any(issue.path == f"fields.{name}" for issue in errors(record, facts))
    assert service().publication_facts(facts).fields[name] == facts.fields[name]


def test_explicit_latest_full_date_resolves_original_unlabelled_yearless_deadline():
    record, facts = ready(deadline="2099-10-31")
    record.ocr_text += "\n报名截止：10月31日。"
    record.supplemental_text = "最新报名截止：2099年10月31日。"
    assert errors(record, facts) == []


@pytest.mark.parametrize("update", ["最新报名截止：10月31日。", "最新报名截止：2099年2月30日。"])
def test_yearless_or_invalid_latest_update_stays_unresolved(update):
    record, facts = ready()
    record.ocr_text += "\n报名截止：2099年12月31日。"
    record.supplemental_text = update
    assert any(issue.code == "deadline_unresolved" for issue in errors(record, facts))


def test_competing_latest_full_date_updates_are_conflict_not_guessed_order():
    record, facts = ready(deadline="2099-10-31")
    record.ocr_text += "\n报名截止：10月31日。"
    record.supplemental_text = "最新报名截止：2099年10月31日。最新报名截止：2099年11月30日。"
    assert any(issue.code == "deadline_conflict" for issue in errors(record, facts))
