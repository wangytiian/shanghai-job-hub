"""Four public-source trials feed the existing pending-review pool, never publishing."""
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.models import Job, Source, SourceTrialRun, SourceTrialSample
from app.services.b_pilot_intake import import_trial_run
from app.services.source_candidate_policy import RULE_VERSION, SourceCandidate, evaluate_candidate, candidate_from_detail
from app.services.source_trials import _sample_from_candidate, is_registered_trial_source
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG


ROUTES = (
    ("ncss-shanghai", "ncss_shanghai_jobs", "www.ncss.cn"),
    ("sues-internships", "campus_jsonp_sues", "career.sues.edu.cn"),
    ("sit-internships", "campus_jsonp_sit", "job.sit.edu.cn"),
    ("shcpa-jobs", "shcpa_jobs", "www.shcpa.org.cn"),
)
NOW = datetime(2026, 10, 10, 15)


def _candidate(host, requirement="接受2027届本科毕业生"):
    identity = "1" * 32 if host in {"career.sues.edu.cn", "job.sit.edu.cn"} else ("7453" if host == "www.shcpa.org.cn" else "notice1")
    source_url = {
        "www.ncss.cn": f"https://{host}/student/jobs/{identity}/detail.html",
        "career.sues.edu.cn": f"https://{host}/page/positionsx.html?wid={identity}",
        "job.sit.edu.cn": f"https://{host}/page/positionzp.html?wid={identity}&type=1",
        "www.shcpa.org.cn": f"https://{host}/recruit/getRecruitDetailedView.do?rid={identity}",
    }[host]
    return SourceCandidate(
        identity_key=identity, announcement_id=identity, job_id="",
        employer_name="上海示例企业", title="财务助理", location_category="明确上海",
        location_detail="上海市黄浦区", published_at="2026-10-09", deadline="",
        evidence_text=f"岗位职责：整理财务报表。任职要求：{requirement}。工作地点：上海。官方投递邮箱：hr@example.com",
        source_url=source_url, application_kind="email",
        application_value="hr@example.com", application_evidence="官方投递邮箱：hr@example.com",
    )


@pytest.mark.parametrize("key,adapter,host", ROUTES)
def test_four_routes_registered_and_catalog_b_disabled(key, adapter, host):
    entry = next(item for item in OFFICIAL_SOURCE_CATALOG if item.source_key == key)
    assert (entry.adapter_key, entry.library_tier, entry.is_enabled) == (adapter, "B", False)
    assert is_registered_trial_source(Source(source_key=key, adapter_key=adapter))


@pytest.mark.parametrize("key,adapter,host", ROUTES)
@pytest.mark.parametrize("requirement,grade", [("接受2027届本科毕业生", "B"), ("熟悉Excel", "B")])
def test_four_routes_import_explicit_sample_pending_and_idempotent(session, key, adapter, host, requirement, grade):
    source = Source(name=key, source_key=key, adapter_key=adapter, adapter_version="test-v1",
                    url=f"https://{host}/list", library_tier="B", is_enabled=False,
                    level="一级", source_type="公开就业平台", status="正常")
    session.add(source)
    session.flush()
    run = SourceTrialRun(run_id="four-run", source_id=source.id, adapter_version=source.adapter_version,
                         rule_version=RULE_VERSION, state="completed", started_at=NOW, finished_at=NOW,
                         idempotency_key="four-run")
    session.add(run)
    session.flush()
    candidate = _candidate(host, requirement)
    sample = _sample_from_candidate(run.run_id, candidate, evaluate_candidate(candidate, now=NOW))
    session.add(sample)
    session.commit()
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW, sample_ids=(sample.id,))
    assert (result.created, result.skipped) == (1, 0)
    job = session.query(Job).one()
    assert (job.status, job.is_demo, job.intake_grade) == ("待核验", False, grade)
    assert (source.library_tier, source.is_enabled) == ("B", False)
    again = import_trial_run(session, source.id, run.run_id, None, now=NOW, sample_ids=(sample.id,))
    assert (again.created, again.unchanged, session.query(Job).count()) == (0, 1, 1)


@pytest.mark.parametrize("requirement", ["硕士及以上学历，接受2027届毕业生", "学历要求：博士", "可随意填"])
def test_hard_undergraduate_and_placeholder_exclusions(requirement):
    decision = evaluate_candidate(_candidate("www.ncss.cn", requirement), now=NOW)
    assert decision.verdict == "excluded"


