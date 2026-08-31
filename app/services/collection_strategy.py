from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class CollectionPlan:
    season: str
    frequency_hours: int
    next_due_at: datetime | None
    is_due: bool


def _a_level_policy(month: int) -> tuple[str, int]:
    if month in (1, 2):
        return "政策与事业单位窗口", 12
    if month in (3, 4, 5):
        return "春招、补录与实习高峰", 8
    if month in (6, 7, 8):
        return "暑期实习、补招与初级岗位", 12
    if month in (9, 10, 11):
        return "秋招主高峰", 8
    return "秋招补录与提前批", 12


def build_collection_plan(
    source_level: str, now: datetime, last_success_at: datetime | None
) -> CollectionPlan:
    if source_level == "一级":
        season, frequency_hours = _a_level_policy(now.month)
    elif source_level == "二级":
        season, frequency_hours = "企业招聘常规监测", 24
    else:
        season, frequency_hours = "线索发现常规监测", 24
    next_due_at = (
        last_success_at + timedelta(hours=frequency_hours) if last_success_at else None
    )
    return CollectionPlan(
        season=season,
        frequency_hours=frequency_hours,
        next_due_at=next_due_at,
        is_due=next_due_at is None or now >= next_due_at,
    )
