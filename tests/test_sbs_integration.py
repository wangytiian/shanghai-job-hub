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
    assert (job.intake_grade, job.status, job.intake_route) == ("B", "待核验", "普通待核验")
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


@pytest.mark.parametrize("changed_url", [
    "https://outside.example.edu/PositionDetail.aspx?zwid=35001",
    "http://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=35001",
])
def test_sbs_same_body_changed_detail_url_rechecks_host_and_revokes_old_review(monkeypatch, session, changed_url):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    assert collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26)).created_jobs == 1
    job = session.query(Job).one()
    original_fingerprint = job.content_fingerprint
    original_version, original_row_version = job.version, job.row_version
    job.status = "可发布"
    job.verification_checks = '{"source_checked": true}'
    job.verification_version = 1
    job.quality_score = 86
    session.commit()

    _mock_sbs(monkeypatch, replace(detail, detail_url=changed_url))
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert (result.updated_jobs, result.unchanged_jobs) == (1, 0)
    assert job.source_url == changed_url
    assert job.content_fingerprint == original_fingerprint
    assert (job.intake_grade, job.intake_route, job.status) == ("C", "人工复核", "待核验")
    assert "HTTPS" in job.intake_reason
    assert (job.verification_checks, job.verification_version, job.quality_score) == ("{}", 0, 0)
    assert (job.version, job.row_version) == (original_version + 1, original_row_version + 1)


def test_sbs_changed_source_evidence_invalidates_stale_structure_form(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()
    old_version, old_row_version = job.version, job.row_version
    job.quality_score = 82
    job.ai_suggested_score = 90
    job.ai_score_status = "AI建议"
    session.commit()

    _mock_sbs(monkeypatch, replace(detail, evidence_text=detail.evidence_text + "\n补充：每周到岗四天。"))
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert result.updated_jobs == 1
    assert (job.version, job.row_version) == (old_version + 1, old_row_version + 1)
    assert (job.quality_score, job.ai_suggested_score, job.ai_score_status) == (0, 0, "待建议")


def test_sbs_excluded_refresh_retracts_existing_approved_job_without_deleting_history(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()
    old_id, old_version, old_row_version = job.id, job.version, job.row_version
    job.status, job.intake_grade, job.verification_checks, job.verification_version = "可发布", "B", '{"source_checked": true}', 1
    session.commit()

    excluded = replace(detail, location_category="其他地区", location_detail="浙江省", evidence_text=detail.evidence_text + "\n岗位地点：浙江省。")
    _mock_sbs(monkeypatch, excluded)
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert result.updated_jobs == 1
    assert session.query(Job).count() == 1
    assert job.id == old_id
    assert (job.status, job.intake_grade, job.intake_route) == ("待核验", "D", "过滤留档")
    assert "LOCATION_NOT_SHANGHAI" in job.intake_reason
    assert job.evidence_text == excluded.evidence_text
    assert (job.verification_checks, job.verification_version) == ("{}", 0)
    assert (job.version, job.row_version) == (old_version + 1, old_row_version + 1)

    repeated = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27, 8))
    assert repeated.updated_jobs == 0
    assert (job.version, job.row_version) == (old_version + 1, old_row_version + 1)


def test_sbs_qualified_refresh_replaces_old_manual_audience_override(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()
    job.intake_grade = "B"
    job.intake_reason = "人工曾确认适用，但原文后来变化"
    job.status = "可发布"
    session.commit()

    qualified = replace(detail, evidence_text=(
        "职位描述：负责市场资料整理、项目策划、活动执行与数据分析。"
        "任职要求：面向2027届应届毕业生。截止日期：2026-11-30。官方投递邮箱：jobs@example.com"
    ))
    _mock_sbs(monkeypatch, qualified)
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert result.updated_jobs == 1
    assert (job.intake_grade, job.intake_route, job.status) == ("A", "优先待核验", "待核验")
    assert "人工曾确认" not in job.intake_reason
    assert "应届" in job.intake_reason


def test_sbs_expired_refresh_retracts_existing_publishable_job(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()
    old_version, old_row_version = job.version, job.row_version
    job.status = "可发布"
    job.intake_grade = "B"
    session.commit()

    expired = replace(detail, evidence_text=(
        "职位描述：负责市场资料整理、项目策划、活动执行与数据分析。"
        "截止日期：2026-09-20。官方投递邮箱：jobs@example.com"
    ), deadline="2026-09-20")
    _mock_sbs(monkeypatch, expired)
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert result.updated_jobs == 1
    assert (job.status, job.lifecycle_status, job.deadline, job.intake_grade) == ("已截止", "已截止", "2026-09-20", "D")
    assert "APPLICATION_EXPIRED" in job.intake_reason
    assert (job.version, job.row_version) == (old_version + 1, old_row_version + 1)


def test_sbs_new_audience_uncertainty_clears_old_manual_evidence(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    qualified = replace(detail, evidence_text=(
        "职位描述：负责市场资料整理、项目策划、活动执行与数据分析。"
        "任职要求：面向2027届应届毕业生。截止日期：2026-11-30。官方投递邮箱：jobs@example.com"
    ))
    _mock_sbs(monkeypatch, qualified)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()
    job.intake_grade = "B"
    job.intake_evidence = "旧的人工受众证明"
    session.commit()

    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert (job.intake_grade, job.intake_route) == ("B", "普通待核验")
    assert "TARGET_AUDIENCE_UNCLEAR" in job.intake_reason
    assert job.intake_evidence == "未发现直接适配证据"


def test_sbs_changed_detail_refreshes_source_facts_before_new_manual_review(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()

    changed = replace(
        detail,
        title="品牌策划专员",
        location_detail="上海市徐汇区",
        deadline="2026-12-15",
        evidence_text=(
            "职位描述：负责市场资料整理、项目策划、活动执行与数据分析。"
            "工作地点：上海市徐汇区。截止日期：2026-12-15。官方投递邮箱：jobs@example.com"
        ),
    )
    _mock_sbs(monkeypatch, changed)
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))

    assert result.updated_jobs == 1
    assert (job.job_title, job.location_detail, job.deadline) == ("品牌策划专员", "上海市徐汇区", "2026-12-15")


def test_sbs_corrected_location_reenters_review_even_if_body_is_unchanged(monkeypatch, session):
    from app.services.real_collection import collect_sbs_jobs
    _, detail = _review_sample()
    _mock_sbs(monkeypatch, detail)
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 26))
    job = session.query(Job).one()

    _mock_sbs(monkeypatch, replace(detail, location_category="其他地区", location_detail="浙江省"))
    collect_sbs_jobs(session, object(), now=datetime(2026, 9, 27))
    assert job.intake_grade == "D"
    previous_version = job.version

    _mock_sbs(monkeypatch, detail)
    result = collect_sbs_jobs(session, object(), now=datetime(2026, 9, 28))

    assert result.updated_jobs == 1
    assert (job.intake_grade, job.intake_route, job.status, job.location_detail) == ("B", "普通待核验", "待核验", "上海市")
    assert job.version == previous_version + 1


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
