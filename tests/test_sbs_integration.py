from datetime import datetime
from dataclasses import replace
import pytest
from app.database import create_database
from app.models import Job, Source
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG, ensure_official_source_catalog

SBS_NAME = "上海商学院就业网（待专用适配）"

def test_catalog_registers_sbs_as_an_enabled_a_trial_source_with_a_stable_adapter_key():
    source = next(item for item in OFFICIAL_SOURCE_CATALOG if item.name == SBS_NAME)
    assert (source.url, source.source_key, source.adapter_key, source.library_tier, source.is_enabled) == ("https://jiuye.sbs.edu.cn/PositionList.aspx/", "sbs-jobs", "sbs_jobs", "A", True)

def test_sbs_is_registered_for_trials_and_converts_a_verified_detail_to_a_candidate(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget
    from app.sources.sbs_jobs import SbsJobDetail, SbsListing
    listing = SbsListing("1001", "上海示例咨询有限公司", "财务分析实习生", "2026-09-15", "https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=1001")
    detail = SbsJobDetail(listing.title, listing.published_at, listing.detail_url, "岗位职责：协助财务分析。任职要求：2027届在校生。官方报名入口：https://jobs.example.com/apply/1001", listing.job_id, listing.employer_name, "明确上海", "上海市浦东新区", "https://jobs.example.com/apply/1001", "2026-10-15")
    monkeypatch.setattr(source_trials, "fetch_sbs_listings", lambda *_args, **_kwargs: [listing])
    monkeypatch.setattr(source_trials, "fetch_sbs_detail", lambda *_args, **_kwargs: detail)
    report = source_trials.TRIAL_ADAPTERS["sbs_jobs"](object(), TrialBudget(pages=1, list_limit=10, detail_limit=3))
    assert (report.list_count, report.detail_success_count, report.candidates[0].identity_key, report.candidates[0].application_kind) == (1, 1, "1001", "official_url")

def test_catalog_sync_promotes_existing_sbs_source_to_enabled_a_trial_with_eight_hour_interval():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session); source = session.query(Source).filter_by(name=SBS_NAME).one()
    assert (source.library_tier, source.is_enabled, source.validation_state, source.check_frequency_hours) == ("A", True, "A类试运行", 8)

def test_collect_sbs_jobs_saves_only_a_qualified_shanghai_detail(monkeypatch):
    from app.services import real_collection
    from app.sources.sbs_jobs import SbsJobDetail, SbsListing
    listing = SbsListing("1001", "上海示例咨询有限公司", "财务分析实习生", "2026-09-15", "https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=1001")
    detail = SbsJobDetail(listing.title, listing.published_at, listing.detail_url, "岗位职责：协助财务分析。任职要求：2027届在校生。官方报名入口：https://jobs.example.com/apply/1001", listing.job_id, listing.employer_name, "明确上海", "上海市浦东新区", "https://jobs.example.com/apply/1001", "2026-10-15")
    monkeypatch.setattr(real_collection, "fetch_sbs_listings", lambda *_args, **_kwargs: [listing])
    monkeypatch.setattr(real_collection, "fetch_sbs_detail", lambda *_args, **_kwargs: detail)
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        result = real_collection.collect_sbs_jobs(session, object(), now=datetime(2026, 9, 16, 9, 0)); jobs = session.query(Job).all()
    assert (result.created_jobs, len(jobs), jobs[0].source_url, jobs[0].status) == (1, 1, listing.detail_url, "待核验")


def _review_sample():
    from app.sources.sbs_jobs import SbsJobDetail, SbsListing
    listing = SbsListing("review-1", "上海示例服务有限公司", "策划专员", "2026-09-20", "https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=35001")
    detail = SbsJobDetail(listing.title, listing.published_at, listing.detail_url,
        "职位描述：负责市场资料整理、项目策划、活动执行与数据分析，协助团队进行客户沟通和项目复盘。\n截止日期：2026-11-30\n官方投递邮箱：jobs@example.com",
        listing.job_id, listing.employer_name, "明确上海", "上海市", "", "2026-11-30")
    return listing, detail


def _mock_sbs(monkeypatch, detail):
    from app.services import real_collection
    listing, _ = _review_sample()
    monkeypatch.setattr(real_collection, "fetch_sbs_listings", lambda *_a, **_kw: [listing])
    monkeypatch.setattr(real_collection, "fetch_sbs_detail", lambda *_a, **_kw: detail)


