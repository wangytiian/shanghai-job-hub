import pytest

from app.sources.campus_json import CampusAnnouncement, CampusJobDetail, SJTU_INTERNSHIP_SOURCE


def test_run_campus_trial_returns_only_explicit_shanghai_candidates_without_writing_a_database(monkeypatch):
    from app.services.campus_trial import run_campus_trial

    announcements = [
        CampusAnnouncement("notice-1", "校园招聘公告", "2026-09-07", "https://example.com/list"),
        CampusAnnouncement("notice-2", "第二则公告", "2026-09-06", "https://example.com/list"),
    ]
    shanghai = CampusJobDetail(
        "产品运营实习生", "2026-09-07", "https://example.com/list", "岗位职责：协助运营。任职要求：在校生。工作地点：上海市徐汇区。官方报名入口：https://jobs.example.com/apply",
        "notice-1:role-1", "校园招聘公告", "上海示例科技有限公司", "明确上海", "上海市徐汇区",
        "https://jobs.example.com/apply",
    )
    non_shanghai = CampusJobDetail(
        "数据分析实习生", "2026-09-07", "https://example.com/list", "工作地点：北京市朝阳区。",
        "notice-1:role-2", "校园招聘公告", "北京示例科技有限公司", "其他地区", "北京市朝阳区",
        "https://jobs.example.com/apply",
    )
    shanghai_without_apply = CampusJobDetail(
        "信息收集员", "2026-09-07", "https://example.com/list", "工作地点：上海市长宁区。",
        "notice-1:role-3", "校园招聘公告", "上海示例科技有限公司", "明确上海", "上海市长宁区", "",
    )
    monkeypatch.setattr("app.services.campus_trial.fetch_campus_announcements", lambda client, source, pages, **kwargs: announcements)
    monkeypatch.setattr(
        "app.services.campus_trial.fetch_campus_details",
        lambda client, source, announcement, **kwargs: [shanghai, non_shanghai, shanghai_without_apply]
        if announcement.parent_id == "notice-1"
        else (_ for _ in ()).throw(ValueError("岗位正文不足")),
    )
    report = run_campus_trial(object(), SJTU_INTERNSHIP_SOURCE, pages=1, list_limit=2, detail_limit=2)

    assert report.announcement_count == 2
    assert report.detail_success_count == 1
    assert report.detail_failure_count == 1
    assert report.non_shanghai_count == 1
    assert report.missing_application_count == 1
    assert report.candidate_count == 1
    assert report.candidates[0].identity_key == "notice-1:role-1"
    assert report.candidates[0].official_url == "https://jobs.example.com/apply"
    assert len(report.details) == 3
    assert report.failures[0].announcement_id == "notice-2"


def test_campus_trial_stops_immediately_on_rate_limit(monkeypatch):
    from app.services.campus_trial import CampusTrialTerminalError, run_campus_trial

    announcements = [
        CampusAnnouncement("notice-1", "第一则", "2026-09-07", "https://example.com/1"),
        CampusAnnouncement("notice-2", "第二则", "2026-09-07", "https://example.com/2"),
    ]

    class RateLimited(Exception):
        response = type("Response", (), {"status_code": 429})()

    calls = []
    monkeypatch.setattr("app.services.campus_trial.fetch_campus_announcements", lambda *args, **kwargs: announcements)
    monkeypatch.setattr(
        "app.services.campus_trial.fetch_campus_details",
        lambda *args, **kwargs: calls.append(True) or (_ for _ in ()).throw(RateLimited()),
    )

    with pytest.raises(CampusTrialTerminalError) as caught:
        run_campus_trial(object(), SJTU_INTERNSHIP_SOURCE, list_limit=2, detail_limit=2)

    assert isinstance(caught.value.__cause__, RateLimited)
    assert calls == [True]
