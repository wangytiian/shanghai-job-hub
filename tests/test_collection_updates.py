from datetime import datetime

from app.models import Job, Source
from app.services.real_collection import collect_shanghai_sasac


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
