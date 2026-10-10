import pytest

from app.services.jobs import validate_publishable
from app.services.review_intake import REVIEW_NOTE


def test_validation_reports_missing_official_link(demo_job):
    demo_job.official_url = ""

    errors = validate_publishable(demo_job)

    assert "缺少官方链接" in errors


def test_validation_blocks_unverified_placeholder_and_attachment_pending_job(demo_job):
    demo_job.quality_score = 60
    demo_job.risk_flags = "真实线索：尚未人工核验，不得对外发布"
    demo_job.location_detail = "以公告原文为准"
    demo_job.posting_scope = "attachment_pending"
    demo_job.attachment_status = "pending"

    errors = validate_publishable(demo_job)

    assert "质量分不足70" in errors
    assert "尚未人工核验" in "；".join(errors)
    assert "占位字段" in "；".join(errors)
    assert "附件尚未核验" in errors


def test_validation_allows_explicitly_unstated_deadline(demo_job):
    demo_job.deadline = "公告未明确统一截止时间"

    errors = validate_publishable(demo_job)

    assert "存在占位字段" not in errors


@pytest.mark.parametrize("grade", ["C", "D"])
def test_validation_blocks_c_or_d_grade_even_if_other_checks_are_unchanged(demo_job, grade):
    demo_job.intake_grade = "B"
    baseline_errors = validate_publishable(demo_job)

    demo_job.intake_grade = grade
    errors = validate_publishable(demo_job)

    assert errors == ["只有 A/B 分级岗位可以发布", *baseline_errors]


@pytest.mark.parametrize("grade", ["A", "B"])
def test_validation_keeps_existing_checks_for_a_and_b_grades(demo_job, grade):
    demo_job.intake_grade = "B"
    baseline_errors = validate_publishable(demo_job)

    demo_job.intake_grade = grade

    assert validate_publishable(demo_job) == baseline_errors


def test_audience_unconfirmed_b_cannot_be_published(demo_job):
    demo_job.intake_grade = "B"
    demo_job.intake_reason = REVIEW_NOTE

    assert "学生受众尚未人工确认" in validate_publishable(demo_job)
