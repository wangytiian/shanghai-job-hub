from app.services.jobs import validate_publishable


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
