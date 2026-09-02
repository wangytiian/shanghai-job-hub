from datetime import date

from app.services.deadline_policy import (
    expire_known_deadline_jobs,
    extract_application_deadline,
    is_application_expired,
)
from app.services.jobs import validate_publishable


def test_extracts_end_date_from_chinese_registration_range():
    evidence = "报名时间：2026年6月8日起至2026年6月18日"

    assert extract_application_deadline(evidence) == date(2026, 6, 18)


def test_explicit_past_registration_range_is_expired():
    evidence = "报名时间：2026年6月8日起至2026年6月18日"

    assert is_application_expired(evidence, today=date(2026, 9, 2)) is True


def test_publishable_validation_blocks_a_past_iso_deadline(demo_job):
    demo_job.deadline = "2026-06-18"

    errors = validate_publishable(demo_job, today=date(2026, 9, 2))

    assert "报名已截止" in errors


def test_expiry_sweep_moves_pending_real_job_out_of_the_review_queue(session, demo_job):
    demo_job.is_demo = False
    demo_job.status = "待核验"
    demo_job.deadline = "原文待人工确认"
    demo_job.evidence_text = "报名时间：2026年6月8日起至2026年6月18日"
    session.commit()

    changed = expire_known_deadline_jobs(session, today=date(2026, 9, 2))

    assert changed == 1
    assert demo_job.status == "已截止"
    assert demo_job.lifecycle_status == "已截止"
