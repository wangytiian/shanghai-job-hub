from datetime import datetime


def _candidate(**overrides):
    from app.services.source_candidate_policy import SourceCandidate

    values = {
        "identity_key": "notice-1:role-1",
        "announcement_id": "notice-1",
        "job_id": "role-1",
        "employer_name": "上海示例科技有限公司",
        "title": "产品运营实习生",
        "location_category": "明确上海",
        "location_detail": "上海市徐汇区",
        "published_at": "2026-09-10",
        "deadline": "",
        "evidence_text": "岗位职责：协助产品运营。任职要求：面向在校生，工作地点：上海。官方投递入口：https://jobs.example.com/apply",
        "source_url": "https://career.example.edu/detail/notice-1",
        "source_listing_url": "https://career.example.edu/jobs/list",
        "application_kind": "official_url",
        "application_value": "https://jobs.example.com/apply",
        "application_evidence": "官方投递入口：https://jobs.example.com/apply",
    }
    values.update(overrides)
    return SourceCandidate(**values)


def test_candidate_is_qualified_only_with_explicit_student_body_location_and_application_evidence():
    from app.services.source_candidate_policy import evaluate_candidate

    decision = evaluate_candidate(_candidate(), now=datetime(2026, 9, 11))

    assert decision.verdict == "qualified"
    assert decision.reason_codes == ()


def test_school_listing_url_cannot_be_used_as_application_proof():
    from app.services.source_candidate_policy import evaluate_candidate

    listing_url = "https://career.example.edu/jobs/list"
    decision = evaluate_candidate(
        _candidate(
            source_url=listing_url,
            application_value=listing_url,
            application_evidence=f"学校栏目：{listing_url}",
        ),
        now=datetime(2026, 9, 11),
    )

    assert decision.verdict == "needs_evidence"
    assert "APPLICATION_IS_SOURCE_PAGE" in decision.reason_codes


def test_school_listing_query_variant_cannot_be_used_as_application_proof():
    from app.services.source_candidate_policy import evaluate_candidate

    listing_url = "https://career.example.edu/jobs/list"
    application_url = f"{listing_url}?page=2#jobs"
    decision = evaluate_candidate(
        _candidate(
            source_url="https://career.example.edu/detail/notice-1",
            source_listing_url=listing_url,
            application_value=application_url,
            application_evidence=f"官方报名入口：{application_url}",
        ),
        now=datetime(2026, 9, 11),
    )

    assert decision.verdict == "needs_evidence"
    assert "APPLICATION_IS_SOURCE_PAGE" in decision.reason_codes


def test_original_article_url_is_traceability_not_application_evidence():
    from app.services.source_candidate_policy import evaluate_candidate

    article_url = "https://company.example.com/news/campus-2027"
    decision = evaluate_candidate(
        _candidate(
            application_value=article_url,
            application_evidence=f"官方原始链接：{article_url}",
        ),
        now=datetime(2026, 9, 11),
    )

    assert decision.verdict == "needs_evidence"
    assert "APPLICATION_EVIDENCE_INVALID" in decision.reason_codes


def test_non_shanghai_candidate_is_excluded_with_a_stable_reason():
    from app.services.source_candidate_policy import evaluate_candidate

    decision = evaluate_candidate(
        _candidate(location_category="其他地区", location_detail="北京市朝阳区"),
        now=datetime(2026, 9, 11),
    )

    assert decision.verdict == "excluded"
    assert "LOCATION_NOT_SHANGHAI" in decision.reason_codes


def test_explicit_senior_role_is_excluded_without_a_student_exception():
    from app.services.source_candidate_policy import evaluate_candidate

    decision = evaluate_candidate(
        _candidate(
            title="高级产品负责人",
            evidence_text="岗位职责：负责产品战略。任职要求：五年以上经验，工作地点：上海。官方投递入口：https://jobs.example.com/apply",
        ),
        now=datetime(2026, 9, 11),
    )

    assert decision.verdict == "excluded"
    assert "SENIOR_ROLE" in decision.reason_codes


def test_empty_body_needs_evidence_instead_of_becoming_a_candidate():
    from app.services.source_candidate_policy import evaluate_candidate

    decision = evaluate_candidate(_candidate(evidence_text=""), now=datetime(2026, 9, 11))

    assert decision.verdict == "needs_evidence"
    assert "BODY_MISSING" in decision.reason_codes
