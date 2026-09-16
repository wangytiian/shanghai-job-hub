from datetime import datetime, timedelta
import json

import pytest

from app.auth.service import create_user
from app.models import AuditEvent, Source, SourceTrialRun, SourceTrialSample
from app.services.source_candidate_policy import RULE_VERSION


def _source(session, *, adapter_version="adapter-v1"):
    source = Source(
        name="准入测试来源",
        source_key="admission-test-source",
        url="https://career.example.edu/jobs",
        level="一级",
        source_type="高校就业平台",
        adapter_key="test_adapter",
        library_tier="B",
        validation_state="试采中",
        adapter_version=adapter_version,
        is_enabled=False,
        status="正常",
    )
    session.add(source)
    session.commit()
    return source


def _add_run(
    session,
    source,
    *,
    run_id,
    finished_at,
    attempts=10,
    successes=9,
    qualified=("job-1",),
    announcements=None,
    state="completed",
    adapter_version=None,
    rule_version=RULE_VERSION,
):
    announcements = announcements or [f"{run_id}-notice-{index}" for index in range(4)]
    run = SourceTrialRun(
        run_id=run_id,
        source_id=source.id,
        adapter_version=adapter_version or source.adapter_version,
        rule_version=rule_version,
        started_at=finished_at - timedelta(minutes=5),
        finished_at=finished_at,
        state=state,
        detail_attempt_count=attempts,
        detail_success_count=successes,
        candidate_count=len(qualified),
        requested_by="tester",
        idempotency_key=f"task-{run_id}",
    )
    session.add(run)
    session.flush()
    for index, announcement_id in enumerate(announcements):
        identity_key = qualified[index] if index < len(qualified) else f"excluded-{run_id}-{index}"
        decision = "qualified" if index < len(qualified) else "excluded"
        session.add(
            SourceTrialSample(
                run_id=run_id,
                identity_key=identity_key,
                announcement_id=announcement_id,
                job_id=identity_key,
                employer_name="示例公司",
                title="实习生",
                location_category="明确上海",
                location_detail="上海",
                evidence_text="岗位职责和任职条件",
                source_url=f"https://example.test/{announcement_id}",
                application_kind="email",
                application_value="jobs@example.test",
                application_evidence="投递邮箱 jobs@example.test",
                decision=decision,
                reason_codes="[]" if decision == "qualified" else json.dumps(["NOT_STUDENT_FIT"]),
                content_hash=f"hash-{run_id}-{index}",
            )
        )
    session.commit()
    return run


def _three_valid_runs(session, source):
    _add_run(
        session,
        source,
        run_id="run-1",
        finished_at=datetime(2026, 9, 10, 9, 0),
        qualified=("job-a",),
        announcements=["notice-1", "notice-2", "notice-3", "notice-4"],
    )
    _add_run(
        session,
        source,
        run_id="run-2",
        finished_at=datetime(2026, 9, 10, 18, 0),
        qualified=("job-b",),
        announcements=["notice-5", "notice-6", "notice-7"],
    )
    _add_run(
        session,
        source,
        run_id="run-3",
        finished_at=datetime(2026, 9, 11, 9, 0),
        qualified=("job-c",),
        announcements=["notice-8", "notice-9", "notice-10"],
    )


def test_three_version_matched_rounds_across_two_days_are_eligible(session):
    from app.services.source_admission import evaluate_source_admission

    source = _source(session)
    _three_valid_runs(session, source)

    result = evaluate_source_admission(session, source.id, datetime(2026, 9, 11, 10, 0))

    assert result.eligible is True
    assert result.run_ids == ("run-1", "run-2", "run-3")
    assert len(result.sample_ids) == 3
    assert result.blocking_reasons == ()


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("too_close", "RUN_INTERVAL_TOO_SHORT"),
        ("one_day", "NATURAL_DAY_COVERAGE_INSUFFICIENT"),
        ("adapter_changed", "ADAPTER_VERSION_MISMATCH"),
        ("rule_changed", "RULE_VERSION_MISMATCH"),
        ("zero_attempts", "DETAIL_ATTEMPTS_ZERO"),
        ("low_success", "DETAIL_SUCCESS_RATE_LOW"),
        ("duplicate_job", "QUALIFIED_SAMPLE_COUNT_INSUFFICIENT"),
        ("too_few_announcements", "UNIQUE_ANNOUNCEMENT_COUNT_INSUFFICIENT"),
    ],
)
def test_admission_reports_each_blocker_without_relaxing_thresholds(session, mutation, reason_code):
    from app.services.source_admission import evaluate_source_admission

    source = _source(session)
    _three_valid_runs(session, source)
    runs = session.query(SourceTrialRun).order_by(SourceTrialRun.finished_at).all()
    if mutation == "too_close":
        runs[1].finished_at = runs[0].finished_at + timedelta(hours=7)
    elif mutation == "one_day":
        runs[2].finished_at = datetime(2026, 9, 10, 23, 0)
    elif mutation == "adapter_changed":
        runs[1].adapter_version = "adapter-old"
    elif mutation == "rule_changed":
        runs[1].rule_version = "rule-old"
    elif mutation == "zero_attempts":
        runs[1].detail_attempt_count = 0
        runs[1].detail_success_count = 0
    elif mutation == "low_success":
        runs[1].detail_success_count = 8
    elif mutation == "duplicate_job":
        samples = session.query(SourceTrialSample).filter_by(decision="qualified").order_by(SourceTrialSample.id).all()
        for sample in samples:
            sample.identity_key = "same-job"
            sample.job_id = "same-job"
    elif mutation == "too_few_announcements":
        samples = session.query(SourceTrialSample).all()
        for index, sample in enumerate(samples):
            sample.announcement_id = f"notice-{index % 9}"
    session.commit()

    result = evaluate_source_admission(session, source.id, datetime(2026, 9, 11, 10, 0))

    assert result.eligible is False
    assert reason_code in {reason.code for reason in result.blocking_reasons}


