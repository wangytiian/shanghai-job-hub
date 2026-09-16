"""Deterministic B-source admission checks and audited approval."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Source, SourceTrialRun, SourceTrialSample, TaskRun, User, WorkItem
from app.services.audit import record_event
from app.services.source_candidate_policy import RULE_VERSION
from app.services.source_trials import is_registered_trial_source
from app.services.work_queue import ACTIVE_STATUSES, enqueue


MIN_VALID_RUNS = 3
MIN_RUN_INTERVAL = timedelta(hours=8)
MIN_NATURAL_DAYS = 2
MIN_DETAIL_SUCCESS_RATE = 0.90
MIN_UNIQUE_ANNOUNCEMENTS = 10
MIN_UNIQUE_QUALIFIED_SAMPLES = 3
PASSING_RUN_STATES = {"completed", "partial"}


class AdmissionError(ValueError):
    """Raised when a source cannot safely be admitted."""


@dataclass(frozen=True)
class AdmissionBlocker:
    code: str
    message: str


@dataclass(frozen=True)
class AdmissionResult:
    eligible: bool
    run_ids: tuple[str, ...]
    blocking_reasons: tuple[AdmissionBlocker, ...]
    sample_ids: tuple[int, ...]
    latest_run: SourceTrialRun | None


def enqueue_source_trial(
    session: Session, source: Source, requested_by: int | None
) -> WorkItem:
    """Queue one bounded source trial and reuse any active task for that source."""
    if source.library_tier != "B":
        raise AdmissionError("只有 B 类来源可以执行试采")
    if source.status == "暂停":
        raise AdmissionError(f"来源已暂停：{source.pause_reason or '请先恢复来源'}")
    if not is_registered_trial_source(source):
        raise AdmissionError("来源没有已注册的试采适配器")
    dedupe_key = f"source_trial:{source.id}"
    existing = session.scalar(
        select(WorkItem).where(
            WorkItem.dedupe_key == dedupe_key,
            WorkItem.status.in_(ACTIVE_STATUSES),
        )
    )
    if existing is not None:
        return existing
    task = TaskRun(
        task_name=f"B 类来源试采：{source.name}",
        status="已入队",
        message="已提交后台试采；结果仅写入试采报告，不进入正式岗位库。",
    )
    session.add(task)
    session.flush()
    return enqueue(
        session,
        kind="source_trial",
        target_type="source",
        target_id=source.id,
        expected_row_version=0,
        expected_fact_version=0,
        config_revision=source.adapter_version,
        template_version=RULE_VERSION,
        dedupe_key=dedupe_key,
        requested_by=requested_by,
        batch_id=task.id,
    )


def _add_blocker(
    blockers: list[AdmissionBlocker], seen: set[str], code: str, message: str
) -> None:
    if code not in seen:
        blockers.append(AdmissionBlocker(code, message))
        seen.add(code)


def evaluate_source_admission(
    session: Session,
    source_id: int,
    now: datetime | None = None,
    *,
    lock_rows: bool = False,
) -> AdmissionResult:
    """Evaluate the latest consecutive run window against the fixed admission rules."""
    del now  # Kept in the contract so future time-sensitive rules remain deterministic.
    source = session.get(Source, source_id)
    if source is None:
        raise AdmissionError("来源不存在")

    runs_query = (
        select(SourceTrialRun)
        .where(
            SourceTrialRun.source_id == source_id,
            SourceTrialRun.finished_at.is_not(None),
        )
        .order_by(SourceTrialRun.finished_at, SourceTrialRun.id)
    )
    if lock_rows:
        runs_query = runs_query.with_for_update()
    all_runs = session.scalars(runs_query).all()
    latest_run = all_runs[-1] if all_runs else None
    selected = all_runs[-MIN_VALID_RUNS:]
    blockers: list[AdmissionBlocker] = []
    seen: set[str] = set()

    if len(selected) < MIN_VALID_RUNS:
        _add_blocker(
            blockers,
            seen,
            "CONSECUTIVE_VALID_RUNS_INSUFFICIENT",
            f"还需完成 {MIN_VALID_RUNS - len(selected)} 轮合格试采",
        )

    samples_by_run: dict[str, list[SourceTrialSample]] = {}
    if selected:
        samples = session.scalars(
            select(SourceTrialSample).where(
                SourceTrialSample.run_id.in_([run.run_id for run in selected])
            )
        ).all()
        for sample in samples:
            samples_by_run.setdefault(sample.run_id, []).append(sample)

    round_level_valid = len(selected) == MIN_VALID_RUNS
    for run in selected:
        if run.state not in PASSING_RUN_STATES:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "CONSECUTIVE_VALID_RUNS_INSUFFICIENT",
                "最近三轮中存在失败、空结果或中断轮次，需重新累计连续合格轮次",
            )
        if run.adapter_version != source.adapter_version:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "ADAPTER_VERSION_MISMATCH",
                "最近三轮使用的适配器版本不一致",
            )
        if run.rule_version != RULE_VERSION:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "RULE_VERSION_MISMATCH",
                "最近三轮使用的准入规则版本不一致",
            )
        if run.detail_attempt_count <= 0:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "DETAIL_ATTEMPTS_ZERO",
                "至少一轮没有实际尝试公告详情",
            )
        elif run.detail_success_count / run.detail_attempt_count < MIN_DETAIL_SUCCESS_RATE:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "DETAIL_SUCCESS_RATE_LOW",
                "每轮公告详情成功率必须达到 90%",
            )
        qualified = {
            sample.identity_key
            for sample in samples_by_run.get(run.run_id, ())
            if sample.decision == "qualified"
        }
        if not qualified:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "ROUND_QUALIFIED_SAMPLE_MISSING",
                "每轮至少需要 1 条合格岗位",
            )

    if len(selected) == MIN_VALID_RUNS:
        for previous, current in zip(selected, selected[1:]):
            if current.finished_at - previous.finished_at < MIN_RUN_INTERVAL:
                round_level_valid = False
                _add_blocker(
                    blockers,
                    seen,
                    "RUN_INTERVAL_TOO_SHORT",
                    "相邻合格试采至少间隔 8 小时",
                )
        natural_days = {run.finished_at.date() for run in selected}
        if len(natural_days) < MIN_NATURAL_DAYS:
            round_level_valid = False
            _add_blocker(
                blockers,
                seen,
                "NATURAL_DAY_COVERAGE_INSUFFICIENT",
                "三轮试采需要覆盖至少 2 个自然日",
            )

    successful_samples = [
        sample
        for run in selected
        for sample in samples_by_run.get(run.run_id, ())
        if sample.decision in {"qualified", "needs_evidence", "excluded"}
    ]
    unique_announcements = {
        sample.announcement_id for sample in successful_samples if sample.announcement_id
    }
    if len(unique_announcements) < MIN_UNIQUE_ANNOUNCEMENTS:
        _add_blocker(
            blockers,
            seen,
            "UNIQUE_ANNOUNCEMENT_COUNT_INSUFFICIENT",
            f"三轮累计需至少 {MIN_UNIQUE_ANNOUNCEMENTS} 个不同公告详情",
        )
    qualified_samples = [
        sample for sample in successful_samples if sample.decision == "qualified"
    ]
    unique_qualified = {sample.identity_key for sample in qualified_samples}
    if len(unique_qualified) < MIN_UNIQUE_QUALIFIED_SAMPLES:
        _add_blocker(
            blockers,
            seen,
            "QUALIFIED_SAMPLE_COUNT_INSUFFICIENT",
            f"三轮累计需至少 {MIN_UNIQUE_QUALIFIED_SAMPLES} 条不同合格岗位",
        )

    eligible = round_level_valid and not blockers
    return AdmissionResult(
        eligible=eligible,
        run_ids=tuple(run.run_id for run in selected) if round_level_valid else (),
        blocking_reasons=tuple(blockers),
        sample_ids=tuple(sample.id for sample in qualified_samples),
        latest_run=latest_run,
    )


def approve_source_admission(
    session: Session,
    source_id: int,
    actor_id: int | None,
    expected_version: str,
    *,
    expected_rule_version: str = RULE_VERSION,
    now: datetime | None = None,
    request_id: str = "",
) -> Source:
    """Recheck evidence and atomically admit an eligible B source."""
    approved_at = now or datetime.now()
    source = session.scalar(
        select(Source).where(Source.id == source_id).with_for_update()
    )
    if source is None:
        raise AdmissionError("来源不存在")
    actor = session.get(User, actor_id) if actor_id is not None else None
    if actor_id is not None and (
        actor is None or not actor.is_active or actor.role != "admin"
    ):
        raise AdmissionError("只有管理员可以验收启用来源")
    if expected_version != source.adapter_version:
        raise AdmissionError("适配器版本已变化，请刷新页面后重新验收")
    if expected_rule_version != RULE_VERSION:
        raise AdmissionError("准入规则版本已变化，请刷新页面后重新验收")
    if source.library_tier != "B":
        raise AdmissionError("只有 B 类来源可以执行验收启用")
    result = evaluate_source_admission(
        session, source_id, approved_at, lock_rows=True
    )
    if not result.eligible:
        reasons = "；".join(reason.message for reason in result.blocking_reasons)
        raise AdmissionError(f"来源尚未满足验收条件：{reasons}")

    actor_label = actor.display_name if actor else "本地管理员"
    updated = session.execute(
        update(Source)
        .where(
            Source.id == source_id,
            Source.library_tier == "B",
            Source.adapter_version == expected_version,
        )
        .values(
            library_tier="A",
            validation_state="已验收",
            is_enabled=True,
            validated_adapter_version=expected_version,
            validated_rule_version=RULE_VERSION,
            validated_at=approved_at,
            validated_by=actor_label,
        ),
        execution_options={"synchronize_session": False},
    )
    if updated.rowcount != 1:
        session.rollback()
        raise AdmissionError("来源状态或版本发生并发变化，请刷新页面后重新验收")
    record_event(
        session,
        actor_user_id=actor.id if actor else None,
        actor_label=actor_label,
        action="source.admission_approved",
        entity_type="source",
        entity_id=str(source.id),
        request_id=request_id,
        changes={
            "library_tier": "A",
            "is_enabled": True,
            "adapter_version": source.adapter_version,
            "rule_version": RULE_VERSION,
            "run_ids": list(result.run_ids),
        },
    )
    session.commit()
    session.refresh(source)
    return source
