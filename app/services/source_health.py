from dataclasses import dataclass
from datetime import datetime

import httpx

from app.models import Source


@dataclass(frozen=True)
class SourceHealthResult:
    kind: str
    message: str


def _safe_error_message(exc: httpx.HTTPError) -> str:
    text = str(exc).lower()
    if any(marker in text for marker in ("certificate", "ssl", "tls", "verify failed")):
        return "HTTPS 证书或连接校验失败"
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout)):
        return "官网连接超时"
    if isinstance(exc, httpx.TooManyRedirects):
        return "官网重定向异常"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"官网返回 HTTP {exc.response.status_code}"
    return "官网连接失败，请稍后重试"


def check_source_connection(
    source: Source, client, checked_at: datetime
) -> SourceHealthResult:
    url = source.official_career_url or source.url
    source.last_checked_at = checked_at
    try:
        response = client.get(url, follow_redirects=True, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        error_message = _safe_error_message(exc)
        source.last_error_summary = error_message
        source.last_monitor_summary = f"官网连接异常：{error_message}"
        return SourceHealthResult("error", source.last_monitor_summary)

    source.last_error_summary = ""
    source.last_monitor_summary = "官网连接正常，仍待专用适配，不参与每日采集"
    return SourceHealthResult("success", source.last_monitor_summary)
