from datetime import datetime
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
