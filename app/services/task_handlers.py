"""Idempotent task handlers executed by the worker process."""

from datetime import datetime
import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, ReviewLog, Source, SourceTrialRun, User, WorkItem
from app.services.ai_scoring import suggest_job_score
from app.services.concurrency import EditConflict, assert_version
from app.services.work_queue import complete, needs_review
from app.services.source_candidate_policy import RULE_VERSION
from app.services.source_trials import TrialBudget, run_source_trial


def handle_score_job(session: Session, item: WorkItem) -> bool:
    job = session.get(Job, item.target_id)
    if job is None:
        return needs_review(session, item.id, item.lease_token, "target_not_found")
    try:
        assert_version(actual=job.row_version, expected=item.expected_row_version)
        assert_version(actual=job.version, expected=item.expected_fact_version)
    except EditConflict:
        return needs_review(session, item.id, item.lease_token, "stale_target")

    result = suggest_job_score(job, complete=None)
    job.ai_suggested_score = result.score
    job.ai_score_status = result.status
    job.ai_score_reason = result.reason
    job.ai_score_breakdown = json.dumps(result.breakdown, ensure_ascii=False)
    job.ai_score_confidence = result.confidence
    job.ai_scored_at = datetime.now()
    job.row_version += 1
    session.add(
        ReviewLog(
            job_id=job.id,
            action="建议分生成",
            note=f"任务 #{item.id}；建议分：{result.score}；状态：{result.status}；理由：{result.reason}",
            operator_name="系统任务",
        )
    )
    return complete(session, item.id, item.lease_token, {"job_id": job.id, "status": result.status, "score": result.score})


def handle_source_trial(session: Session, item: WorkItem) -> bool:
    """Execute the bounded trial owned by a leased work item."""
    source = session.get(Source, item.target_id)
    if source is None:
        return needs_review(session, item.id, item.lease_token, "target_not_found")
    if source.adapter_version != item.config_revision or item.template_version != RULE_VERSION:
        return needs_review(session, item.id, item.lease_token, "stale_target")
    actor = session.get(User, item.requested_by) if item.requested_by else None
    requested_by = actor.display_name if actor else "本地管理员"
    owned_session_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    with httpx.Client(follow_redirects=True, max_redirects=3) as client:
        run_id = run_source_trial(
            owned_session_factory,
            client,
            source.id,
            requested_by,
            TrialBudget(),
            f"work-item:{item.id}",
            resume_owned_running=True,
        )
    run = session.scalar(
        select(SourceTrialRun).where(SourceTrialRun.run_id == run_id)
    )
    if run is None or run.state == "running":
        return needs_review(session, item.id, item.lease_token, "trial_not_finalized")
    if run.state in {"failed", "interrupted"}:
        return needs_review(session, item.id, item.lease_token, "trial_failed")
    return complete(
        session,
        item.id,
        item.lease_token,
        {"source_id": source.id, "run_id": run_id, "state": run.state},
    )
