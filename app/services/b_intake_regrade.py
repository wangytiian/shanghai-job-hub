"""Narrow, auditable regrade of historical SBS audience-unclear leads."""

from datetime import datetime
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Job, ReviewLog, Source
from app.services.audit import record_event
from app.services.deadline_policy import extract_application_deadline, parse_known_deadline
from app.services.real_collection import REAL_RISK_FLAG, _content_fingerprint
from app.services.review_intake import REVIEW_NOTE, evaluate_review_intake
from app.services.source_candidate_policy import candidate_from_detail


def _source_for_job(session: Session, job: Job) -> Source | None:
    source_name, separator, identity = job.fingerprint.partition("|")
    if not separator or not identity:
        return None
    source = session.scalar(select(Source).where(Source.name == source_name))
    if source is None or source.source_key != "sbs-jobs" or source.library_tier != "A":
        return None
    return source


def _decision(session: Session, job: Job, now: datetime) -> str:
    if job.is_demo or job.status != "待核验" or job.notice_type != "新招聘":
        return "仅处理真实、待核验、已确认为新招聘的线索"
    # A returned review can be 待核验 again while retaining an operator's
    # score, checklist, routing choice or notes. Never erase that work just
    # to relabel an old audience-unclear lead.
    has_human_fields = (
        job.quality_score != 0
        or (job.verification_checks or "{}").strip() != "{}"
        or job.verification_version != 0
        or job.student_fit_level != "待人工判断"
        or job.distribution_recommendation != "仅保留资料库"
        or job.target_audience != "待人工判断"
        or job.risk_flags != REAL_RISK_FLAG
        or job.updated_by_user_id is not None
    )
    substantive_review = session.scalar(
        select(ReviewLog.id).where(
            ReviewLog.job_id == job.id,
            ReviewLog.action.not_in(("分类：新招聘", "批量分类：新招聘")),
        ).limit(1)
    )
    if has_human_fields or substantive_review is not None:
        return "岗位已有人工核验、编辑或审核记录，历史重判不覆盖"
    if job.lifecycle_status == "已截止" or job.posting_scope != "single_role":
        return "岗位已截止或不是单一明确岗位"
    source = _source_for_job(session, job)
    if source is None:
        return "历史迁移仅对已验证的上海商学院 A 类岗位开放"
    if not job.content_fingerprint or job.version < 1 or job.row_version < 1:
        return "缺少可追溯的岗位版本或原文指纹"
    if _content_fingerprint(f"{job.evidence_text}\n{job.attachment_links}") != job.content_fingerprint:
        return "原文指纹与当前岗位事实不一致"
    known_deadlines = tuple(
        deadline for deadline in (
            parse_known_deadline(job.deadline),
            extract_application_deadline(job.evidence_text),
        ) if deadline is not None
    )
    if not known_deadlines or any(deadline < now.date() for deadline in known_deadlines):
        return "报名期限未知或已截止"
    source_host = urlsplit(source.url).hostname
    detail_url = urlsplit(job.source_url)
    if detail_url.scheme != "https" or detail_url.hostname != source_host:
        return "岗位详情不是已登记的官方就业网地址"
    identity = job.fingerprint.partition("|")[2]
    detail = SimpleNamespace(
        identity_key=identity,
        detail_url=job.source_url,
        title=job.job_title,
        employer_name=job.employer_name,
        evidence_text=job.evidence_text,
        official_url=job.official_url,
        location_category=job.location_category,
        location_detail=job.location_detail,
        published_at=job.published_at.date().isoformat() if job.published_at else "",
    )
    candidate = candidate_from_detail(detail, source_listing_url=source.url)
    decision = evaluate_review_intake(candidate, now=now)
    if decision.verdict != "review_only":
        return "岗位证据未满足 B 类门槛：" + ",".join(decision.reason_codes or (decision.verdict,))
    return ""


def preview_b_regrade(session: Session, now: datetime | None = None) -> list[dict[str, object]]:
    """Read-only preview; a result is not authorization to publish or distribute."""
    current = now or datetime.now()
    rows = session.scalars(
        select(Job).where(Job.intake_grade == "C", Job.intake_reason == REVIEW_NOTE).order_by(Job.id)
    ).all()
    return [
        {
            "id": job.id,
            "decision": "eligible" if not (reason := _decision(session, job, current)) else "skip",
            "reason": reason or "官方岗位事实完整；学生受众仍待人工确认",
            "employer_name": job.employer_name,
            "job_title": job.job_title,
            "source_url": job.source_url,
            "deadline": job.deadline,
            "version": job.version,
            "content_fingerprint": job.content_fingerprint,
        }
        for job in rows
    ]


def apply_b_regrade(session: Session, now: datetime | None, ids: list[int] | tuple[int, ...],
                    *, strict: bool = False) -> int:
    """Recheck explicit IDs and move eligible leads into the non-publishable B review queue."""
    selected = {int(job_id) for job_id in ids}
    if not selected:
        return 0
    current = now or datetime.now()
    eligible = {
        int(row["id"])
        for row in preview_b_regrade(session, current)
        if row["decision"] == "eligible" and int(row["id"]) in selected
    }
    if strict and selected - eligible:
        raise ValueError(f"指定岗位不在当前可重判预览中：{sorted(selected - eligible)}")
    applied = 0
    try:
        for job_id in sorted(eligible):
            job = session.get(Job, job_id)
            if job is None or _decision(session, job, current):
                raise ValueError(f"岗位 #{job_id} 已不符合重判条件，迁移已回滚")
            old_version = job.version
            old_row_version = job.row_version
            fingerprint = job.content_fingerprint
            changed = session.execute(
                update(Job).where(
                    Job.id == job_id,
                    Job.intake_grade == "C",
                    Job.intake_reason == REVIEW_NOTE,
                    Job.status == "待核验",
                    Job.notice_type == "新招聘",
                    Job.version == old_version,
                    Job.row_version == old_row_version,
                    Job.content_fingerprint == fingerprint,
                ).values(
                    intake_grade="B",
                    intake_route="普通待核验",
                    intake_reason=REVIEW_NOTE,
                    intake_evidence="岗位事实完整，受众待确认",
                    intake_confidence="中",
                    status="待核验",
                    quality_score=0,
                    risk_flags=REAL_RISK_FLAG,
                    verification_checks="{}",
                    verification_version=0,
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
                    last_change_summary="历史受众待确认线索重判为 B；仍待人工核验",
                    version=old_version + 1,
                    row_version=old_row_version + 1,
                    updated_at=current,
                ),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                raise ValueError(f"岗位 #{job_id} 已变化，迁移已回滚")
            evidence = {
                "old_grade": "C",
                "new_grade": "B",
                "content_fingerprint": fingerprint,
                "old_version": old_version,
                "new_version": old_version + 1,
                "source_url": job.source_url,
                "audience_status": "待人工确认",
            }
            session.add(ReviewLog(
                job_id=job_id,
                action="历史重判：C→B",
                note=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                operator_name="本地迁移脚本",
                created_at=current,
            ))
            record_event(
                session,
                actor_user_id=None,
                actor_label="本地迁移脚本",
                action="job.intake_regraded",
                entity_type="job",
                entity_id=str(job_id),
                request_id="",
                changes=evidence,
            )
            applied += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return applied
