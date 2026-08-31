import pytest

from app.models import ReviewLog
from app.services.reviews import review_job


def test_job_without_official_link_cannot_be_approved(session, demo_job):
    demo_job.official_url = ""
    session.commit()

    with pytest.raises(ValueError, match="官方链接"):
        review_job(session, demo_job.id, "approve", "信息已核验", "管理员")


def test_approval_moves_pending_review_to_publishable_and_logs_action(session, pending_review_job):
    job = review_job(session, pending_review_job.id, "approve", "信息已核验", "管理员")

    assert job.status == "可发布"
    assert session.query(ReviewLog).filter_by(job_id=job.id, action="通过").count() == 1


def test_approval_requires_verification_note(session, pending_review_job):
    pending_review_job.quality_score = 80
    pending_review_job.risk_flags = "已人工核验，无未解决风险"
    session.commit()

    with pytest.raises(ValueError, match="核验备注"):
        review_job(session, pending_review_job.id, "approve", "", "管理员")