def test_sbs_review_only_lead_is_visible_but_not_promoted_or_duplicated(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    assert evaluate_candidate(candidate_from_detail(detail)).verdict == "needs_evidence"
    first = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    assert first.created_jobs == 1
    job = session.query(Job).one()
    assert (job.intake_grade, job.status, job.intake_route) == ("C", "待核验", "人工复核")
    assert "受众" in job.intake_reason
    assert "TARGET_AUDIENCE_UNCLEAR" in job.evidence_note
    created_at = job.created_at
    # Recollection must not reverse a later manual decision on unchanged content.
    job.intake_grade, job.intake_reason = "B", "人工已确认接收应届生"
    session.commit()
    second = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26, 8))
    assert (second.created_jobs, second.unchanged_jobs, session.query(Job).count()) == (0, 1, 1)
    assert job.created_at == created_at
    assert job.intake_grade == "B"


@pytest.mark.parametrize("change", [
    {"location_category": "其他地区", "location_detail": "浙江省"},
    {"evidence_text": "职位描述：负责市场资料整理、项目策划、活动执行与数据分析。"},
    {"evidence_text": "职位描述：负责市场分析。要求三年以上工作经验。另有校招。官方投递邮箱：jobs@example.com"},
    {"evidence_text": "职位描述：负责市场分析。要求2年以上相关工作经验。官方投递邮箱：jobs@example.com"},
    {"evidence_text": "职位描述：实习岗位，仅限本校学生申请。官方投递邮箱：jobs@example.com"},
    {"evidence_text": "职位描述：负责市场分析，招聘仅面向上海商学院开放申请。官方投递邮箱：jobs@example.com"},
    {"evidence_text": "职位描述：负责市场分析。截止日期：2026-09-01。官方投递邮箱：jobs@example.com", "deadline": "2026-09-01"},
    {"employer_name": ""},
    {"evidence_text": "公司欢迎人才。官方投递邮箱：jobs@example.com"},
])
def test_review_intake_does_not_relax_hard_boundaries(monkeypatch, session, change):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, replace(detail, **change))
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    assert result.created_jobs == 0
    assert session.query(Job).count() == 0
    assert result.failed_jobs == 0  # policy exclusions are not fetch failures


def test_daily_entrypoint_really_collects_sbs_a_trial_source(monkeypatch, session):
    from app.services.real_collection import collect_due_sources
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    ensure_official_source_catalog(session)
    for source in session.query(Source).all():
        source.is_enabled = source.name == SBS_NAME
    session.commit()
    result = collect_due_sources(session, object(), now=datetime(2026, 9, 26))
    assert (result.attempted_sources, result.successful_sources, result.created_jobs) == (1, 1, 1)


def test_sbs_reports_policy_skips_separately_from_transport_failures(monkeypatch, session):
    from app.services import real_collection
    listing, detail = _review_sample()
    monkeypatch.setattr(real_collection, "fetch_sbs_listings", lambda *_a, **_kw: [listing, replace(listing, job_id="broken")])
    def fetch(_client, item, **kwargs):
        if item.job_id == "broken":
            raise TimeoutError("sample timeout")
        return replace(detail, location_category="其他地区", location_detail="浙江省")
    monkeypatch.setattr(real_collection, "fetch_sbs_detail", fetch)
    result = real_collection.collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    source = session.query(Source).filter_by(name=SBS_NAME).one()
    assert result.failed_jobs == 1
    assert "LOCATION_NOT_SHANGHAI" in source.last_monitor_summary
    assert "TimeoutError" in source.last_monitor_summary


def test_review_only_intake_does_not_call_paid_ai(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    calls = []
    def ai(_prompt):
        calls.append(_prompt)
        return '{}'
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26), intake_ai_complete=ai)
    assert result.created_jobs == 1
    assert calls == []


@pytest.mark.parametrize("body", [
    "职位描述：面向应届毕业生，参与市场分析，后续统一安排入职体检。官方投递邮箱：jobs@example.com",
    "职位描述：面向应届毕业生，协助部门负责人完成市场调研与数据分析。官方投递邮箱：jobs@example.com",
])
def test_normal_graduate_duties_and_application_labels_are_not_excluded(monkeypatch, session, body):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, replace(detail, evidence_text=body))
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    assert result.created_jobs == 1
    assert session.query(Job).one().intake_grade == "A"
