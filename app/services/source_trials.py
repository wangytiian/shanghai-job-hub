"""Persistent, bounded trials for explicitly registered B-source adapters."""

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models import Source, SourceTrialRun, SourceTrialSample
from app.services.campus_trial import CampusTrialTerminalError, run_campus_trial
from app.services.source_candidate_policy import (
    RULE_VERSION,
    SourceCandidate,
    candidate_from_detail,
    evaluate_candidate,
)
from app.sources.campus_json import SJTU_INTERNSHIP_SOURCE, SUFE_JOB_SOURCE
from app.sources.ncss import fetch_ncss_detail, fetch_ncss_shanghai_listings
from app.sources.sbs_jobs import fetch_sbs_detail, fetch_sbs_listings
from app.services.source_request_budget import RequestBudgetController, is_transport_timeout


@dataclass(frozen=True)
class TrialBudget:
    pages: int = 1
    list_limit: int = 10
    detail_limit: int = 3
    request_timeout_seconds: int = 12
    total_seconds: int = 600
    inter_request_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.pages <= 3:
            raise ValueError("试采页数必须在1到3页之间")
        if not 1 <= self.list_limit <= 30:
            raise ValueError("列表数量必须在1到30条之间")
        if not 1 <= self.detail_limit <= 10:
            raise ValueError("详情试采数量必须在1到10条之间")
        if not 1 <= self.request_timeout_seconds <= 12:
            raise ValueError("单请求超时必须在1到12秒之间")
        if not 1 <= self.total_seconds <= 600:
            raise ValueError("总执行预算必须在1到600秒之间")
        if self.inter_request_seconds < 1:
            raise ValueError("请求间隔不能少于1秒")


@dataclass(frozen=True)
class TrialFetchFailure:
    identity_key: str
    announcement_id: str
    source_url: str
    error_code: str
    message: str


@dataclass(frozen=True)
class TrialCollection:
    list_count: int
    detail_attempt_count: int
    detail_success_count: int
    candidates: tuple[SourceCandidate, ...]
    failures: tuple[TrialFetchFailure, ...]


class PartialTrialError(Exception):
    """A terminal fetch failure that still carries already collected results."""

    def __init__(self, collection: TrialCollection, cause: Exception):
        super().__init__(str(cause))
        self.collection = collection
        self.cause = cause


TrialAdapter = Callable[[object, TrialBudget], TrialCollection]
STALE_RUN_AFTER = timedelta(minutes=15)


def _campus_collection(report, campus_source, budget: TrialBudget) -> TrialCollection:
    failures = tuple(
        TrialFetchFailure(
            identity_key=f"error:{failure.announcement_id}",
            announcement_id=failure.announcement_id,
            source_url=failure.source_url,
            error_code=failure.error_code,
            message=failure.message,
        )
        for failure in report.failures
    )
    return TrialCollection(
        list_count=min(report.announcement_count, budget.list_limit),
        detail_attempt_count=report.detail_success_count + report.detail_failure_count,
        detail_success_count=report.detail_success_count,
        candidates=tuple(
            candidate_from_detail(detail, source_listing_url=campus_source.base_url)
            for detail in report.details
        ),
        failures=failures,
    )


def _campus_adapter(campus_source) -> TrialAdapter:
    def collect(client, budget: TrialBudget) -> TrialCollection:
        try:
            report = run_campus_trial(
                client,
                campus_source,
                pages=budget.pages,
                list_limit=budget.list_limit,
                detail_limit=budget.detail_limit,
                request_timeout_seconds=budget.request_timeout_seconds,
                total_seconds=budget.total_seconds,
                request_interval_seconds=budget.inter_request_seconds,
            )
        except CampusTrialTerminalError as exc:
            raise PartialTrialError(
                _campus_collection(exc.report, campus_source, budget), exc.cause
            ) from exc
        return _campus_collection(report, campus_source, budget)

    return collect


