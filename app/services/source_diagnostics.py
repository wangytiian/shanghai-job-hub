"""Small, conservative source checks that distinguish reachability from collectability."""

from dataclasses import dataclass
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from app.models import Source, SourceDiagnostic


KNOWN_ADAPTERS = {"shanghai_sasac", "official_dated_list", "spdb_shanghai_jobs", "boc_announcements"}
RECRUITMENT_WORDS = ("招聘公告", "校园招聘", "社会招聘", "实习生招聘", "招聘信息", "职位空缺")


@dataclass(frozen=True)
class SourceDiagnosticResult:
    connection_status: str
    content_status: str
    adapter_status: str
    http_status: int | None
    final_url: str
    sample_count: int
    detail_success_count: int
    message: str
    error_code: str = ""


def _http_failure(exc: httpx.HTTPError) -> SourceDiagnosticResult:
    text = str(exc).lower()
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return SourceDiagnosticResult("http_error", "blocked" if status in {401, 403, 429, 521} else "not_checked", "missing", status, "", 0, 0, f"官网返回 HTTP {status}，未验证招聘内容。", f"http_{status}")
    if any(marker in text for marker in ("certificate", "ssl", "tls", "verify failed")):
        return SourceDiagnosticResult("tls_error", "not_checked", "missing", None, "", 0, 0, "HTTPS 证书或连接校验失败，未验证招聘内容。", "tls_error")
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout)):
        return SourceDiagnosticResult("timeout", "not_checked", "missing", None, "", 0, 0, "官网连接超时，未验证招聘内容。", "timeout")
    if isinstance(exc, httpx.TooManyRedirects):
        return SourceDiagnosticResult("connection_error", "not_checked", "missing", None, "", 0, 0, "官网重定向异常，未验证招聘内容。", "redirect_error")
    return SourceDiagnosticResult("connection_error", "not_checked", "missing", None, "", 0, 0, "官网连接失败，未验证招聘内容。", "connection_error")


def diagnose_source(source: Source, client, checked_at: datetime, *, depth: str = "connection") -> SourceDiagnosticResult:
    """Inspect exactly one public entry page; this never changes collection permissions."""
    url = source.official_career_url or source.url
    source.last_checked_at = checked_at
    adapter_status = "available" if source.adapter_key in KNOWN_ADAPTERS else "missing"
    try:
        response = client.get(url, follow_redirects=True, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        result = _http_failure(exc)
        source.last_error_summary = result.message
        source.last_monitor_summary = result.message
        return result

    final_url = str(getattr(response, "url", url))
    html = getattr(response, "text", "") or ""
    soup = BeautifulSoup(html, "html.parser")
    visible = soup.get_text(" ", strip=True)
    lowered = visible.lower()
    has_recruitment = any(word in visible for word in RECRUITMENT_WORDS)
    has_links = bool(soup.select("a[href]"))
    has_scripts = bool(soup.select("script[src]"))
    if has_recruitment and has_links:
        content_status, sample_count, message = "recruitment_list", 1, "已找到招聘列表信号；仍须完成专用适配和试采后才能自动抓取。"
    elif len(visible) < 80 and has_scripts:
        content_status, sample_count, message = "dynamic_or_unverified", 0, "官网可访问，但页面内容可能由动态接口加载，尚未验证招聘列表。"
    elif any(word in lowered for word in ("新闻", "媒体", "动态", "投资者")):
        content_status, sample_count, message = "wrong_entry", 0, "官网可访问，但当前入口不是招聘列表，尚不可自动采集。"
    elif has_recruitment:
        content_status, sample_count, message = "dynamic_or_unverified", 0, "页面出现招聘相关文字，但未发现可验证的招聘列表，尚不可自动采集。"
    else:
        content_status, sample_count, message = "wrong_entry", 0, "官网可访问，但当前入口不是招聘列表，尚不可自动采集。"
    result = SourceDiagnosticResult("ok", content_status, adapter_status, getattr(response, "status_code", 200), final_url, sample_count, 0, message)
    source.last_error_summary = ""
    source.last_monitor_summary = message
    return result


def save_diagnostic(session, source: Source, result: SourceDiagnosticResult, checked_at: datetime, *, depth: str = "connection") -> SourceDiagnostic:
    record = SourceDiagnostic(
        source_id=source.id,
        checked_at=checked_at,
        depth=depth,
        connection_status=result.connection_status,
        content_status=result.content_status,
        adapter_status=result.adapter_status,
        http_status=result.http_status,
        final_url=result.final_url[:500],
        sample_count=result.sample_count,
        detail_success_count=result.detail_success_count,
        error_code=result.error_code,
        message=result.message[:500],
    )
    session.add(record)
    return record
