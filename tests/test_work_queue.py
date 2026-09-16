from app.auth.service import create_user
from datetime import datetime, timedelta

from app.database import create_database
from app.models import Source, SourceTrialRun, WorkItem
from app.services.work_queue import claim_next, complete, enqueue, heartbeat, recover_expired_leases


def _trial_source(session):
    source = Source(
        name="队列试采来源",
        source_key="sjtu-internships",
        url="https://example.test/jobs",
        level="一级",
        source_type="高校就业平台",
        adapter_key="sjtu_internship_json",
        library_tier="B",
        validation_state="待适配",
        adapter_version="adapter-v1",
        is_enabled=False,
        status="正常",
    )
    session.add(source)
    session.commit()
    return source


def test_enqueue_deduplicates_active_work_item(session):
    user = create_user(session, "member", "A secure password", "成员")
    first = enqueue(
        session, kind="score_job", target_type="job", target_id=1, expected_row_version=1,
        expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:1:1:r1",
        requested_by=user.id,
    )
    second = enqueue(
        session, kind="score_job", target_type="job", target_id=1, expected_row_version=1,
        expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:1:1:r1",
        requested_by=user.id,
    )

    assert second.id == first.id


def test_enqueue_uses_the_callers_transaction_instead_of_committing_early(session):
    user = create_user(session, "member", "A secure password", "成员")
    item = enqueue(
        session, kind="score_job", target_type="job", target_id=1, expected_row_version=1,
        expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:rollback",
        requested_by=user.id,
    )

    session.rollback()

    assert session.get(WorkItem, item.id) is None


def test_claimed_work_requires_current_lease_token_to_complete(session):
    user = create_user(session, "member", "A secure password", "成员")
    work = enqueue(
        session, kind="score_job", target_type="job", target_id=1, expected_row_version=1,
        expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:1:1:r1",
        requested_by=user.id,
    )

    claimed = claim_next(session)

    assert claimed.id == work.id
    assert complete(session, work.id, "wrong-token", {"score": 20}) is False
    assert complete(session, work.id, claimed.lease_token, {"score": 20}) is True


def test_heartbeat_extends_only_the_current_worker_lease(session):
    user = create_user(session, "member", "A secure password", "成员")
    work = enqueue(session, kind="score_job", target_type="job", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:heartbeat", requested_by=user.id)
    claimed = claim_next(session, lease_seconds=1)
    original_lease = claimed.lease_until

    assert heartbeat(session, work.id, "wrong-token", lease_seconds=120) is False
    assert heartbeat(session, work.id, claimed.lease_token, lease_seconds=120) is True
    assert session.get(WorkItem, work.id).lease_until > original_lease


def test_recover_expired_lease_retries_then_escalates_after_max_attempts(session):
    user = create_user(session, "member", "A secure password", "成员")
    retry = enqueue(session, kind="score_job", target_type="job", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:retry", requested_by=user.id)
    escalate = enqueue(session, kind="score_job", target_type="job", target_id=2, expected_row_version=1, expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="score:escalate", requested_by=user.id)
    for item, attempts in ((retry, 1), (escalate, 3)):
        item.status = "running"
        item.attempts = attempts
        item.lease_until = datetime.now() - timedelta(seconds=1)
    session.commit()

    assert recover_expired_leases(session, max_attempts=3) == 2
    assert session.get(WorkItem, retry.id).status == "queued"
    assert session.get(WorkItem, retry.id).available_at > datetime.now()
    assert session.get(WorkItem, escalate.id).status == "needs_review"


def test_source_trial_enqueue_deduplicates_repeated_clicks_and_creates_one_batch(session):
    from app.services.source_admission import enqueue_source_trial

    user = create_user(session, "operator", "A secure password", "运营")
    source = _trial_source(session)

    first = enqueue_source_trial(session, source, user.id)
    second = enqueue_source_trial(session, source, user.id)

    assert first.id == second.id
    assert first.batch_id is not None
    assert session.query(WorkItem).filter_by(kind="source_trial").count() == 1


def test_source_trial_worker_marks_interrupted_owned_run_for_retry(monkeypatch):
    from app.services import source_trials
    from app.services.source_admission import enqueue_source_trial
    from app.services.source_trials import TrialCollection
    from app.worker import run_once

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "sjtu_internship_json",
        lambda *_: TrialCollection(0, 0, 0, (), ()),
    )
    with session_factory() as session:
        user = create_user(session, "operator", "A secure password", "运营")
        source = _trial_source(session)
        item = enqueue_source_trial(session, source, user.id)
        session.commit()
        session.add(
            SourceTrialRun(
                run_id="worker-owned-run",
                source_id=source.id,
                adapter_version=source.adapter_version,
                rule_version="source-candidate-v1",
                started_at=datetime.now(),
                state="running",
                idempotency_key=f"work-item:{item.id}",
            )
        )
        session.commit()
        item_id = item.id

    assert run_once(session_factory) is True

    with session_factory() as session:
        item = session.get(WorkItem, item_id)
        run = session.query(SourceTrialRun).filter_by(run_id="worker-owned-run").one()
        assert item.status == "succeeded"
        assert run.state == "empty"


def test_source_trial_retry_does_not_turn_a_failed_run_into_false_success(monkeypatch):
    from app.services import source_trials
    from app.services.source_admission import enqueue_source_trial
    from app.worker import run_once

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    calls = []

    def failing_adapter(*_args):
        calls.append(True)
        raise RuntimeError("temporary upstream failure")

    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "sjtu_internship_json", failing_adapter)
    with session_factory() as session:
        user = create_user(session, "operator", "A secure password", "运营")
        source = _trial_source(session)
        item = enqueue_source_trial(session, source, user.id)
        session.commit()
        item_id = item.id

    assert run_once(session_factory) is True
    with session_factory() as session:
        item = session.get(WorkItem, item_id)
        item.available_at = datetime.now() - timedelta(seconds=1)
        session.commit()
    assert run_once(session_factory) is True

    with session_factory() as session:
        item = session.get(WorkItem, item_id)
        assert item.status != "succeeded"
        assert session.query(SourceTrialRun).one().state == "failed"
    assert calls == [True, True]


def test_source_trial_worker_never_marks_a_failed_trial_run_succeeded(monkeypatch):
    from app.services import source_trials
    from app.services.source_admission import enqueue_source_trial
    from app.services.source_trials import PartialTrialError, TrialCollection
    from app.worker import run_once

    class RateLimited(Exception):
        response = type(
            "Response", (), {"status_code": 429, "headers": {"Retry-After": "60"}}
        )()

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "sjtu_internship_json",
        lambda *_: (_ for _ in ()).throw(
            PartialTrialError(TrialCollection(3, 2, 1, (), ()), RateLimited("limited"))
        ),
    )
    with session_factory() as session:
        user = create_user(session, "operator", "A secure password", "运营")
        source = _trial_source(session)
        item = enqueue_source_trial(session, source, user.id)
        session.commit()
        item_id = item.id

    assert run_once(session_factory) is True

    with session_factory() as session:
        item = session.get(WorkItem, item_id)
        run = session.query(SourceTrialRun).one()
        assert run.state == "failed"
        assert item.status == "needs_review"
        assert item.error_code == "trial_failed"
