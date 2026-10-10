from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.services.intake_screening import screen_intake


def backfill_unscreened_intake_jobs(session: Session) -> int:
    """Give legacy pending records a traceable deterministic initial grade once."""
    jobs = session.scalars(
        select(Job).where(
            Job.is_demo.is_(False),
            Job.status == "待核验",
            Job.intake_grade == "C",
            Job.intake_reason == "",
            Job.intake_evidence == "",
        )
    ).all()
    for job in jobs:
        result = screen_intake(job.job_title, job.evidence_text)
        # Legacy backfill has no verified source-candidate contract. Keep a
        # provisional B suggestion in C until its full job facts are checked.
        job.intake_grade = "C" if result.grade == "B" else result.grade
        job.intake_route = "人工复核" if result.grade == "B" else result.route
        job.intake_reason = (
            "旧线索尚未通过来源、上海地点和投递证据核验，保留人工复核。"
            if result.grade == "B" else result.reason
        )
        job.intake_evidence = result.evidence
        job.intake_confidence = "低" if result.grade == "B" else result.confidence
    if jobs:
        session.commit()
    return len(jobs)
