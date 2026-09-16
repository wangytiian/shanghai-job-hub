from datetime import datetime

import pytest

from app.models import Job, Source
from app.services.real_collection import collect_due_sources, collect_shanghai_sasac


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeClient:
    def get(self, url, **kwargs):
        if url.endswith("cqzp/"):
            return FakeResponse(
                '<li>2026-06-08 <a href="/article.html">上海示例国企暑期实习启动</a></li>'
            )
        return FakeResponse(
            '<h1>上海示例国企暑期实习启动</h1><p>发布日期：2026-06-08</p>'
            '<p>面向高校在读学生提供上海地区暑期实习岗位。</p>'
        )


def test_imported_real_clue_is_pending_verification_without_official_application_link(session):
    result = collect_shanghai_sasac(session, FakeClient(), limit=1)
    job = session.query(Job).filter_by(is_demo=False).one()

    assert result.created_jobs == 1
    assert job.status == "待核验"
    assert job.official_url == ""
    assert "尚未人工核验" in job.risk_flags
    assert "高校在读学生" in job.evidence_text
    assert job.published_at == datetime(2026, 6, 8)
    assert job.location_category == "原文未明确"
    assert job.location_detail == ""
    assert job.evidence_status == "正文已提取"


def test_daily_collection_does_not_request_a_paused_source_even_when_forced(session):
    from app.sources.catalog import ensure_official_source_catalog

    ensure_official_source_catalog(session)
    for source in session.query(Source).all():
        source.is_enabled = False
    paused = session.query(Source).filter_by(name="国务院国资委人事招聘").one()
    paused.is_enabled = True
    paused.status = "暂停"
    session.commit()

    class NoRequestClient:
        def get(self, *args, **kwargs):
            raise AssertionError("暂停来源不应发起抓取请求")

    result = collect_due_sources(session, NoRequestClient(), force=True)

    assert result.attempted_sources == 0
    assert result.skipped_sources == 1


def test_boc_collection_uses_the_boc_source_and_keeps_national_location_unverified(monkeypatch, session):
    from app.services.real_collection import collect_boc_announcements
    from app.sources.official_list import OfficialDetail, OfficialListing

    listing = OfficialListing("中国银行股份有限公司2027年全球校园招聘公告", "2026-09-03", "https://www.boc.cn/aboutboc/bi4/a.html")
    detail = OfficialDetail(
        listing.title,
        listing.published_at,
        listing.detail_url,
        "中国银行2027年校园招聘，面向应届毕业生。工作地点以岗位说明为准。",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_boc_listings", lambda client, limit: [listing])
    monkeypatch.setattr("app.services.real_collection.fetch_boc_detail", lambda client, item: detail)

    result = collect_boc_announcements(session, object(), now=datetime(2026, 9, 6, 10, 0))

    job = session.query(Job).filter_by(is_demo=False).one()
    assert result.created_jobs == 1
    assert job.source_url == listing.detail_url
    assert job.location_category == "原文未明确"
    assert job.status == "待核验"


def test_collect_ncss_saves_active_shanghai_detail_as_pending_verification(monkeypatch, session):
    from app.services.real_collection import collect_ncss_shanghai_jobs
    from app.sources.ncss import NcssDetail, NcssListing

    listing = NcssListing(
        "job-a", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/job-a/detail.html",
    )
    detail = NcssDetail(
        "AI 产品运营实习生", "2026-09-06", listing.detail_url,
        "岗位职责：参与 AI 产品用户研究、内容运营与数据分析。工作地点：上海市徐汇区。官方投递入口：https://apply.example.com/job-a",
        "job-a", "上海示例科技有限公司", "上海", "上海市徐汇区",
        official_url="https://apply.example.com/job-a",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda client, pages: [listing])
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_detail", lambda client, item: detail)

    result = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))

    job = session.query(Job).filter_by(is_demo=False).one()
    assert result.created_jobs == 1
    assert job.status == "待核验"
    assert job.location_category == "上海"
    assert job.fingerprint == "国家大学生就业服务平台上海岗位|job-a"


def test_collect_ncss_skips_offline_detail_and_deduplicates_active_detail(monkeypatch, session):
    from app.services.real_collection import collect_ncss_shanghai_jobs
    from app.sources.ncss import NcssDetail, NcssListing

    active = NcssListing(
        "job-a", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/job-a/detail.html",
    )
    offline = NcssListing(
        "job-offline", "已下线岗位", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/job-offline/detail.html",
    )
    detail = NcssDetail(
        active.title, active.published_at, active.detail_url,
        "岗位职责：参与 AI 产品用户研究、内容运营与数据分析。工作地点：上海市徐汇区。官方投递入口：https://apply.example.com/job-a",
        active.job_id, active.employer_name, "上海", "上海市徐汇区",
        official_url="https://apply.example.com/job-a",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda client, pages: [active, offline])
    monkeypatch.setattr(
        "app.services.real_collection.fetch_ncss_detail",
        lambda client, item: detail if item.job_id == active.job_id else (_ for _ in ()).throw(ValueError("职位已下线")),
    )

    first = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))
    second = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 5))

    assert first.created_jobs == 1
    assert first.failed_jobs == 1
    assert second.created_jobs == 0
    assert second.unchanged_jobs == 1
    assert session.query(Job).filter_by(is_demo=False).count() == 1