def test_a_failed_latest_round_breaks_the_consecutive_sequence(session):
    from app.services.source_admission import evaluate_source_admission

    source = _source(session)
    _three_valid_runs(session, source)
    _add_run(
        session,
        source,
        run_id="run-4",
        finished_at=datetime(2026, 9, 11, 18, 0),
        state="failed",
        qualified=(),
        announcements=["notice-11"],
        attempts=1,
        successes=0,
    )

    result = evaluate_source_admission(session, source.id, datetime(2026, 9, 11, 19, 0))

    assert result.eligible is False
    assert result.run_ids == ()
    assert "CONSECUTIVE_VALID_RUNS_INSUFFICIENT" in {reason.code for reason in result.blocking_reasons}


def test_approval_rechecks_evidence_updates_source_and_writes_audit_event(session):
    from app.services.source_admission import approve_source_admission

    source = _source(session)
    _three_valid_runs(session, source)
    admin = create_user(
        session,
        "admin",
        "A secure password",
        "管理员",
        role="admin",
        must_change_password=False,
    )

    approved = approve_source_admission(
        session,
        source.id,
        actor_id=admin.id,
        expected_version="adapter-v1",
        now=datetime(2026, 9, 11, 10, 0),
        request_id="request-1",
    )

    assert approved.library_tier == "A"
    assert approved.is_enabled is True
    assert approved.validation_state == "已验收"
    assert approved.validated_adapter_version == "adapter-v1"
    assert approved.validated_rule_version == RULE_VERSION
    assert approved.validated_by == "管理员"
    event = session.query(AuditEvent).one()
    assert event.action == "source.admission_approved"
    assert json.loads(event.changes)["run_ids"] == ["run-1", "run-2", "run-3"]


def test_approval_rejects_non_admin_stale_version_and_ineligible_source(session):
    from app.services.source_admission import AdmissionError, approve_source_admission

    source = _source(session)
    member = create_user(
        session,
        "member",
        "A secure password",
        "普通成员",
        must_change_password=False,
    )
    with pytest.raises(AdmissionError, match="管理员"):
        approve_source_admission(session, source.id, member.id, "adapter-v1")
    with pytest.raises(AdmissionError, match="版本"):
        approve_source_admission(session, source.id, None, "adapter-old")
    with pytest.raises(AdmissionError, match="尚未满足"):
        approve_source_admission(session, source.id, None, "adapter-v1")

    session.refresh(source)
    assert source.library_tier == "B"
    assert source.is_enabled is False
    assert session.query(AuditEvent).count() == 0


def test_approval_rejects_stale_rule_version_from_an_old_page(session):
    from app.services.source_admission import AdmissionError, approve_source_admission

    source = _source(session)
    _three_valid_runs(session, source)

    with pytest.raises(AdmissionError, match="规则版本"):
        approve_source_admission(
            session,
            source.id,
            actor_id=None,
            expected_version=source.adapter_version,
            expected_rule_version="old-rule",
        )

    session.refresh(source)
    assert source.library_tier == "B"
    assert source.is_enabled is False


def test_approval_conditional_update_rejects_concurrent_adapter_change(tmp_path, monkeypatch):
    from app.database import create_database
    from app.services import source_admission
    from app.services.source_admission import AdmissionError, approve_source_admission

    database_url = f"sqlite:///{(tmp_path / 'admission-race.db').as_posix()}"
    session_factory = create_database(database_url)
    with session_factory() as setup_session:
        source = _source(setup_session)
        _three_valid_runs(setup_session, source)
        source_id = source.id

    real_evaluate = source_admission.evaluate_source_admission

    def evaluate_then_change_adapter(session, target_source_id, now=None, **kwargs):
        result = real_evaluate(session, target_source_id, now, **kwargs)
        with session_factory() as concurrent_session:
            concurrent_source = concurrent_session.get(Source, target_source_id)
            concurrent_source.adapter_version = "adapter-v2"
            concurrent_session.commit()
        return result

    monkeypatch.setattr(
        source_admission, "evaluate_source_admission", evaluate_then_change_adapter
    )
    with session_factory() as approval_session:
        with pytest.raises(AdmissionError, match="并发|版本"):
            approve_source_admission(
                approval_session,
                source_id,
                actor_id=None,
                expected_version="adapter-v1",
            )

    with session_factory() as check_session:
        source = check_session.get(Source, source_id)
        assert source.adapter_version == "adapter-v2"
        assert source.library_tier == "B"
        assert source.is_enabled is False
        assert check_session.query(AuditEvent).count() == 0
