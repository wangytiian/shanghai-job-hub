from datetime import datetime

import httpx

from app.models import Source


class Response:
    def __init__(self, status_code: int, text: str, url: str = "https://official.example/page"):
        self.status_code = status_code
        self.text = text
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad status",
                request=httpx.Request("GET", self.url),
                response=httpx.Response(self.status_code),
            )


class Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _source(session, *, adapter_key="pending_validation"):
    source = Source(
        name="来源诊断测试",
        url="https://official.example/news",
        official_career_url="https://official.example/news",
        level="一级",
        source_type="企业官网",
        library_tier="B",
        adapter_key=adapter_key,
        is_enabled=False,
    )
    session.add(source)
    session.commit()
    return source


def test_news_page_is_not_reported_as_collectable_recruitment_source(session):
    from app.services.source_diagnostics import diagnose_source

    source = _source(session)
    result = diagnose_source(
        source,
        Client(Response(200, "<html><title>公司新闻</title><body><h1>媒体报道</h1><p>企业动态</p></body></html>")),
        datetime(2026, 9, 6, 10, 0),
    )

    assert result.connection_status == "ok"
    assert result.content_status == "wrong_entry"
    assert result.adapter_status == "missing"
    assert "不是招聘列表" in result.message
    assert source.is_enabled is False


def test_dynamic_shell_is_reported_as_unverified_without_enabling_collection(session):
    from app.services.source_diagnostics import diagnose_source

    source = _source(session)
    result = diagnose_source(
        source,
        Client(Response(200, "<html><body><div id='app'></div><script src='app.js'></script></body></html>")),
        datetime(2026, 9, 6, 10, 0),
    )

    assert result.content_status == "dynamic_or_unverified"
    assert "动态" in result.message
    assert source.is_enabled is False


def test_blocked_response_has_a_precise_diagnostic_status(session):
    from app.services.source_diagnostics import diagnose_source

    source = _source(session)
    result = diagnose_source(
        source,
        Client(Response(403, "forbidden")),
        datetime(2026, 9, 6, 10, 0),
    )

    assert result.connection_status == "http_error"
    assert result.content_status == "blocked"
    assert result.http_status == 403
    assert "HTTP 403" in result.message


def test_known_adapter_with_recruitment_list_is_still_not_auto_enabled(session):
    from app.services.source_diagnostics import diagnose_source

    source = _source(session, adapter_key="boc_announcements")
    result = diagnose_source(
        source,
        Client(Response(200, "<html><body><h1>招聘公告</h1><a href='detail.html'>2027年校园招聘公告</a></body></html>")),
        datetime(2026, 9, 6, 10, 0),
    )

    assert result.content_status == "recruitment_list"
    assert result.adapter_status == "available"
    assert source.is_enabled is False


def test_diagnostic_record_is_persisted_without_changing_collection_success_time(session):
    from app.models import SourceDiagnostic
    from app.services.source_diagnostics import diagnose_source, save_diagnostic

    source = _source(session)
    result = diagnose_source(
        source,
        Client(Response(200, "<html><body><h1>媒体动态</h1><p>企业新闻</p></body></html>")),
        datetime(2026, 9, 6, 10, 0),
    )
    save_diagnostic(session, source, result, datetime(2026, 9, 6, 10, 0))
    session.commit()

    record = session.query(SourceDiagnostic).filter_by(source_id=source.id).one()
    assert record.content_status == "wrong_entry"
    assert record.depth == "connection"
    assert source.last_success_at is None
