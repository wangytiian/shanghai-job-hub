from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.models import Source


@dataclass(frozen=True)
class SourceLibrarySummary:
    catalog_sources: int
    demo_sources: int
    schedulable_sources: int


def summarize_source_library(sources: Iterable[Source], catalog) -> SourceLibrarySummary:
    """Report catalog, demo, and currently runnable source counts separately."""
    source_list = list(sources)
    catalog_names = {definition.name for definition in catalog}
    return SourceLibrarySummary(
        catalog_sources=sum(source.name in catalog_names for source in source_list),
        demo_sources=sum(source.name.endswith("（演示）") for source in source_list),
        schedulable_sources=sum(
            source.is_enabled and source.status != "暂停" for source in source_list
        ),
    )


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
