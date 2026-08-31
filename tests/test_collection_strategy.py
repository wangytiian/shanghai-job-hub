from datetime import datetime

from app.services.collection_strategy import build_collection_plan


def test_a_level_source_uses_policy_window_frequency_in_january():
    now = datetime(2026, 1, 15, 9, 0)

    plan = build_collection_plan("一级", now, None)

    assert plan.season == "政策与事业单位窗口"
    assert plan.frequency_hours == 12
    assert plan.is_due is True
    assert plan.next_due_at is None


def test_a_level_source_uses_spring_peak_frequency_and_next_due_time():
    last_success = datetime(2026, 4, 10, 8, 30)
    now = datetime(2026, 4, 10, 12, 0)

    plan = build_collection_plan("一级", now, last_success)

    assert plan.season == "春招、补录与实习高峰"
    assert plan.frequency_hours == 8
    assert plan.next_due_at == datetime(2026, 4, 10, 16, 30)
    assert plan.is_due is False


def test_a_level_source_uses_summer_and_autumn_frequency():
    summer = build_collection_plan("一级", datetime(2026, 7, 1, 9, 0), None)
    autumn = build_collection_plan("一级", datetime(2026, 10, 1, 9, 0), None)

    assert (summer.season, summer.frequency_hours) == ("暑期实习、补招与初级岗位", 12)
    assert (autumn.season, autumn.frequency_hours) == ("秋招主高峰", 8)


def test_b_and_c_level_sources_are_checked_daily():
    now = datetime(2026, 10, 1, 9, 0)

    assert build_collection_plan("二级", now, None).frequency_hours == 24
    assert build_collection_plan("三级", now, None).frequency_hours == 24
