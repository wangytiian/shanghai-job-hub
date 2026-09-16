from dataclasses import dataclass
from datetime import datetime

from app.models import Source
from app.services.source_diagnostics import diagnose_source


@dataclass(frozen=True)
class SourceHealthResult:
    kind: str
    message: str


def check_source_connection(
    source: Source, client, checked_at: datetime
) -> SourceHealthResult:
    result = diagnose_source(source, client, checked_at)
    if result.connection_status == "ok":
        source.last_monitor_summary = "官网连接正常，仍待专用适配，不参与每日采集"
        return SourceHealthResult("success", source.last_monitor_summary)
    if result.connection_status == "tls_error":
        source.last_error_summary = "HTTPS 证书或连接校验失败"
    elif result.connection_status == "timeout":
        source.last_error_summary = "官网连接超时"
    elif result.connection_status == "http_error":
        source.last_error_summary = f"官网返回 HTTP {result.http_status}"
    else:
        source.last_error_summary = "官网连接失败，请稍后重试"
    source.last_monitor_summary = f"官网连接异常：{source.last_error_summary}"
    return SourceHealthResult("error", source.last_monitor_summary)
