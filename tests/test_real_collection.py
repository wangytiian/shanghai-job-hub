from app.models import Job
from app.services.real_collection import collect_shanghai_sasac


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
