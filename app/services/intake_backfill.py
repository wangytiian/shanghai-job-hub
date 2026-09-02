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
        job.intake_grade = result.grade
        job.intake_route = result.route
        job.intake_reason = result.reason
        job.intake_evidence = result.evidence
        job.intake_confidence = result.confidence
    if jobs:
        session.commit()
    return len(jobs)
