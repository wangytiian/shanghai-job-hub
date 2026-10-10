import pytest

from app.models import ReviewLog
from app.auth.service import create_user
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


def test_review_log_can_link_to_the_authenticated_actor(session, pending_review_job):
    user = create_user(session, "reviewer", "A secure password", "审核同学", can_review=True)

    job = review_job(session, pending_review_job.id, "approve", "信息已核验", user.display_name, actor_user_id=user.id)

    log = session.query(ReviewLog).filter_by(job_id=job.id, action="通过").one()
    assert log.actor_user_id == user.id


def test_approval_requires_verification_note(session, pending_review_job):
    pending_review_job.quality_score = 80
    pending_review_job.risk_flags = "已人工核验，无未解决风险"
    session.commit()

    with pytest.raises(ValueError, match="核验备注"):
        review_job(session, pending_review_job.id, "approve", "", "管理员")


@pytest.mark.parametrize("grade", ["C", "D"])
def test_final_approval_rejects_non_ab_intake_even_after_structuring(session, pending_review_job, grade):
    pending_review_job.intake_grade = grade
    pending_review_job.status = "待审核"
    pending_review_job.deadline = "2099-12-31"
    pending_review_job.quality_score = 90
    pending_review_job.risk_flags = "已人工核验"
    session.commit()
    before = session.query(ReviewLog).filter_by(job_id=pending_review_job.id).count()

    with pytest.raises(ValueError, match="A/B|分级"):
        review_job(session, pending_review_job.id, "approve", "信息已核验", "管理员")

    assert pending_review_job.status == "待审核"
    assert session.query(ReviewLog).filter_by(job_id=pending_review_job.id).count() == before
