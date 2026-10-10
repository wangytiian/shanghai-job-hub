from datetime import datetime
from types import SimpleNamespace

from app.models import Job, Source
from app.services.real_collection import _save_detail, collect_shanghai_sasac
from app.services.review_intake import REVIEW_NOTE


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeClient:
    def get(self, url, **kwargs):
        if url.endswith("cqzp/"):
            return FakeResponse('<li>2026-06-08 <a href="/article.html">示例公告</a></li>')
        return FakeResponse("<h1>示例公告</h1><p>发布日期：2026-06-08</p><p>上海实习岗位。</p>")


def test_complete_b_role_needs_official_shanghai_and_application_evidence(session):
    source = Source(name="示例高校就业网", url="https://career.example.edu/jobs/list",
                    level="一级", source_type="高校")
    session.add(source)
    session.flush()
    body = "岗位职责：整理客户资料。任职要求：本科。官方投递邮箱：jobs@example.com"
    base = dict(identity_key="role-1", detail_url="https://career.example.edu/jobs/role-1",
                title="客户助理", employer_name="上海示例公司", evidence_text=body,
                location_category="明确上海", location_detail="上海市", official_url="",
                published_at="2026-10-01", deadline="2026-11-30")
    for identity, changes in (
        ("role-1", {}),
        ("role-2", {"location_category": "其他地区", "location_detail": "杭州市"}),
        ("role-3", {"evidence_text": body.replace("官方投递邮箱：jobs@example.com", "投递方式请看附件")}),
        ("role-4", {"detail_url": "https://other.example/jobs/role-4"}),
    ):
        _save_detail(session, source, SimpleNamespace(**(base | changes | {"identity_key": identity})),
                     datetime(2026, 10, 10))
    session.flush()
    jobs = session.query(Job).order_by(Job.id).all()
    assert [job.intake_grade for job in jobs] == ["B", "C", "C", "C"]
    assert jobs[0].intake_reason == REVIEW_NOTE


def test_public_notice_without_verified_shanghai_location_cannot_bypass_b_gate(session):
    class PublicNoticeClient(FakeClient):
        def get(self, url, **kwargs):
            if url.endswith("cqzp/"):
                return FakeResponse('<li>2026-10-01 <a href="/article.html">策划专员</a></li>')
            return FakeResponse(
                "<h1>策划专员</h1><p>岗位职责：负责活动策划。任职要求：本科。"
                "官方投递邮箱：jobs@example.com。报名截止：2026-11-30。</p>"
            )

    collect_shanghai_sasac(session, PublicNoticeClient(), limit=1, now=datetime(2026, 10, 10))
    job = session.query(Job).filter_by(is_demo=False).one()
    assert job.location_category == "原文未明确"
    assert job.intake_grade == "C"


def test_recollecting_unchanged_notice_only_updates_verification_time(session):
    client = FakeClient()
    collect_shanghai_sasac(session, client, limit=1, now=datetime(2026, 8, 14, 8, 0))
    job = session.query(Job).filter_by(is_demo=False).one()
    original_version = job.version

    result = collect_shanghai_sasac(session, client, limit=1, now=datetime(2026, 8, 14, 9, 0))
    refreshed_job = session.query(Job).filter_by(is_demo=False).one()

    assert result.created_jobs == 0
    assert result.unchanged_jobs == 1
    assert result.updated_jobs == 0
    assert refreshed_job.version == original_version
    assert refreshed_job.last_verified_at == datetime(2026, 8, 14, 9, 0)
    assert refreshed_job.lifecycle_status == "正常"


