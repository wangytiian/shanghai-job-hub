import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import create_database
from app.models import Job, Source, SourceTrialRun, SourceTrialSample


def _source(session, *, status="正常"):
    source = Source(
        name="试采来源",
        source_key="test-source",
        url="https://career.example.edu/jobs",
        level="一级",
        source_type="高校就业平台",
        adapter_key="test_adapter",
        library_tier="B",
        validation_state="待适配",
        adapter_version="test-v1",
        is_enabled=False,
        status=status,
    )
    session.add(source)
    session.commit()
    return source


def _qualified_candidate():
    from app.services.source_candidate_policy import SourceCandidate

    return SourceCandidate(
        identity_key="notice-1:role-1",
        announcement_id="notice-1",
        job_id="role-1",
        employer_name="上海示例科技有限公司",
        title="数据分析实习生",
        location_category="明确上海",
        location_detail="上海市杨浦区",
        published_at="2026-09-10",
        deadline="",
        evidence_text="岗位职责：整理数据。任职要求：在校生，工作地点：上海。官方投递邮箱：apply@example.com",
        source_url="https://career.example.edu/detail/notice-1",
        application_kind="email",
        application_value="apply@example.com",
        application_evidence="官方投递邮箱：apply@example.com",
    )


def test_b_trial_persists_report_and_samples_without_creating_jobs_or_changing_enable_state(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source = _source(session)
        source_id = source.id
    calls = []

    def adapter(_client, _budget):
        calls.append(True)
        return TrialCollection(
            list_count=1,
            detail_attempt_count=1,
            detail_success_count=1,
            candidates=(_qualified_candidate(),),
            failures=(),
        )

    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "test_adapter", adapter)
    run_id = run_source_trial(
        session_factory,
        object(),
        source_id,
        requested_by="tester",
        budget=TrialBudget(pages=1, list_limit=10, detail_limit=3),
        idempotency_key="task-1",
        now=datetime(2026, 9, 11, 9, 0),
    )

    with session_factory() as session:
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
        samples = session.scalars(select(SourceTrialSample).where(SourceTrialSample.run_id == run_id)).all()
        saved_source = session.get(Source, source_id)
        assert session.query(Job).count() == 0
    assert calls == [True]
    assert run.state == "completed"
    assert (run.list_count, run.detail_attempt_count, run.detail_success_count, run.candidate_count) == (1, 1, 1, 1)
    assert json.loads(run.exclusion_summary) == {}
    assert len(samples) == 1
    assert samples[0].decision == "qualified"
    assert saved_source.is_enabled is False
    assert saved_source.library_tier == "B"
    assert saved_source.validation_state == "试采中"


def test_same_idempotency_key_reuses_run_without_calling_adapter_or_duplicating_samples(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    calls = []

    def adapter(_client, _budget):
        calls.append(True)
        return TrialCollection(1, 1, 1, (_qualified_candidate(),), ())

    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "test_adapter", adapter)
    kwargs = dict(
        requested_by="tester",
        budget=TrialBudget(),
        idempotency_key="same-task",
        now=datetime(2026, 9, 11, 9, 0),
    )
    first = run_source_trial(session_factory, object(), source_id, **kwargs)
    second = run_source_trial(session_factory, object(), source_id, **kwargs)

    with session_factory() as session:
        assert session.query(SourceTrialRun).count() == 1
        assert session.query(SourceTrialSample).count() == 1
    assert first == second
    assert calls == [True]


def test_suspended_source_is_blocked_before_an_adapter_request(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session, status="暂停").id
    calls = []
    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "test_adapter", lambda *_: calls.append(True))

    with pytest.raises(ValueError, match="暂停"):
        run_source_trial(
            session_factory,
            object(),
            source_id,
            requested_by="tester",
            budget=TrialBudget(),
            idempotency_key="blocked",
        )

    assert calls == []
    with session_factory() as session:
        assert session.query(SourceTrialRun).count() == 0


def test_trial_budget_rejects_unbounded_values():
    from app.services.source_trials import TrialBudget

    with pytest.raises(ValueError, match="页数"):
        TrialBudget(pages=4)
    with pytest.raises(ValueError, match="详情"):
        TrialBudget(detail_limit=11)
    with pytest.raises(ValueError, match="总执行"):
        TrialBudget(total_seconds=601)


def test_empty_valid_result_is_persisted_but_not_counted_as_successful_candidates(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "test_adapter", lambda *_: TrialCollection(0, 0, 0, (), ()))

    run_id = run_source_trial(
        session_factory, object(), source_id, "tester", TrialBudget(), "empty-result"
    )

    with session_factory() as session:
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
    assert run.state == "empty"
    assert run.candidate_count == 0


def test_rate_limit_failure_is_persisted_and_sets_a_future_probe_time(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    class RateLimited(Exception):
        response = type("Response", (), {"status_code": 429})()

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: (_ for _ in ()).throw(RateLimited("too many requests")),
    )
    started = datetime(2026, 9, 11, 9, 0)

    with pytest.raises(RateLimited):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "rate-limit", now=started
        )

    with session_factory() as session:
        run = session.query(SourceTrialRun).one()
        source = session.get(Source, source_id)
    assert run.state == "failed"
    assert run.error_code == "RATE_LIMITED"
    assert source.next_probe_at == datetime(2026, 9, 11, 10, 0)


