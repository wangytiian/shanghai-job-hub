from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest

from app.models import Job, Source, SourceDiagnostic, TaskRun
from app.services import real_collection
from app.services.source_diagnostics import KNOWN_ADAPTERS
from app.services.source_library import can_auto_collect
from app.sources.campus_json import CampusAnnouncement, CampusJobDetail
from app.sources.catalog import ensure_official_source_catalog


NOW = datetime(2026, 9, 27, 10)
NAME = "上海第二工业大学就业网"


def _detail(key="101:1", *, student=True, location="上海市杨浦区", application=True):
    return CampusJobDetail(
        title="物流运营助理",
        published_at="2026-09-10",
        detail_url="https://career.sspu.edu.cn/career/news/view/zpgg/101",
        evidence_text=(
            "岗位职责：整理物流业务资料和订单，协助客户沟通。\n"
            "任职要求：本科及以上学历，国际贸易或物流管理专业。\n"
            + ("面向2027届应届生。\n" if student else "")
            + f"工作地点：{location}\n"
            + ("官方投递邮箱：campus@example.com" if application else "")
        ),
        identity_key=key,
        announcement_title="示例物流公司招聘公告",
        employer_name="示例物流有限公司",
        location_category="明确上海" if "上海" in location else "其他地区",
        location_detail=location,
        official_url="",
    )


def _fake_fetch(monkeypatch, details):
    announcement = CampusAnnouncement("101", "示例物流公司招聘公告", "2026-09-10", _detail().detail_url)
    monkeypatch.setattr(real_collection, "fetch_sspu_announcements", lambda *a, **kw: [announcement], raising=False)
    monkeypatch.setattr(real_collection, "fetch_sspu_details", lambda *a, **kw: details, raising=False)


def _collect(session, monkeypatch, details, **kwargs):
    _fake_fetch(monkeypatch, details)
    collector = getattr(real_collection, "collect_sspu_jobs", None)
    assert callable(collector), "SSPU collector must be implemented"
    return collector(session, object(), now=NOW, **kwargs)


def test_sspu_is_a_enabled_and_registered_adapter(session):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(source_key="sspu-news").first()
    assert source is not None
    assert source.name == NAME
    assert source.adapter_key == "sspu_news"
    assert source.library_tier == "A" and can_auto_collect(source)
    assert source.validation_state == "用户批准A类（持续观察）"
    assert "sspu_news" in KNOWN_ADAPTERS


def test_sspu_catalog_preserves_operator_pause_and_disable(session):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(source_key="sspu-news").first()
    assert source is not None
    source.is_enabled = False
    source.status = "暂停"
    source.pause_reason = "人工停用"
    session.commit()
    ensure_official_source_catalog(session)
    assert source.is_enabled is False and source.status == "暂停"
    assert source.pause_reason == "人工停用"


def test_sspu_saves_only_allowed_details_and_keeps_audience_unclear_as_c(session, monkeypatch):
    result = _collect(session, monkeypatch, [
        _detail(),
        replace(_detail("101:2", student=False), title="跨境业务助理"),
        _detail("101:3", location="青岛胶州"),
        _detail("101:4", application=False),
    ])
    jobs = session.query(Job).order_by(Job.id).all()
    assert result.created_jobs == 2 and result.failed_jobs == 0
    assert [job.intake_grade for job in jobs] == ["A", "C"]
    assert all(job.status == "待核验" and not job.is_demo for job in jobs)
    assert jobs[0].announcement_title == "示例物流公司招聘公告"
    assert jobs[0].published_at == datetime(2026, 9, 10)
    assert jobs[0].deadline == "原文待人工确认"
    assert "TARGET_AUDIENCE_UNCLEAR" in jobs[1].evidence_note
    summary = session.query(Source).filter_by(name=NAME).one().last_monitor_summary
    assert "规则跳过 2" in summary and "请求或处理失败 0" in summary
    assert "LOCATION_NOT_SHANGHAI" in summary and "APPLICATION_MISSING" in summary
    diagnostic = session.query(SourceDiagnostic).one()
    assert diagnostic.content_status == "detail_verified"
    assert diagnostic.sample_count == 1 and diagnostic.detail_success_count == 1


def test_sspu_repeat_is_idempotent_and_does_not_touch_created_at(session, monkeypatch):
    assert _collect(session, monkeypatch, [_detail()]).created_jobs == 1
    job = session.query(Job).one()
    created_at = job.created_at
    second = _collect(session, monkeypatch, [_detail()])
    assert second.created_jobs == 0 and second.updated_jobs == 0 and second.unchanged_jobs == 1
    assert session.query(Job).count() == 1 and job.created_at == created_at


def test_sspu_updated_facts_invalidate_approval_and_refresh_display_fields(session, monkeypatch):
    _collect(session, monkeypatch, [_detail()])
    job = session.query(Job).one()
    job.status = "可发布"
    job.verification_checks = '{"location": true}'
    job.verification_version = job.version
    session.commit()
    changed = replace(_detail(location="上海市浦东新区"), title="物流业务助理")
    result = _collect(session, monkeypatch, [changed])
    assert result.updated_jobs == 1 and result.created_jobs == 0
    assert job.location_detail == "上海市浦东新区" and job.job_title == "物流业务助理"
    assert job.status == "待核验" and job.verification_version == 0 and job.version == 2


