from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.services.jobs import validate_publishable


def return_unsafe_publishable_jobs(session: Session) -> int:
    """Protect historic records created before the current publication gate existed."""
    changed = 0
    for job in session.scalars(select(Job).where(Job.status == "可发布")).all():
        if validate_publishable(job):
            job.status = "待核验"
            job.last_change_summary = "发布保护：历史记录不满足当前发布门槛，已退回待核验"
            changed += 1
    if changed:
        session.commit()
    return changed
