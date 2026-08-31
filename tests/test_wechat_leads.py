import pytest

from app.models import Source
from app.sources.catalog import ensure_official_source_catalog


ARTICLE_URL = "https://mp.weixin.qq.com/s/example-public-article"
ARTICLE_HTML = """
<html><head><meta property="og:title" content="2026年上海某区事业单位公开招聘公告" /></head>
<body><div id="js_content">上海某区事业单位现面向社会公开招聘工作人员。报名时间为2026年9月1日至9月8日，请以官方公告为准。</div></body></html>
"""
PROMOTION_HTML = """
<html><head><title>华智公考课程优惠</title></head>
<body><div id="js_content">2026公考培训课程开课，提供试听优惠与报班咨询服务，欢迎同学预约了解完整课程安排。</div></body></html>
"""


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, text):
        self.text = text

    def get(self, url, headers, timeout):
        return FakeResponse(self.text)


@pytest.fixture
def huazhi_source(session):
    ensure_official_source_catalog(session)
    return session.query(Source).filter_by(name="上海华智公考（公众号招聘线索）").one()


def test_import_public_wechat_article_creates_pending_verification_lead(session, huazhi_source):
    from app.services.wechat_leads import import_public_wechat_article

    job = import_public_wechat_article(session, huazhi_source, ARTICLE_URL, FakeClient(ARTICLE_HTML))

    assert job.status == "待核验"
    assert job.is_demo is False
    assert job.official_url == ""
    assert job.source_url == ARTICLE_URL
    assert "须补充官方原文" in job.risk_flags
    assert "事业单位" in job.evidence_text


def test_import_rejects_non_wechat_url(session, huazhi_source):
    from app.services.wechat_leads import import_public_wechat_article

    with pytest.raises(ValueError, match="公众号公开文章链接"):
        import_public_wechat_article(session, huazhi_source, "https://example.com/x", FakeClient(""))


def test_import_rejects_course_promotion_without_recruitment_signal(session, huazhi_source):
    from app.services.wechat_leads import import_public_wechat_article

    with pytest.raises(ValueError, match="推广内容"):
        import_public_wechat_article(session, huazhi_source, ARTICLE_URL, FakeClient(PROMOTION_HTML))


def test_import_deduplicates_same_public_article_url(session, huazhi_source):
    from app.services.wechat_leads import import_public_wechat_article

    first = import_public_wechat_article(session, huazhi_source, ARTICLE_URL, FakeClient(ARTICLE_HTML))
    second = import_public_wechat_article(session, huazhi_source, ARTICLE_URL, FakeClient(ARTICLE_HTML))

    assert second.id == first.id
    assert session.query(type(first)).count() == 1
