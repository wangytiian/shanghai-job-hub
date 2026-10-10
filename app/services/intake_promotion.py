"""Evidence-backed confirmation of audience-unclear real leads."""

from datetime import datetime
import json
from urllib.parse import urlsplit

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Job, ReviewLog, User
from app.services.audit import record_event
from app.services.deadline_policy import extract_application_deadline, job_application_deadline
from app.services.real_collection import REAL_RISK_FLAG
from app.services.review_intake import REVIEW_NOTE


def _validate_evidence(url: str, excerpt: str, note: str) -> tuple[str, str, str]:
    url, excerpt, note = url.strip(), excerpt.strip(), note.strip()
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid_url = False
    if not valid_url or any(char.isspace() for char in url):
        raise ValueError("请提供有效的官方证据 HTTP(S) 链接")
    if not excerpt:
        raise ValueError("请填写官方页面的连续原文摘录")
    if not note:
        raise ValueError("请填写人工判断说明")
    return url, excerpt, note


def promote_review_only_job(
    session: Session,
    job_id: int,
    *,
    actor_id: int | None,
    expected_version: int,
    expected_fingerprint: str,
    evidence_url: str,
    evidence_excerpt: str,
    note: str,
    request_id: str = "",
    now: datetime | None = None,
) -> Job:
    """Confirm only a current REVIEW_NOTE lead; all publication gates remain in force."""
    actor = session.get(User, actor_id) if actor_id is not None else None
    if actor_id is not None and (
        actor is None or not actor.is_active or not (actor.can_review or actor.role == "admin")
    ):
        raise ValueError("只有审核员或管理员可以复核入库分级")
    url, excerpt, explanation = _validate_evidence(evidence_url, evidence_excerpt, note)
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    if job.version != expected_version:
        raise ValueError("岗位版本已变化，请刷新页面后重试")
    if job.content_fingerprint != expected_fingerprint or not job.content_fingerprint:
        raise ValueError("岗位原文指纹已变化，请刷新页面后重试")
    if job.is_demo:
        raise ValueError("演示岗位不能执行真实线索升档")
    if job.status != "待核验":
        raise ValueError("只有待核验岗位可以执行入库升档")
    if job.notice_type != "新招聘":
        raise ValueError("只有确认为新招聘的岗位可以执行入库升档")
    if job.intake_grade not in {"B", "C"} or job.intake_reason != REVIEW_NOTE:
        raise ValueError("只有受众待确认的 B/C 级线索可以确认")

    current_time = now or datetime.now()
    known_deadlines = (job_application_deadline(job), extract_application_deadline(job.evidence_text))
    if job.lifecycle_status == "已截止" or any(
        deadline is not None and deadline < current_time.date() for deadline in known_deadlines
    ):
        raise ValueError("岗位报名已截止，不能升档")

    old_row_version = job.row_version
    old_grade = job.intake_grade
    evidence_note = " ".join((job.evidence_note or "").replace(REVIEW_NOTE, "").split())
    actor_label = actor.display_name if actor else "本地管理员"
    evidence = {
        "old_grade": old_grade,
        "new_grade": "B",
        "evidence_url": url,
        "evidence_excerpt": excerpt,
        "note": explanation,
        "content_fingerprint": expected_fingerprint,
        "old_version": expected_version,
        "new_version": expected_version + 1,
    }
    try:
        changed = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.version == expected_version,
                Job.row_version == old_row_version,
                Job.content_fingerprint == expected_fingerprint,
                Job.intake_grade == old_grade,
                Job.intake_reason == REVIEW_NOTE,
                Job.status == "待核验",
                Job.notice_type == "新招聘",
                Job.is_demo.is_(False),
                Job.lifecycle_status != "已截止",
            )
            .values(
                intake_grade="B",
                intake_route="普通待核验",
                intake_reason="人工复核已确认受众可申请；证据与判断见升档审计记录",
                intake_evidence=excerpt[:160],
                intake_confidence="中",
                evidence_note=evidence_note,
                status="待核验",
                quality_score=0,
                risk_flags=REAL_RISK_FLAG,
                verification_checks="{}",
                verification_version=0,
                last_verified_at=None,
                ai_suggested_score=0,
                ai_score_status="待建议",
                ai_score_reason="",
                ai_score_breakdown="{}",
                ai_score_confidence="低",
                ai_scored_at=None,
                student_fit_level="待人工判断",
                distribution_recommendation="仅保留资料库",
                ai_rationale="",
                ai_confidence="低",
                last_change_summary=f"人工补充受众证据，入库分级 {old_grade}→B；仍待完整核验",
                version=expected_version + 1,
                row_version=old_row_version + 1,
                updated_by_user_id=actor.id if actor else None,
                updated_at=current_time,
            ),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount != 1:
            raise ValueError("岗位状态或版本发生并发变化，请刷新页面后重试")
        session.add(
            ReviewLog(
                job_id=job_id,
                action="入库升档：C→B" if old_grade == "C" else "受众确认：B",
                note=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                operator_name=actor_label,
                actor_user_id=actor.id if actor else None,
                created_at=current_time,
            )
        )
        record_event(
            session,
            actor_user_id=actor.id if actor else None,
            actor_label=actor_label,
            action="job.intake_promoted",
            entity_type="job",
            entity_id=str(job_id),
            request_id=request_id,
            changes=evidence,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(job)
    return job