def _ncss_adapter(client, budget: TrialBudget) -> TrialCollection:
    controller = RequestBudgetController(
        total_seconds=budget.total_seconds,
        request_timeout_seconds=budget.request_timeout_seconds,
        interval_seconds=budget.inter_request_seconds,
    )
    listings = fetch_ncss_shanghai_listings(
        client,
        pages=budget.pages,
        timeout=float(budget.request_timeout_seconds),
        request_interval=budget.inter_request_seconds,
        controller=controller,
    )[: budget.list_limit]
    candidates: list[SourceCandidate] = []
    failures: list[TrialFetchFailure] = []
    success_count = 0
    for listing in listings[: budget.detail_limit]:
        try:
            candidates.append(
                candidate_from_detail(
                    fetch_ncss_detail(
                        client,
                        listing,
                        timeout=float(budget.request_timeout_seconds),
                        request_interval=budget.inter_request_seconds,
                        controller=controller,
                    ),
                    source_listing_url="https://www.ncss.cn/student/jobs/index.html",
                )
            )
            success_count += 1
        except Exception as exc:
            code = _classify_exception(exc)
            if code in {"RATE_LIMITED", "ACCESS_RESTRICTED", "UPSTREAM_ERROR", "TIMEOUT"}:
                failures.append(
                    TrialFetchFailure(
                        identity_key=f"error:{listing.job_id}",
                        announcement_id=listing.job_id,
                        source_url=listing.detail_url,
                        error_code=code,
                        message=str(exc)[:500],
                    )
                )
                raise PartialTrialError(
                    TrialCollection(
                        list_count=len(listings),
                        detail_attempt_count=success_count + len(failures),
                        detail_success_count=success_count,
                        candidates=tuple(candidates),
                        failures=tuple(failures),
                    ),
                    exc,
                ) from exc
            failures.append(
                TrialFetchFailure(
                    identity_key=f"error:{listing.job_id}",
                    announcement_id=listing.job_id,
                    source_url=listing.detail_url,
                    error_code="DETAIL_FETCH_FAILED",
                    message=str(exc)[:500],
                )
            )
    return TrialCollection(
        list_count=len(listings),
        detail_attempt_count=min(len(listings), budget.detail_limit),
        detail_success_count=success_count,
        candidates=tuple(candidates),
        failures=tuple(failures),
    )


def _sbs_adapter(client, budget: TrialBudget) -> TrialCollection:
    controller = RequestBudgetController(
        total_seconds=budget.total_seconds,
        request_timeout_seconds=budget.request_timeout_seconds,
        interval_seconds=budget.inter_request_seconds,
    )
    listings = fetch_sbs_listings(client, pages=budget.pages, timeout=float(budget.request_timeout_seconds), controller=controller)[: budget.list_limit]
    candidates: list[SourceCandidate] = []
    failures: list[TrialFetchFailure] = []
    successful_details = 0
    for listing in listings[: budget.detail_limit]:
        try:
            detail = fetch_sbs_detail(client, listing, timeout=float(budget.request_timeout_seconds), controller=controller)
            candidates.append(candidate_from_detail(detail, source_listing_url="https://jiuye.sbs.edu.cn/PositionList.aspx/"))
            successful_details += 1
        except Exception as exc:
            code = _classify_exception(exc)
            failures.append(TrialFetchFailure(f"error:{listing.job_id}", listing.job_id, listing.detail_url, code if code != "TRIAL_FAILED" else "DETAIL_FETCH_FAILED", str(exc)[:500]))
            if code in {"RATE_LIMITED", "ACCESS_RESTRICTED", "UPSTREAM_ERROR", "TIMEOUT"}:
                raise PartialTrialError(TrialCollection(len(listings), successful_details + len(failures), successful_details, tuple(candidates), tuple(failures)), exc) from exc
    return TrialCollection(len(listings), min(len(listings), budget.detail_limit), successful_details, tuple(candidates), tuple(failures))


TRIAL_ADAPTERS: dict[str, TrialAdapter] = {
    "sjtu_internship_json": _campus_adapter(SJTU_INTERNSHIP_SOURCE),
    "sufe_job_json": _campus_adapter(SUFE_JOB_SOURCE),
    "ncss_shanghai_jobs": _ncss_adapter,
    "sbs_jobs": _sbs_adapter,
}

_REGISTERED_SOURCE_KEYS = {
    "ncss_shanghai_jobs": "ncss-shanghai",
    "sjtu_internship_json": "sjtu-internships",
    "sufe_job_json": "sufe-jobs",
    "sbs_jobs": "sbs-jobs",
}


def is_registered_trial_source(source: Source) -> bool:
    expected_key = _REGISTERED_SOURCE_KEYS.get(source.adapter_key)
    return bool(
        source.source_key
        and source.adapter_key in TRIAL_ADAPTERS
        and (expected_key is None or source.source_key == expected_key)
    )