def test_changed_notice_marks_real_clue_as_updated_and_pending_verification(session):
    collect_shanghai_sasac(session, FakeClient(), limit=1, now=datetime(2026, 8, 14, 8, 0))
    original_job = session.query(Job).filter_by(is_demo=False).one()
    original_version = original_job.version
    original_row_version = original_job.row_version

    class ChangedClient(FakeClient):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if not url.endswith("cqzp/"):
                response.text += "<p>报名截止时间更新为 2026-08-20</p>"
            return response

    result = collect_shanghai_sasac(
        session, ChangedClient(), limit=1, now=datetime(2026, 8, 14, 10, 0)
    )
    refreshed_job = session.query(Job).filter_by(is_demo=False).one()

    assert result.updated_jobs == 1
    assert refreshed_job.id == original_job.id
    assert refreshed_job.version == original_version + 1
    assert refreshed_job.row_version == original_row_version + 1
    assert refreshed_job.lifecycle_status == "有更新"
    assert refreshed_job.status == "待核验"
    assert "原文内容发生变化" in refreshed_job.last_change_summary


def test_collection_skips_notice_with_explicitly_expired_registration_window(session):
    class ExpiredDateClient(FakeClient):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if not url.endswith("cqzp/"):
                response.text += "<p>报名时间：2026年6月8日起至2026年6月18日</p>"
            return response

    result = collect_shanghai_sasac(
        session, ExpiredDateClient(), limit=1, now=datetime(2026, 9, 2, 9, 0)
    )

    assert result.created_jobs == 0
    assert session.query(Job).filter_by(is_demo=False).count() == 0


def test_existing_notice_newly_expired_retracts_approval_and_invalidates_stale_form(session):
    collect_shanghai_sasac(session, FakeClient(), limit=1, now=datetime(2026, 8, 14, 8, 0))
    job = session.query(Job).filter_by(is_demo=False).one()
    old_version, old_row_version = job.version, job.row_version
    job.status = "可发布"
    job.verification_checks = '{"source_checked": true}'
    job.verification_version = 1
    session.commit()

    class ExpiredCorrectionClient(FakeClient):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if not url.endswith("cqzp/"):
                response.text += "<p>报名截止时间：2026-08-13</p>"
            return response

    collect_shanghai_sasac(session, ExpiredCorrectionClient(), limit=1, now=datetime(2026, 8, 14, 10, 0))

    assert (job.status, job.lifecycle_status, job.deadline) == ("已截止", "已截止", "2026-08-13")
    assert (job.version, job.row_version) == (old_version + 1, old_row_version + 1)
    assert (job.intake_grade, job.verification_checks, job.verification_version) == ("D", "{}", 0)


def test_repeated_expired_notice_preserves_manual_rejection_without_version_churn(session):
    collect_shanghai_sasac(session, FakeClient(), limit=1, now=datetime(2026, 8, 14, 8, 0))
    job = session.query(Job).filter_by(is_demo=False).one()
    job.status = "淘汰"
    session.commit()

    class ExpiredCorrectionClient(FakeClient):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if not url.endswith("cqzp/"):
                response.text += "<p>报名截止时间：2026-08-13</p>"
            return response

    collect_shanghai_sasac(session, ExpiredCorrectionClient(), limit=1, now=datetime(2026, 8, 14, 10, 0))
    version_after_first, row_version_after_first = job.version, job.row_version
    collect_shanghai_sasac(session, ExpiredCorrectionClient(), limit=1, now=datetime(2026, 8, 14, 11, 0))

    assert job.status == "淘汰"
    assert (job.version, job.row_version) == (version_after_first, row_version_after_first)


def test_three_whole_source_failures_pause_source_and_success_resets_counter(session):
    class FailingClient:
        def get(self, url, **kwargs):
            raise RuntimeError("temporary source network failure")

    for attempt in range(3):
        try:
            collect_shanghai_sasac(
                session, FailingClient(), limit=1, now=datetime(2026, 8, 14, 8 + attempt, 0)
            )
        except RuntimeError:
            pass

    source = session.query(Source).one()
    assert source.consecutive_failure_count == 3
    assert source.status == "暂停"
    assert source.pause_reason

    collect_shanghai_sasac(session, FakeClient(), limit=1, now=datetime(2026, 8, 14, 12, 0))
    source = session.query(Source).one()

    assert source.consecutive_failure_count == 0
    assert source.status == "正常"
    assert source.last_success_at == datetime(2026, 8, 14, 12, 0)