@pytest.mark.parametrize("requirement", ["本科及以上学历，硕士优先，接受2027届毕业生", "本科或硕士在校生", "接受2027届毕业生。学历要求：硕士,本科", "接受2027届毕业生。学历要求：博士、硕士、本科"])
def test_masters_preference_or_alternative_is_not_hard_exclusion(requirement):
    assert evaluate_candidate(_candidate("www.ncss.cn", requirement), now=NOW).verdict == "qualified"


NATIVE_PROOF = "平台原生投递证据：.job .apply > a[href='#']；按钮=投递简历；原始href=#；需登录后提交"


def test_verified_ncss_native_control_survives_normalization_and_persisted_policy():
    detail = SimpleNamespace(identity_key="abc123", title="财务实习生", employer_name="示例公司",
        detail_url="https://www.ncss.cn/student/jobs/abc123/detail.html", published_at="2026-10-09",
        location_category="明确上海", location_detail="上海市", official_url="",
        evidence_text=f"岗位职责：整理报表。任职要求：本科在校生。\n{NATIVE_PROOF}",
        native_application_verified=True, native_application_selector=".job .apply > a[href='#']",
        native_application_href="#")
    candidate = candidate_from_detail(detail)
    assert candidate.application_kind == "official_platform"
    assert candidate.application_evidence == NATIVE_PROOF
    assert evaluate_candidate(candidate, now=NOW).verdict == "qualified"
    for change in ({"source_url": "https://other.example/student/jobs/abc123/detail.html"},
                   {"application_value": "https://www.ncss.cn/student/jobs/other/detail.html"},
                   {"evidence_text": "岗位职责：财务实习生"}):
        assert evaluate_candidate(replace(candidate, **change), now=NOW).verdict != "qualified"


def test_ncss_native_proof_text_without_structured_adapter_verification_is_not_application():
    detail = SimpleNamespace(identity_key="abc123", title="财务实习生", employer_name="示例公司",
        detail_url="https://www.ncss.cn/student/jobs/abc123/detail.html",
        evidence_text=f"岗位职责：整理报表。\n{NATIVE_PROOF}", official_url="")
    assert candidate_from_detail(detail).application_kind == ""


@pytest.mark.parametrize("requirement", ["接受2027届毕业生，五年以上工作经验", "接受在校生，要求2年工作经验"])
def test_student_words_do_not_override_mandatory_experience(requirement):
    assert evaluate_candidate(_candidate("www.ncss.cn", requirement), now=NOW).verdict == "excluded"


@pytest.mark.parametrize("key,adapter,host", ROUTES)
def test_new_import_urls_must_bind_fixed_official_path_to_identity(key, adapter, host):
    from app.services.b_pilot_intake import _sample_problem
    sample = _sample_from_candidate("run", _candidate(host), evaluate_candidate(_candidate(host), now=NOW))
    sample.source_url = f"https://{host}/unrelated/page"
    assert _sample_problem(sample, host) == "SOURCE_URL_MISMATCH"


@pytest.mark.parametrize("restriction", ["仅限上海工程技术大学学生", "仅面向上海应用技术大学学生"])
def test_new_school_exclusive_restrictions_block_undergraduate_pool(restriction):
    from app.services.review_intake import evaluate_review_intake
    assert evaluate_review_intake(_candidate("career.sues.edu.cn", f"接受2027届毕业生，{restriction}"), now=NOW).reason_codes == ("SCHOOL_EXCLUSIVE",)


def test_real_english_description_labels_are_body_evidence():
    candidate = replace(_candidate("career.sues.edu.cn"), title="商业地产实习生",
        evidence_text="Responsibilities: Assist market research and data management. Person Specification: available three days weekly. 官方投递邮箱：hr@example.com")
    assert evaluate_candidate(candidate, now=NOW).verdict == "qualified"


@pytest.mark.parametrize("text,verdict", [("1.负责财务报表整理，协助费用核对与数据归档。", "qualified"), ("详见招聘公告或其他职位详情，请自行查阅。", "needs_evidence")])
def test_visible_ncss_detail_section_can_prove_body_but_not_placeholder(text, verdict):
    candidate = replace(_candidate("www.ncss.cn"), title="财务实习生",
        evidence_text=f"岗位名称：财务实习生\n职位详情\n{text}\n官方投递邮箱：hr@example.com")
    assert evaluate_candidate(candidate, now=NOW).verdict == verdict
