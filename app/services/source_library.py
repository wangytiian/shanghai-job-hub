from datetime import datetime

from app.models import Source


def can_auto_collect(source: Source) -> bool:
    """Only fully verified A-tier sources may enter the job collection task."""
    return (
        source.library_tier == "A"
        and source.is_enabled
        and source.adaptation_status == "已自动采集"
    )


def monitoring_message(source: Source) -> str:
    if source.library_tier == "A":
        return "已验证来源：采集结果仍须人工核验。"
    if source.library_tier == "B":
        return "待专用适配：不参与每日采集。"
    if source.library_tier == "C":
        return "重点监控：仅记录官网变化，不抓取岗位。"
    return "观察库：仅保留官方入口与招聘季信息，不抓取岗位。"


def record_monitor_check(source: Source, summary: str, checked_at: datetime) -> None:
    source.last_checked_at = checked_at
    source.last_monitor_summary = (summary or "未发现可确认的变化")[:300]
