"""Admission safety gates for A-source review-only leads."""

from datetime import datetime

import pytest

from app.services.review_intake import evaluate_review_intake
from app.services.source_candidate_policy import SourceCandidate


def _candidate(requirement: str) -> SourceCandidate:
    return SourceCandidate(
        identity_key="test-1",
        announcement_id="test-1",
        job_id="",
        employer_name="上海示例公司",
        title="数据分析实习生",
        location_category="明确上海",
        location_detail="上海市杨浦区",
        published_at="2026-10-01",
        deadline="2026-10-20",
        evidence_text=(
            f"岗位职责：协助数据分析。任职要求：{requirement}。"
            "工作地点：上海。官方投递邮箱：apply@example.com"
        ),
        source_url="https://example.edu/jobs/test-1",
        application_kind="email",
        application_value="apply@example.com",
        application_evidence="官方投递邮箱：apply@example.com",
    )


@pytest.mark.parametrize("restriction", [
    "只招本校在校生",
    "仅接收本校学生",
    "不对外校开放",
])
def test_explicit_school_only_variants_are_not_reviewable(restriction):
    decision = evaluate_review_intake(_candidate(restriction), now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == ("excluded", ("SCHOOL_EXCLUSIVE",))


@pytest.mark.parametrize("requirement", [
    "要求2年工作经验",
    "须具备两年相关工作经验",
    "至少2年相关经验",
    "工作经验2年以上",
    "2年及以上从业经验",
])
def test_required_two_year_experience_is_not_reviewable(requirement):
    decision = evaluate_review_intake(_candidate(requirement), now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == (
        "excluded", ("UNSUITABLE_FOR_STUDENT_REVIEW",)
    )


@pytest.mark.parametrize("preference", [
    "有2年工作经验者优先",
    "2年以上相关经验优先",
    "有两年项目经验优先",
])
def test_experience_preferences_do_not_trigger_required_experience_gate(preference):
    decision = evaluate_review_intake(_candidate(preference), now=datetime(2026, 10, 5))
    assert decision.verdict == "qualified"


@pytest.mark.parametrize("open_statement", [
    "不限本校学生，外校学生均可报名",
    "不对外校设置限制，外校学生可申请",
])
def test_explicitly_open_school_language_is_not_treated_as_school_only(open_statement):
    decision = evaluate_review_intake(_candidate(open_statement), now=datetime(2026, 10, 5))
    assert decision.verdict == "qualified"