def test_login_page_disguised_as_success_is_recorded_as_parse_failure(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: (_ for _ in ()).throw(ValueError("200 响应实际为登录页面")),
    )

    with pytest.raises(ValueError, match="登录页面"):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "login-page"
        )

    with session_factory() as session:
        run = session.query(SourceTrialRun).one()
    assert run.error_code == "PARSE_FAILED"


def test_post_fetch_policy_failure_finalizes_run_instead_of_leaving_it_running(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: TrialCollection(1, 1, 1, (_qualified_candidate(),), ()),
    )
    monkeypatch.setattr(
        source_trials,
        "evaluate_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad candidate")),
    )

    with pytest.raises(ValueError, match="bad candidate"):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "policy-failure"
        )

    with session_factory() as session:
        run = session.query(SourceTrialRun).one()
    assert run.state == "failed"
    assert run.finished_at is not None


def test_idempotency_key_longer_than_storage_limit_is_rejected_before_adapter_call(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    calls = []
    monkeypatch.setitem(source_trials.TRIAL_ADAPTERS, "test_adapter", lambda *_: calls.append(True))

    with pytest.raises(ValueError, match="幂等键"):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "x" * 121
        )

    assert calls == []


def test_arbitrary_source_key_cannot_reuse_a_registered_adapter(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source = _source(session)
        source.adapter_key = "sjtu_internship_json"
        session.commit()
        source_id = source.id

    with pytest.raises(ValueError, match="已注册"):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "wrong-source-key"
        )


def test_database_allows_only_one_running_trial_per_source():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
        first = SourceTrialRun(
            run_id="run-one",
            source_id=source_id,
            adapter_version="v1",
            rule_version="v1",
            state="running",
            idempotency_key="task-one",
        )
        session.add(first)
        session.commit()
        session.add(
            SourceTrialRun(
                run_id="run-two",
                source_id=source_id,
                adapter_version="v1",
                rule_version="v1",
                state="running",
                idempotency_key="task-two",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_stale_running_trial_is_marked_interrupted_before_a_new_run(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
        session.add(
            SourceTrialRun(
                run_id="stale-run",
                source_id=source_id,
                adapter_version="v1",
                rule_version="v1",
                started_at=datetime(2026, 9, 11, 8, 0),
                state="running",
                idempotency_key="stale-task",
            )
        )
        session.commit()
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: TrialCollection(0, 0, 0, (), ()),
    )

    new_run_id = run_source_trial(
        session_factory,
        object(),
        source_id,
        "tester",
        TrialBudget(),
        "new-task",
        now=datetime(2026, 9, 11, 9, 0),
    )

    with session_factory() as session:
        stale = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == "stale-run"))
        new = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == new_run_id))
    assert stale.state == "interrupted"
    assert stale.error_code == "STALE_RUN"
    assert new.state == "empty"


def test_same_idempotency_key_resumes_a_stale_run_id(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import TrialBudget, TrialCollection, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
        session.add(
            SourceTrialRun(
                run_id="resume-run",
                source_id=source_id,
                adapter_version="v1",
                rule_version="v1",
                started_at=datetime(2026, 9, 11, 8, 0),
                state="running",
                idempotency_key="resume-task",
            )
        )
        session.commit()
    calls = []
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: calls.append(True) or TrialCollection(0, 0, 0, (), ()),
    )

    run_id = run_source_trial(
        session_factory,
        object(),
        source_id,
        "tester",
        TrialBudget(),
        "resume-task",
        now=datetime(2026, 9, 11, 9, 0),
    )

    with session_factory() as session:
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
    assert run_id == "resume-run"
    assert run.state == "empty"
    assert calls == [True]


def test_terminal_error_persists_successful_partial_samples(monkeypatch):
    from app.services import source_trials
    from app.services.source_trials import (
        PartialTrialError,
        TrialBudget,
        TrialCollection,
        run_source_trial,
    )

    class RateLimited(Exception):
        response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "60"}})()

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    partial = TrialCollection(3, 3, 2, (_qualified_candidate(),), ())
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: (_ for _ in ()).throw(PartialTrialError(partial, RateLimited("limited"))),
    )

    run_id = run_source_trial(
        session_factory,
        object(),
        source_id,
        "tester",
        TrialBudget(),
        "partial-limit",
        now=datetime(2026, 9, 11, 9, 0),
    )

    with session_factory() as session:
        run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.run_id == run_id))
        samples = session.scalars(select(SourceTrialSample).where(SourceTrialSample.run_id == run_id)).all()
        source = session.get(Source, source_id)
    assert run.state == "failed"
    assert run.error_code == "RATE_LIMITED"
    assert len(samples) == 1
    assert samples[0].decision == "qualified"
    assert source.next_probe_at == datetime(2026, 9, 11, 9, 1)


def test_real_httpx_timeout_is_persisted_as_timeout(monkeypatch):
    import httpx

    from app.services import source_trials
    from app.services.source_trials import TrialBudget, run_source_trial

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source_id = _source(session).id
    timeout = httpx.ReadTimeout(
        "socket stalled", request=httpx.Request("GET", "https://example.test")
    )
    monkeypatch.setitem(
        source_trials.TRIAL_ADAPTERS,
        "test_adapter",
        lambda *_: (_ for _ in ()).throw(timeout),
    )

    with pytest.raises(httpx.ReadTimeout):
        run_source_trial(
            session_factory, object(), source_id, "tester", TrialBudget(), "httpx-timeout"
        )

    with session_factory() as session:
        run = session.query(SourceTrialRun).one()
    assert run.error_code == "TIMEOUT"
