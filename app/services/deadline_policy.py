"""Deterministic deadline parsing and expiry protection for recruitment notices."""

from datetime import date, datetime
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job


DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*(?:月|[-/.])\s*(?P<day>\d{1,2})\s*(?:日)?"
)
APPLICATION_WINDOW_PATTERN = re.compile(
    r"(?:报名|报考|申请|投递|应聘)(?:时间|期限|截止)?[^。；;\n]{0,100}", re.IGNORECASE
)
DEADLINE_HINT_PATTERN = re.compile(r"(?:报名截止|截止日期|截止时间|截至|截止)[^。；;\n]{0,80}")


def _dates_in(value: str) -> list[date]:
    dates: list[date] = []
    for match in DATE_PATTERN.finditer(value):
        try:
            dates.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            continue
    return dates


def extract_application_deadline(evidence_text: str) -> date | None:
    """Return an explicit application end date, never guessing from publication dates."""
    for match in APPLICATION_WINDOW_PATTERN.finditer(evidence_text):
        dates = _dates_in(match.group(0))
        if dates:
            return max(dates)
    for match in DEADLINE_HINT_PATTERN.finditer(evidence_text):
        dates = _dates_in(match.group(0))
        if dates:
            return max(dates)
    return None


def is_application_expired(evidence_text: str, today: date | None = None) -> bool:
    deadline = extract_application_deadline(evidence_text)
    return deadline is not None and deadline < (today or date.today())


def parse_known_deadline(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        dates = _dates_in(value)
        return max(dates) if dates else None


def job_application_deadline(job: Job) -> date | None:
    return parse_known_deadline(job.deadline) or extract_application_deadline(job.evidence_text)


def mark_job_expired(job: Job, deadline: date) -> None:
    job.deadline = deadline.isoformat()
    job.lifecycle_status = "已截止"
    if job.status in {"待核验", "待审核", "可发布"}:
        job.status = "已截止"
    job.last_change_summary = f"截止保护：原文明确报名截止至 {deadline.isoformat()}，已移出待处理队列"


def expire_known_deadline_jobs(session: Session, today: date | None = None) -> int:
    current_day = today or date.today()
    changed = 0
    jobs = session.scalars(
        select(Job).where(Job.is_demo.is_(False), Job.status.in_(("待核验", "待审核", "可发布")))
    ).all()
    for job in jobs:
        deadline = job_application_deadline(job)
        if deadline is not None and deadline < current_day:
            mark_job_expired(job, deadline)
            changed += 1
    if changed:
        session.commit()
    return changed
