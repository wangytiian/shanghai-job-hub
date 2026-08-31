from datetime import datetime

import httpx

from app.models import Source


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "unexpected status", request=httpx.Request("GET", "https://official.example/jobs"), response=httpx.Response(self.status_code)
            )


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _source(session):
    source = Source(
        name="连接检查测试来源",
        url="https://official.example/jobs",
        official_career_url="https://official.example/jobs",
        level="一级",
        source_type="企业官网",
        library_tier="B",
        is_enabled=False,
    )
    session.add(source)
    session.commit()
    return source


def test_connection_check_records_success_without_changing_collection_rights(session):
    from app.services.source_health import check_source_connection

    source = _source(session)
    client = FakeClient(response=FakeResponse(200))

    result = check_source_connection(source, client, datetime(2026, 8, 31, 12, 0))

    assert result.kind == "success"
    assert "官网连接正常" in source.last_monitor_summary
    assert source.library_tier == "B"
    assert source.is_enabled is False
    assert client.calls[0][1]["follow_redirects"] is True
    assert client.calls[0][1]["timeout"] == 15.0


def test_connection_check_converts_tls_failure_to_safe_message(session):
    from app.services.source_health import check_source_connection

    source = _source(session)
    client = FakeClient(error=httpx.ConnectError("certificate verify failed"))

    result = check_source_connection(source, client, datetime(2026, 8, 31, 12, 0))

    assert result.kind == "error"
    assert source.last_error_summary == "HTTPS 证书或连接校验失败"
    assert "官网连接异常" in source.last_monitor_summary