def test_sspu_never_calls_configured_paid_ai(session, monkeypatch):
    def unexpected(prompt):
        pytest.fail("SSPU collection must use deterministic screening without paid AI")
    result = _collect(session, monkeypatch, [_detail()], intake_ai_complete=unexpected)
    assert result.created_jobs == 1


def test_sspu_metadata_only_change_invalidates_approval_and_then_is_stable(session, monkeypatch):
    _collect(session, monkeypatch, [_detail()])
    job = session.query(Job).one()
    job.status = "可发布"
    job.verification_version = job.version
    session.commit()
    changed = replace(_detail(), title="关务业务助理", employer_name="另一物流有限公司")
    result = _collect(session, monkeypatch, [changed])
    assert result.updated_jobs == 1
    assert job.job_title == "关务业务助理" and job.employer_name == "另一物流有限公司"
    assert job.status == "待核验" and job.verification_version == 0 and job.version == 2
    assert _collect(session, monkeypatch, [changed]).unchanged_jobs == 1


@pytest.mark.parametrize("extra", [
    "有相关实习经历优先", "岗位职责：参与校园招聘，整理应届毕业生应聘材料。",
    "岗位职责：协助实习生完成工作。",
    "任职要求：有实习生管理经验优先。",
])
def test_sspu_experience_or_hr_duties_do_not_prove_student_eligibility(session, monkeypatch, extra):
    detail = _detail(student=False)
    _collect(session, monkeypatch, [replace(detail, evidence_text=detail.evidence_text + "\n" + extra)])
    assert session.query(Job).one().intake_grade == "C"


@pytest.mark.parametrize("text", ["仅限上海第二工业大学学生", "仅面向二工大毕业生", "只招本校学生", "任职要求：硕士及以上学历", "8k~12k/上海/硕士", "学历要求：博士", "学历要求：硕士，专业不限。面向2027届应届生。"])
def test_sspu_school_exclusive_or_postgraduate_only_not_admitted(session, monkeypatch, text):
    detail = _detail()
    result = _collect(session, monkeypatch, [replace(detail, evidence_text=detail.evidence_text + "\n" + text)])
    assert result.created_jobs == 0 and result.failed_jobs == 0
    assert session.query(Job).count() == 0


def test_sspu_expiry_of_existing_job_blocks_previous_publication(session, monkeypatch):
    _collect(session, monkeypatch, [_detail()])
    job = session.query(Job).one()
    job.status = "可发布"
    session.commit()
    expired = replace(_detail(), evidence_text=_detail().evidence_text + "\n报名截止：2026-09-20。")
    _collect(session, monkeypatch, [expired])
    assert job.status == "已截止" and job.lifecycle_status == "已截止"


def test_sspu_changed_to_non_shanghai_invalidates_existing_record(session, monkeypatch):
    _collect(session, monkeypatch, [_detail()])
    job = session.query(Job).one()
    job.status = "可发布"
    job.verification_version = job.version
    session.commit()
    _collect(session, monkeypatch, [_detail(location="北京市")])
    assert job.status == "待核验" and job.intake_grade == "D"
    assert job.verification_version == 0
    assert job.location_detail == "北京市"


def test_sspu_all_detail_failures_are_not_successful_empty_results(session, monkeypatch):
    _fake_fetch(monkeypatch, [])
    def fail(*a, **kw):
        raise httpx.ReadTimeout("request timed out")
    monkeypatch.setattr(real_collection, "fetch_sspu_details", fail)
    collector = getattr(real_collection, "collect_sspu_jobs", None)
    assert callable(collector)
    for _ in range(3):
        with pytest.raises(ValueError, match="全部详情"):
            collector(session, object(), now=NOW)
    source = session.query(Source).filter_by(name=NAME).one()
    assert source.status == "暂停" and source.consecutive_failure_count == 3
    assert source.last_success_at is None
    assert session.query(TaskRun).order_by(TaskRun.id.desc()).first().status == "失败"


def test_sspu_empty_list_is_failure_and_disabled_collector_does_not_request(session, monkeypatch):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(name=NAME).first()
    assert source is not None
    collector = getattr(real_collection, "collect_sspu_jobs", None)
    assert callable(collector)
    monkeypatch.setattr(real_collection, "fetch_sspu_announcements", lambda *a, **k: [], raising=False)
    with pytest.raises(ValueError, match="列表为空"):
        collector(session, object(), now=NOW)
    source.is_enabled = False
    session.commit()
    def unexpected(*a, **k):
        pytest.fail("disabled source must not access the network")
    monkeypatch.setattr(real_collection, "fetch_sspu_announcements", unexpected)
    with pytest.raises(ValueError, match="未启用|暂停"):
        collector(session, object(), now=NOW)


def test_sspu_due_scheduler_dispatches_and_respects_next_cycle(session, monkeypatch):
    ensure_official_source_catalog(session)
    for source in session.query(Source).all():
        source.is_enabled = source.source_key == "sspu-news"
    session.commit()
    _fake_fetch(monkeypatch, [_detail()])
    result = real_collection.collect_due_sources(session, object(), now=NOW)
    assert result.attempted_sources == 1 and result.created_jobs == 1
    source = session.query(Source).filter_by(name=NAME).one()
    assert source.next_due_at == NOW + timedelta(hours=8)
    second = real_collection.collect_due_sources(session, object(), now=NOW + timedelta(hours=1))
    assert second.attempted_sources == 0 and second.created_jobs == 0
