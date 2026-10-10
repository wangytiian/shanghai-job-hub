"""Explicit, bounded import of registered B-source trials into pending review."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit, parse_qs

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Job, Source, SourceTrialRun, SourceTrialSample, User
from app.services.audit import record_event
from app.services.deadline_policy import extract_application_deadline, mark_job_expired
from app.services.intake_screening import screen_intake
from app.services.real_collection import REAL_RISK_FLAG, _content_fingerprint, _parse_published_at, _save_detail
from app.services.review_intake import REVIEW_NOTE, evaluate_review_intake
from app.services.source_candidate_policy import RULE_VERSION, SourceCandidate
from app.sources.campus_json import CampusJobDetail, _location


_PILOT_ADAPTERS = {
    "sufe-jobs": ("sufe_job_json", "career.sufe.edu.cn"),
    "sjtu-internships": ("sjtu_internship_json", "www.job.sjtu.edu.cn"),
    "ncss-shanghai": ("ncss_shanghai_jobs", "www.ncss.cn"),
    "sues-internships": ("campus_jsonp_sues", "career.sues.edu.cn"),
    "sit-internships": ("campus_jsonp_sit", "job.sit.edu.cn"),
    "shcpa-jobs": ("shcpa_jobs", "www.shcpa.org.cn"),
}


def is_pilot_import_source(source: Source) -> bool:
    expected = _PILOT_ADAPTERS.get(source.source_key or "")
    return bool(expected and source.adapter_key == expected[0]
                and source.library_tier == "B" and not source.is_enabled
                and source.status != "暂停")
_SCHOOL_ONLY = re.compile(
    r"(?<!不)(?:仅限|只限|限|仅面向|只面向|只接受|仅接受|只招|仅招|仅接收|只接收)\s*"
    r"(?:本校|上海财经大学|上海交通大学|上海工程技术大学|上海应用技术大学|上财|交大学生)|"
    r"(?:不对|不向)\s*外校(?:学生)?(?:开放|报名|申请|投递)|"
    r"(?:不接受|不接收)\s*外校(?:学生)?"
)
_ELITE_SCHOOL_ONLY = re.compile(
    r"(?:国内外|全国)?\s*(?:Top|顶尖|985|211)\s*(?:院校|高校|大学)"
    r"(?!\s*(?:的)?\s*(?:背景|毕业生|学生|者)?\s*(?:的)?\s*(?:毕业生|学生|者)?\s*(?:优先|加分|更佳))",
    re.I,
)


@dataclass(frozen=True)
class PilotImportResult:
    created: int
    updated: int
    unchanged: int
    skipped: int
    skip_reasons: dict[str, int]
    invalidated: int = 0


def _sample_candidate(sample: SourceTrialSample, source: Source) -> SourceCandidate:
    return SourceCandidate(
        identity_key=sample.identity_key,
        announcement_id=sample.announcement_id,
        job_id=sample.job_id,
        employer_name=sample.employer_name,
        title=sample.title,
        location_category=sample.location_category,
        location_detail=sample.location_detail,
        published_at=sample.published_at,
        deadline=sample.deadline,
        evidence_text=sample.evidence_text,
        source_url=sample.source_url,
        application_kind=sample.application_kind,
        application_value=sample.application_value,
        application_evidence=sample.application_evidence,
        source_listing_url=source.url,
    )


def _sample_problem(sample: SourceTrialSample, expected_host: str) -> str:
    expected_identity = (
        f"{sample.announcement_id}:{sample.job_id}"
        if sample.job_id else sample.announcement_id
    )
    if not expected_identity or sample.identity_key != expected_identity:
        return "IDENTITY_MISMATCH"
    actual_hash = sha256(" ".join(sample.evidence_text.split()).encode("utf-8")).hexdigest()
    if actual_hash != sample.content_hash:
        return "CONTENT_HASH_MISMATCH"
    url = urlsplit(sample.source_url.strip())
    try:
        valid_port = url.port in {None, 443}
    except ValueError:
        valid_port = False
    if url.scheme != "https" or url.hostname != expected_host or url.username or url.password or not valid_port or url.fragment:
        return "SOURCE_URL_MISMATCH"
    query = parse_qs(url.query, keep_blank_values=True)
    if expected_host == "www.ncss.cn":
        if url.path != f"/student/jobs/{sample.announcement_id}/detail.html" or query or sample.job_id:
            return "SOURCE_URL_MISMATCH"
    elif expected_host in {"career.sues.edu.cn", "job.sit.edu.cn"}:
        expected_path = "/page/positionsx.html" if expected_host == "career.sues.edu.cn" else "/page/positionzp.html"
        expected_query = {"wid": [sample.announcement_id]}
        if expected_host == "job.sit.edu.cn":
            expected_query["type"] = ["1"]
        if (not re.fullmatch(r"[a-fA-F0-9]{32}", sample.announcement_id)
                or url.path != expected_path or query != expected_query):
            return "SOURCE_URL_MISMATCH"
        if sample.job_id and sample.job_id not in {"position", sha256(sample.title.encode("utf-8")).hexdigest()[:16]}:
            return "IDENTITY_MISMATCH"
    elif expected_host == "www.shcpa.org.cn":
        if (url.path != "/recruit/getRecruitDetailedView.do" or not sample.announcement_id.isdigit()
                or query != {"rid": [sample.announcement_id]} or sample.job_id):
            return "SOURCE_URL_MISMATCH"
    if expected_host in {"career.sufe.edu.cn", "www.job.sjtu.edu.cn"} and sample.evidence_text.startswith("招聘公告："):
        lines = dict(
            line.split("：", 1) for line in sample.evidence_text.splitlines()
            if "：" in line and line.split("：", 1)[0] in {"正文", "岗位地点", "单位地址"}
        )
        inferred_category, inferred_detail = _location(
            lines.get("岗位地点", ""), lines.get("正文", ""), bool(sample.job_id),
            lines.get("单位地址", ""), parent_body_applies=False,
        )
        if (
            sample.location_category != inferred_category
            or sample.location_detail.strip() != inferred_detail.strip()
        ):
            return "LOCATION_EVIDENCE_MISMATCH"
    return ""


def _has_newer_sample(session: Session, source_id: int, run: SourceTrialRun, identity_key: str) -> bool:
    return session.scalar(
        select(SourceTrialSample.id)
        .join(SourceTrialRun, SourceTrialRun.run_id == SourceTrialSample.run_id)
        .where(
            SourceTrialRun.source_id == source_id,
            SourceTrialRun.state.in_(("completed", "partial")),
            SourceTrialRun.finished_at > run.finished_at,
            SourceTrialSample.identity_key == identity_key,
        )
        .limit(1)
    ) is not None


def _decision_problem(sample: SourceTrialSample, candidate: SourceCandidate, now: datetime) -> tuple[str, str]:
    body_deadline = extract_application_deadline(candidate.evidence_text)
    if body_deadline is not None and body_deadline < now.date():
        return "", "APPLICATION_EXPIRED"
    decision = evaluate_review_intake(candidate, now=now)
    if _SCHOOL_ONLY.search(f"{candidate.title}\n{candidate.evidence_text}") or _ELITE_SCHOOL_ONLY.search(
        f"{candidate.title}\n{candidate.evidence_text}"
    ):
        return "", "SCHOOL_EXCLUSIVE"
    if decision.verdict not in {"qualified", "review_only"}:
        return "", next(iter(decision.reason_codes), "TRIAL_NOT_ELIGIBLE")
    if sample.decision not in {"qualified", "needs_evidence", "review_only"}:
        return "", "TRIAL_NOT_ELIGIBLE"
    try:
        recorded_reasons = tuple(json.loads(sample.reason_codes))
    except (TypeError, ValueError):
        return "", "TRIAL_REASON_INVALID"
    if sample.decision == "qualified" and recorded_reasons:
        return "", "TRIAL_REASON_INVALID"
    if sample.decision != "qualified" and set(recorded_reasons) != {"TARGET_AUDIENCE_UNCLEAR"}:
        return "", next(iter(recorded_reasons), "TRIAL_NOT_ELIGIBLE")
    expected_verdict = "qualified" if sample.decision == "qualified" else "review_only"
    if decision.verdict != expected_verdict:
        return "", "TRIAL_DECISION_CHANGED"
    if candidate.application_evidence not in candidate.evidence_text:
        return "", "APPLICATION_MISSING"
    return decision.verdict, ""


def _fact_context(candidate: SourceCandidate) -> str:
    """Invalidate prior review when a persisted non-body fact changes."""
    return json.dumps(
        {
            "employer": candidate.employer_name,
            "title": candidate.title,
            "location_category": candidate.location_category,
            "location_detail": candidate.location_detail,
            "published_at": candidate.published_at,
            "deadline": candidate.deadline,
            "application_kind": candidate.application_kind,
            "application_value": candidate.application_value,
            "application_evidence": candidate.application_evidence,
            "source_url": candidate.source_url,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _detail(candidate: SourceCandidate) -> CampusJobDetail:
    return CampusJobDetail(
        title=candidate.title,
        published_at=candidate.published_at,
        detail_url=candidate.source_url,
        evidence_text=candidate.evidence_text,
        identity_key=candidate.identity_key,
        announcement_title=candidate.title,
        employer_name=candidate.employer_name,
        location_category=candidate.location_category,
        location_detail=candidate.location_detail,
        official_url=(
            candidate.application_value
            if candidate.application_kind in {"official_url", "official_platform"}
            else candidate.source_url
        ),
    )


def _latest_trial_run_at(session: Session, job: Job) -> datetime | None:
    """Human verification time is not a source-run watermark; use the audit trail."""
    events = session.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_type == "job",
            AuditEvent.entity_id == str(job.id),
            AuditEvent.action.in_(("source_trial.import", "source_trial.invalidate")),
        )
    ).all()
    latest: datetime | None = None
    for event in events:
        try:
            run_id = json.loads(event.changes).get("run_id")
        except (TypeError, ValueError, AttributeError):
            continue
        if not run_id:
            continue
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
        if run is not None and run.finished_at is not None:
            latest = max(latest, run.finished_at) if latest else run.finished_at
    return latest


def _invalidate_existing_job(
    job: Job, candidate: SourceCandidate, problem: str, run: SourceTrialRun,
) -> bool:
    new_fingerprint = _content_fingerprint(
        f"{candidate.evidence_text}\n[]\n{_fact_context(candidate)}"
    )
    if (
        job.content_fingerprint == new_fingerprint
        and job.intake_grade == "D"
        and job.last_verified_at == run.finished_at
    ):
        return False
    job.evidence_text = candidate.evidence_text
    job.content_fingerprint = new_fingerprint
    job.employer_name = candidate.employer_name or job.employer_name
    job.job_title = candidate.title or job.job_title
    job.announcement_title = candidate.title or job.announcement_title
    job.location_category = candidate.location_category
    job.location_detail = candidate.location_detail
    job.source_url = candidate.source_url
    job.published_at = _parse_published_at(candidate.published_at)
    job.application_method = "email" if candidate.application_kind == "email" else "official_page"
    job.application_contact = candidate.application_value if candidate.application_kind == "email" else ""
    job.deadline = candidate.deadline or "原文待人工确认"
    job.status = "待核验"
    job.lifecycle_status = "有更新"
    job.last_change_summary = f"较新试采样本不符合入库条件：{problem}；旧人工核验已撤销"
    job.intake_grade = "D"
    job.intake_route = "过滤留档"
    job.intake_reason = job.last_change_summary
    job.intake_confidence = "高"
    job.quality_score = 0
    job.risk_flags = REAL_RISK_FLAG
    job.verification_checks = "{}"
    job.verification_version = 0
    job.ai_suggested_score = 0
    job.ai_score_status = "待建议"
    job.ai_score_reason = ""
    job.ai_score_breakdown = "{}"
    job.ai_score_confidence = "低"
    job.ai_scored_at = None
    job.student_fit_level = "待人工判断"
    job.distribution_recommendation = "仅保留资料库"
    job.ai_rationale = ""
    job.ai_confidence = "低"
    job.last_verified_at = run.finished_at
    job.version += 1
    job.row_version += 1
    expired = extract_application_deadline(candidate.evidence_text)
    if problem == "APPLICATION_EXPIRED" and expired is not None:
        mark_job_expired(job, expired)
    return True


def import_trial_run(
    session: Session,
    source_id: int,
    run_id: str,
    actor_id: int | None,
    *,
    now: datetime | None = None,
    max_jobs: int = 10,
    sample_ids: tuple[int, ...] | None = None,
    request_id: str = "",
) -> PilotImportResult:
    """Recheck persisted trial evidence, then commit at most ten new review jobs."""
    current = now or datetime.now()
    if max_jobs < 1:
        raise ValueError("单次导入上限必须大于零")
    if not sample_ids or len(sample_ids) > 10 or len(set(sample_ids)) != len(sample_ids):
        raise ValueError("请选择 1 至 10 条不重复的试采样本")
    limit = min(max_jobs, 10)
    try:
        source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source is None:
            raise ValueError("试点来源不存在")
        expected = _PILOT_ADAPTERS.get(source.source_key or "")
        if expected is None or source.adapter_key != expected[0]:
            raise ValueError("来源不在 B 类试点白名单或适配器不匹配")
        if source.library_tier != "B" or source.is_enabled:
            raise ValueError("试点来源必须保持 B 类且禁用日常采集")
        if source.status == "暂停":
            raise ValueError("试点来源已暂停")
        actor = session.get(User, actor_id) if actor_id is not None else None
        if actor_id is not None and (actor is None or not actor.is_active or actor.role != "admin"):
            raise ValueError("只有管理员可以导入 B 类试采样本")
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id).with_for_update())
        if run is None or run.source_id != source.id:
            raise ValueError("试采轮次不存在或不属于该来源")
        if run.state not in {"completed", "partial"} or run.finished_at is None:
            raise ValueError("仅允许导入已结束的完成或部分完成轮次")
        if run.adapter_version != source.adapter_version or run.rule_version != RULE_VERSION:
            raise ValueError("试采轮次适配器或候选规则版本已变化")
        samples = session.scalars(
            select(SourceTrialSample)
            .where(SourceTrialSample.run_id == run.run_id)
            .order_by(SourceTrialSample.id)
        ).all()
        selected_ids = set(sample_ids)
        if not selected_ids.issubset({sample.id for sample in samples}):
            raise ValueError("选中的试采样本不存在或不属于该轮次")
        samples = [sample for sample in samples if sample.id in selected_ids]
        counts = Counter(created=0, updated=0, unchanged=0, skipped=0)
        reasons: Counter[str] = Counter()
        for sample in samples:
            if current - run.finished_at > timedelta(days=30):
                counts["skipped"] += 1
                reasons["TRIAL_TOO_OLD"] += 1
                continue
            problem = _sample_problem(sample, expected[1])
            if problem:
                counts["skipped"] += 1
                reasons[problem] += 1
                continue
            candidate = _sample_candidate(sample, source)
            fingerprint = f"{source.name}|{candidate.identity_key}"
            existing = session.scalar(select(Job).where(Job.fingerprint == fingerprint).with_for_update())
            if existing is not None and existing.is_demo:
                counts["skipped"] += 1
                reasons["EXISTING_DEMO_JOB"] += 1
                continue
            last_source_run_at = _latest_trial_run_at(session, existing) if existing is not None else None
            if last_source_run_at is not None and run.finished_at < last_source_run_at:
                counts["skipped"] += 1
                reasons["STALE_TRIAL_RUN"] += 1
                continue
            if _has_newer_sample(session, source.id, run, sample.identity_key):
                counts["skipped"] += 1
                reasons["SUPERSEDED_TRIAL_SAMPLE"] += 1
                continue
            verdict, problem = _decision_problem(sample, candidate, current)
            if problem:
                if existing is not None and not problem.startswith("TRIAL_"):
                    if _invalidate_existing_job(existing, candidate, problem, run):
                        counts["updated"] += 1
                        counts["invalidated"] += 1
                        record_event(
                            session,
                            actor_user_id=actor.id if actor else None,
                            actor_label=actor.display_name if actor else "本地管理员",
                            action="source_trial.invalidate",
                            entity_type="job",
                            entity_id=str(existing.id),
                            request_id=request_id,
                            changes={
                                "source_key": source.source_key,
                                "source_id": source.id,
                                "run_id": run.run_id,
                                "sample_id": sample.id,
                                "identity_key": candidate.identity_key,
                                "content_hash": sample.content_hash,
                                "reason": problem,
                            },
                        )
                    else:
                        counts["unchanged"] += 1
                    continue
                counts["skipped"] += 1
                reasons[problem] += 1
                continue
            if existing is None and counts["created"] >= limit:
                counts["skipped"] += 1
                reasons["IMPORT_LIMIT_REACHED"] += 1
                continue
            review_note = REVIEW_NOTE if verdict == "review_only" else ""
            outcome = _save_detail(
                session, source, _detail(candidate), current,
                review_note=review_note, fingerprint_context=_fact_context(candidate),
            )
            if outcome not in {"created", "updated", "unchanged"}:
                counts["skipped"] += 1
                reasons["APPLICATION_EXPIRED"] += 1
                continue
            session.flush()
            job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
            assert job is not None
            job.last_verified_at = run.finished_at
            if outcome != "unchanged":
                if outcome == "created":
                    job.collected_at = run.finished_at
                job.employer_name = candidate.employer_name
                job.job_title = candidate.title
                job.location_category = candidate.location_category
                job.location_detail = candidate.location_detail
                job.application_method = (
                    "email" if candidate.application_kind == "email" else "official_page"
                )
                job.application_contact = (
                    candidate.application_value if candidate.application_kind == "email" else ""
                )
                job.deadline = candidate.deadline or "原文待人工确认"
                job.published_at = _parse_published_at(candidate.published_at)
                job.quality_score = 0
                job.ai_suggested_score = 0
                job.ai_score_status = "待建议"
                job.ai_score_reason = ""
                job.ai_score_breakdown = "{}"
                job.ai_score_confidence = "低"
                job.ai_scored_at = None
                if verdict == "qualified":
                    screening = screen_intake(candidate.title, candidate.evidence_text)
                    job.intake_grade = "B" if screening.grade == "C" else screening.grade
                    job.intake_route = "普通待核验" if screening.grade == "C" else screening.route
                    job.intake_reason = (
                        "试采规则确认原文有学生适配证据；待人工核验。"
                        if screening.grade == "C" else screening.reason
                    )
                    job.intake_evidence = screening.evidence
                    job.intake_confidence = screening.confidence
                provenance = (
                    f"试采来源={source.source_key}；run_id={run.run_id}；sample_id={sample.id}"
                )
                if provenance not in job.evidence_note:
                    job.evidence_note = " ".join(filter(None, (job.evidence_note, provenance)))
                if re.search(r"知名高校|重点高校|双一流", candidate.evidence_text):
                    school_note = "院校资格待人工确认：请核实目标学生是否符合原文要求。"
                    if school_note not in job.evidence_note:
                        job.evidence_note = " ".join((job.evidence_note, school_note))
                job.risk_flags = REAL_RISK_FLAG
            counts[outcome] += 1
            record_event(
                session,
                actor_user_id=actor.id if actor else None,
                actor_label=actor.display_name if actor else "本地管理员",
                action="source_trial.import",
                entity_type="job",
                entity_id=str(job.id),
                request_id=request_id,
                changes={
                    "source_key": source.source_key,
                    "source_id": source.id,
                    "run_id": run.run_id,
                    "sample_id": sample.id,
                    "identity_key": candidate.identity_key,
                    "content_hash": sample.content_hash,
                    "outcome": outcome,
                },
            )
        session.commit()
        return PilotImportResult(
            counts["created"], counts["updated"], counts["unchanged"],
            counts["skipped"], dict(reasons), counts["invalidated"],
        )
    except Exception:
        session.rollback()
        raise