def _classify_exception(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code in {401, 403, 483}:
        return "ACCESS_RESTRICTED"
    if status_code is not None and status_code >= 500:
        return "UPSTREAM_ERROR"
    if is_transport_timeout(exc) or "timeout" in str(exc).lower() or "超时" in str(exc):
        return "TIMEOUT"
    if any(marker in str(exc).lower() for marker in ("登录", "login", "html", "结构", "字段")):
        return "PARSE_FAILED"
    return "TRIAL_FAILED"


def _retry_after_time(exc: Exception, current: datetime) -> datetime:
    raw = str(getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After", "")).strip()
    if raw.isdigit():
        return current + timedelta(seconds=max(1, int(raw)))
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except (TypeError, ValueError, OverflowError):
            pass
    return current + timedelta(hours=1)


def _sample_from_candidate(run_id: str, candidate: SourceCandidate, decision) -> SourceTrialSample:
    normalized = " ".join(candidate.evidence_text.split())
    return SourceTrialSample(
        run_id=run_id,
        identity_key=candidate.identity_key,
        announcement_id=candidate.announcement_id,
        job_id=candidate.job_id,
        employer_name=candidate.employer_name,
        title=candidate.title,
        location_category=candidate.location_category,
        location_detail=candidate.location_detail,
        published_at=candidate.published_at,
        deadline=candidate.deadline,
        evidence_text=candidate.evidence_text,
        source_url=candidate.source_url,
        application_kind=candidate.application_kind,
        application_value=candidate.application_value,
        application_evidence=candidate.application_evidence,
        decision=decision.verdict,
        reason_codes=json.dumps(decision.reason_codes, ensure_ascii=False),
        content_hash=sha256(normalized.encode("utf-8")).hexdigest(),
    )


def _sample_from_failure(run_id: str, failure: TrialFetchFailure) -> SourceTrialSample:
    return SourceTrialSample(
        run_id=run_id,
        identity_key=failure.identity_key,
        announcement_id=failure.announcement_id,
        source_url=failure.source_url,
        evidence_text=failure.message,
        decision="fetch_error",
        reason_codes=json.dumps([failure.error_code], ensure_ascii=False),
        content_hash=sha256(failure.message.encode("utf-8")).hexdigest(),
    )


def run_source_trial(
    session_factory,
    client,
    source_id: int,
    requested_by: str,
    budget: TrialBudget,
    idempotency_key: str,
    *,
    now: datetime | None = None,
    resume_owned_running: bool = False,
) -> str:
    """Run one B-source trial without holding a transaction during network I/O."""
    started_at = now or datetime.now()
    idempotency_key = idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("试采任务幂等键长度必须在1到120个字符之间")
    with session_factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError("试采来源不存在")
        existing = session.scalar(
            select(SourceTrialRun).where(
                SourceTrialRun.source_id == source_id,
                SourceTrialRun.idempotency_key == idempotency_key,
            )
        )
        resume_existing = bool(
            existing is not None
            and (
                (existing.state == "running" and resume_owned_running)
                or (existing.state == "running" and existing.started_at < started_at - STALE_RUN_AFTER)
                or (existing.state in {"failed", "interrupted"} and resume_owned_running)
            )
        )
        if existing is not None and not resume_existing:
            return existing.run_id
        if source.status == "暂停":
            raise ValueError(f"来源已暂停：{source.pause_reason or '请先恢复来源'}")
        if source.library_tier != "B":
            raise ValueError("只有 B 类来源可以执行试采")
        if not is_registered_trial_source(source):
            raise ValueError("来源没有已注册的试采适配器")
        if source.next_probe_at is not None and source.next_probe_at > started_at:
            raise ValueError(f"来源尚未到允许再次试采时间：{source.next_probe_at}")
        adapter = TRIAL_ADAPTERS[source.adapter_key]
        adapter_version = source.adapter_version
        if resume_existing:
            run_id = existing.run_id
            session.execute(
                delete(SourceTrialSample).where(SourceTrialSample.run_id == run_id)
            )
            existing.adapter_version = adapter_version
            existing.rule_version = RULE_VERSION
            existing.started_at = started_at
            existing.finished_at = None
            existing.state = "running"
            existing.budget_json = json.dumps(
                asdict(budget), ensure_ascii=False, separators=(",", ":")
            )
            existing.error_code = ""
            existing.error_message = ""
            existing.list_count = 0
            existing.detail_attempt_count = 0
            existing.detail_success_count = 0
            existing.candidate_count = 0
            existing.exclusion_summary = "{}"
            existing.requested_by = requested_by[:80]
        else:
            running = session.scalar(
                select(SourceTrialRun).where(
                    SourceTrialRun.source_id == source_id,
                    SourceTrialRun.state == "running",
                )
            )
            if running is not None:
                if running.started_at < started_at - STALE_RUN_AFTER:
                    running.state = "interrupted"
                    running.finished_at = started_at
                    running.error_code = "STALE_RUN"
                    running.error_message = "试采进程中断，已释放来源运行锁"
                    session.flush()
                else:
                    raise ValueError(f"该来源已有试采运行中：{running.run_id}")
            run_id = str(uuid4())
            session.add(
                SourceTrialRun(
                    run_id=run_id,
                    source_id=source.id,
                    adapter_version=adapter_version,
                    rule_version=RULE_VERSION,
                    started_at=started_at,
                    state="running",
                    budget_json=json.dumps(asdict(budget), ensure_ascii=False, separators=(",", ":")),
                    requested_by=requested_by[:80],
                    idempotency_key=idempotency_key,
                )
            )
        source.validation_state = "试采中"
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            winner = session.scalar(
                select(SourceTrialRun).where(
                    SourceTrialRun.source_id == source_id,
                    SourceTrialRun.idempotency_key == idempotency_key,
                )
            )
            if winner is not None:
                return winner.run_id
            raise ValueError("该来源已有另一个试采任务运行中") from exc

    terminal_error: Exception | None = None
    try:
        collection = adapter(client, budget)
    except PartialTrialError as exc:
        collection = exc.collection
        terminal_error = exc.cause
    except Exception as exc:
        error_code = _classify_exception(exc)
        with session_factory() as session:
            run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
            source = session.get(Source, source_id)
            run.state = "failed"
            run.finished_at = now or datetime.now()
            run.error_code = error_code
            run.error_message = str(exc)[:1000]
            if error_code == "ACCESS_RESTRICTED":
                source.validation_state = "受限"
            elif error_code == "RATE_LIMITED":
                source.next_probe_at = _retry_after_time(exc, now or datetime.now())
            else:
                source.validation_state = "待修复"
            session.commit()
        raise

    try:
        decisions = []
        samples: list[SourceTrialSample] = []
        with session_factory() as session:
            seen_identity_keys = set(
                session.scalars(
                    select(SourceTrialSample.identity_key).where(
                        SourceTrialSample.run_id == run_id
                    )
                ).all()
            )
        for candidate in collection.candidates:
            if candidate.identity_key in seen_identity_keys:
                continue
            seen_identity_keys.add(candidate.identity_key)
            decision = evaluate_candidate(candidate, now=started_at)
            decisions.append(decision)
            samples.append(_sample_from_candidate(run_id, candidate, decision))
        for failure in collection.failures:
            if failure.identity_key in seen_identity_keys:
                continue
            seen_identity_keys.add(failure.identity_key)
            samples.append(_sample_from_failure(run_id, failure))
        for sample in samples:
            with session_factory() as session:
                session.add(sample)
                session.commit()

        with session_factory() as session:
            run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
            source = session.get(Source, source_id)
            saved_samples = session.scalars(
                select(SourceTrialSample).where(SourceTrialSample.run_id == run_id)
            ).all()
            reason_counts: Counter[str] = Counter()
            for sample in saved_samples:
                if sample.decision == "excluded":
                    reason_counts.update(json.loads(sample.reason_codes or "[]"))
            state = (
                "failed" if terminal_error is not None
                else "empty" if collection.list_count == 0
                else "partial" if collection.failures and collection.detail_success_count
                else "failed" if collection.failures
                else "completed"
            )
            run.finished_at = now or datetime.now()
            run.state = state
            run.list_count = collection.list_count
            run.detail_attempt_count = collection.detail_attempt_count
            run.detail_success_count = collection.detail_success_count
            run.candidate_count = sum(
                sample.decision == "qualified" for sample in saved_samples
            )
            run.exclusion_summary = json.dumps(reason_counts, ensure_ascii=False, separators=(",", ":"))
            if terminal_error is not None:
                error_code = _classify_exception(terminal_error)
                run.error_code = error_code
                run.error_message = str(terminal_error)[:1000]
                if error_code == "ACCESS_RESTRICTED":
                    source.validation_state = "受限"
                elif error_code == "RATE_LIMITED":
                    source.next_probe_at = _retry_after_time(
                        terminal_error, now or datetime.now()
                    )
                else:
                    source.validation_state = "待修复"
            elif collection.failures:
                run.error_code = "DETAIL_FAILURE"
                run.error_message = f"{len(collection.failures)} 个详情失败"
            session.commit()
    except Exception as exc:
        with session_factory() as session:
            run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
            run.state = "failed"
            run.finished_at = now or datetime.now()
            run.error_code = "RESULT_PERSIST_FAILED"
            run.error_message = str(exc)[:1000]
            session.commit()
        raise
    return run_id