def test_collect_ncss_updates_stable_job_when_source_publication_date_changes(monkeypatch, session):
    from app.services.real_collection import collect_ncss_shanghai_jobs
    from app.sources.ncss import NcssDetail, NcssListing

    first = NcssListing(
        "stable-job", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/stable-job/detail.html",
    )
    corrected = NcssListing(
        "stable-job", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-07", "https://www.ncss.cn/student/jobs/stable-job/detail.html",
    )

    def detail_for(item):
        return NcssDetail(
            item.title, item.published_at, item.detail_url,
            f"岗位职责：参与 AI 产品用户研究。发布日期：{item.published_at}。工作地点：上海市徐汇区。官方投递入口：https://apply.example.com/stable-job",
            item.job_id, item.employer_name, "上海", "上海市徐汇区",
            official_url="https://apply.example.com/stable-job",
        )

    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda client, pages: [first])
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_detail", lambda client, item: detail_for(item))
    collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))

    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda client, pages: [corrected])
    result = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 7, 12, 0))

    assert result.created_jobs == 0
    assert result.updated_jobs == 1
    assert session.query(Job).filter_by(is_demo=False).count() == 1


def test_collect_ncss_marks_source_failed_when_every_detail_is_unreadable(monkeypatch, session):
    from app.services.real_collection import collect_ncss_shanghai_jobs
    from app.sources.ncss import NcssListing

    listing = NcssListing(
        "unreadable-job", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/unreadable-job/detail.html",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda client, pages: [listing])
    monkeypatch.setattr(
        "app.services.real_collection.fetch_ncss_detail",
        lambda client, item: (_ for _ in ()).throw(ValueError("岗位正文不足")),
    )

    with pytest.raises(ValueError, match="全部详情解析失败"):
        collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))

    source = session.query(Source).filter_by(name="国家大学生就业服务平台上海岗位").one()
    assert source.status == "异常"
    assert source.consecutive_failure_count == 1


def test_ncss_formal_collection_rejects_detail_without_explicit_application_evidence(monkeypatch, session):
    from app.services.real_collection import collect_ncss_shanghai_jobs
    from app.sources.ncss import NcssDetail, NcssListing

    listing = NcssListing(
        "job-no-apply", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心",
        "2026-09-06", "https://www.ncss.cn/student/jobs/job-no-apply/detail.html",
    )
    detail = NcssDetail(
        listing.title, listing.published_at, listing.detail_url,
        "岗位职责：参与产品运营。任职要求：面向在校生。工作地点：上海市徐汇区。",
        listing.job_id, listing.employer_name, "上海", "上海市徐汇区",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda *args, **kwargs: [listing])
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_detail", lambda *args, **kwargs: detail)

    result = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))

    assert result.created_jobs == 0
    assert result.failed_jobs == 1
    assert session.query(Job).filter_by(is_demo=False).count() == 0


def test_collect_sjtu_internships_keeps_only_explicit_shanghai_positions_pending_verification(monkeypatch, session):
    from app.services.real_collection import collect_sjtu_internship_jobs
    from app.sources.campus_json import CampusJobDetail

    shanghai = CampusJobDetail(
        "产品运营实习生", "2026-09-07", "https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx",
        "岗位职责：协助产品运营。任职要求：面向在校学生开放。工作地点：上海市徐汇区。官方报名入口：https://jobs.example.com/apply",
        "sjtu-1:role-1", "校园招聘公告",
        "上海示例科技有限公司", "明确上海", "上海市徐汇区", "https://jobs.example.com/apply",
    )
    beijing = CampusJobDetail(
        "数据分析实习生", "2026-09-07", "https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx",
        "工作地点：北京市朝阳区。", "sjtu-1:role-2", "校园招聘公告",
        "北京示例科技有限公司", "其他地区", "北京市朝阳区", "https://jobs.example.com/apply",
    )
    monkeypatch.setattr("app.services.real_collection.fetch_campus_announcements", lambda client, source, pages: [object()])
    monkeypatch.setattr("app.services.real_collection.fetch_campus_details", lambda client, source, item: [shanghai, beijing])

    result = collect_sjtu_internship_jobs(session, object(), now=datetime(2026, 9, 7, 12, 0))

    job = session.query(Job).filter_by(is_demo=False).one()
    source = session.query(Source).filter_by(name="上海交通大学就业网（待专用适配）").one()
    assert result.created_jobs == 1
    assert job.fingerprint == "上海交通大学就业网（待专用适配）|sjtu-1:role-1"
    assert job.location_category == "明确上海"
    assert source.is_enabled is False
    assert "非上海 1 条" in source.last_monitor_summary


def test_campus_formal_collection_uses_shared_policy_and_rejects_school_listing_as_application(monkeypatch, session):
    from app.services.real_collection import collect_sjtu_internship_jobs
    from app.sources.campus_json import CampusJobDetail

    school_page = "https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx"
    detail = CampusJobDetail(
        "产品实习生", "2026-09-07", school_page,
        f"岗位职责：协助产品工作。任职要求：在校生。工作地点：上海。官方报名入口：{school_page}",
        "sjtu-2:role-1", "校园招聘公告", "上海示例科技有限公司", "明确上海", "上海市",
        school_page,
    )
    monkeypatch.setattr("app.services.real_collection.fetch_campus_announcements", lambda *args, **kwargs: [object()])
    monkeypatch.setattr("app.services.real_collection.fetch_campus_details", lambda *args, **kwargs: [detail])

    result = collect_sjtu_internship_jobs(session, object(), now=datetime(2026, 9, 7, 12, 0))

    assert result.created_jobs == 0
    assert session.query(Job).filter_by(is_demo=False).count() == 0
