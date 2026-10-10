"""The bounded B-source import is explicit, offline, and conservative."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json

import pytest
from sqlalchemy import select

from app.models import AuditEvent, Job, Source, SourceTrialRun, SourceTrialSample, User
from app.services.b_pilot_intake import import_trial_run as _import_trial_run
from app.services.intake_promotion import promote_review_only_job
from app.services.real_collection import REAL_RISK_FLAG
from app.services.source_candidate_policy import (
    RULE_VERSION,
    SourceCandidate,
    evaluate_candidate,
)
from app.services.source_trials import _sample_from_candidate


NOW = datetime(2026, 10, 5, 9, 0)


def import_trial_run(session, source_id, run_id, actor_id, *, sample_ids=None, **kwargs):
    """Tests deliberately select every sample in small fixture rounds."""
    if sample_ids is None:
        sample_ids = tuple(session.scalars(
            select(SourceTrialSample.id).where(SourceTrialSample.run_id == run_id)
        ).all())
    return _import_trial_run(
        session, source_id, run_id, actor_id, sample_ids=sample_ids, **kwargs
    )


def _source(session, *, key="sufe-jobs", status="正常"):
    source = Source(
        name=f"试点来源 {key}",
        source_key=key,
        url="https://career.sufe.edu.cn/career/zpxx/search/zpxx",
        level="一级",
        source_type="高校就业平台",
        adapter_key="sufe_job_json" if key == "sufe-jobs" else "sjtu_internship_json",
        library_tier="B",
        is_enabled=False,
        status=status,
        adapter_version="campus-v1",
    )
    session.add(source)
    session.flush()
    return source


def _candidate(index=1, **changes):
    candidate = SourceCandidate(
        identity_key=f"notice-{index}:role-{index}",
        announcement_id=f"notice-{index}",
        job_id=f"role-{index}",
        employer_name="上海示例科技有限公司",
        title="数据分析岗位",
        location_category="明确上海",
        location_detail="上海市杨浦区",
        published_at="2026-10-01",
        deadline="2026-10-20",
        evidence_text="岗位职责：整理数据。任职要求：接受2027届毕业生，工作地点：上海。官方投递邮箱：apply@example.com",
        source_url=f"https://career.sufe.edu.cn/career/zpxx/detail/{index}",
        application_kind="email",
        application_value="apply@example.com",
        application_evidence="官方投递邮箱：apply@example.com",
    )
    return replace(candidate, **changes)


def _run(session, source, candidates=(), *, run_id="run-1", state="completed"):
    run = SourceTrialRun(
        run_id=run_id,
        source_id=source.id,
        adapter_version=source.adapter_version,
        rule_version=RULE_VERSION,
        state=state,
        started_at=datetime(2026, 10, 2),
        finished_at=datetime(2026, 10, 2, 1) if state != "running" else None,
        idempotency_key=run_id,
    )
    session.add(run)
    session.flush()
    for candidate in candidates:
        decision = evaluate_candidate(candidate, now=datetime(2026, 10, 2))
        session.add(_sample_from_candidate(run_id, candidate, decision))
    session.commit()
    return run


def _rehash(sample):
    sample.content_hash = sha256(" ".join(sample.evidence_text.split()).encode("utf-8")).hexdigest()


def test_explicit_import_creates_pending_real_job_with_origin_and_keeps_b_disabled(session):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    assert session.query(Job).count() == 0

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW, request_id="request-1")

    assert (result.created, result.updated, result.unchanged, result.skipped) == (1, 0, 0, 0)
    job = session.query(Job).one()
    sample = session.query(SourceTrialSample).one()
    assert job.fingerprint == f"{source.name}|notice-1:role-1"
    assert (job.is_demo, job.status, job.risk_flags) == (False, "待核验", REAL_RISK_FLAG)
    assert job.published_at == datetime(2026, 10, 1)
    assert job.collected_at == run.finished_at
    assert job.last_verified_at == run.finished_at
    assert job.quality_score == 0
    assert (job.application_method, job.application_contact) == ("email", "apply@example.com")
    assert (source.library_tier, source.is_enabled) == ("B", False)
    assert source.source_key in job.evidence_note
    assert run.run_id in job.evidence_note
    assert str(sample.id) in job.evidence_note
    audit = session.query(AuditEvent).one()
    changes = json.loads(audit.changes)
    assert (audit.action, audit.request_id) == ("source_trial.import", "request-1")
    assert (changes["source_key"], changes["run_id"], changes["sample_id"]) == (
        source.source_key, run.run_id, sample.id
    )


def test_same_run_second_import_does_not_create_duplicate(session):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    import_trial_run(session, source.id, run.run_id, None, now=NOW)

    second = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (second.created, second.updated, second.unchanged) == (0, 0, 1)
    assert session.query(Job).count() == 1


def test_reimport_of_unchanged_content_preserves_human_review_state(session):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    import_trial_run(session, source.id, run.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.status = "已审核"
    job.risk_flags = "人工已核验"
    note = job.evidence_note
    version = job.version
    session.commit()

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert result.unchanged == 1
    assert (job.status, job.risk_flags, job.evidence_note, job.version) == (
        "已审核", "人工已核验", note, version
    )


def test_older_trial_cannot_replace_newer_imported_content(session):
    source = _source(session)
    old = _candidate()
    older = _run(session, source, [old], run_id="run-older")
    import_trial_run(session, source.id, older.run_id, None, now=NOW)
    new = replace(old, evidence_text=old.evidence_text + " 新增说明。")
    newer = _run(session, source, [new], run_id="run-newer")
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()
    import_trial_run(session, source.id, newer.run_id, None, now=NOW)

    result = import_trial_run(session, source.id, older.run_id, None, now=NOW)

    job = session.query(Job).one()
    assert (result.created, result.updated, result.skipped) == (0, 0, 1)
    assert result.skip_reasons["STALE_TRIAL_RUN"] == 1
    assert job.evidence_text == new.evidence_text
    assert job.last_verified_at == newer.finished_at


def test_expired_date_in_body_revokes_an_existing_job(session):
    source = _source(session)
    original = _candidate()
    older = _run(session, source, [original], run_id="run-valid")
    import_trial_run(session, source.id, older.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.status = "已审核"
    job.verification_checks = '{"manual": true}'
    session.commit()
    expired = replace(
        original,
        evidence_text=original.evidence_text + " 报名截止：2026-10-04。",
        deadline="2026-10-20",
    )
    newer = _run(session, source, [expired], run_id="run-expired")
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()

    result = import_trial_run(session, source.id, newer.run_id, None, now=NOW)

    assert (result.updated, result.skipped) == (1, 0)
    assert result.invalidated == 1
    assert (job.status, job.deadline, job.lifecycle_status) == (
        "已截止", "2026-10-04", "已截止"
    )
    assert (job.verification_checks, job.verification_version) == ("{}", 0)
    assert job.evidence_text == expired.evidence_text
    assert session.query(AuditEvent).filter_by(action="source_trial.invalidate").count() == 1


def test_demo_job_with_same_fingerprint_is_never_used_as_real_import_target(session):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    from app.seed import seed_demo_data

    seed_demo_data(session)
    demo = session.query(Job).filter_by(is_demo=True).first()
    demo.fingerprint = f"{source.name}|notice-1:role-1"
    session.commit()
    original_title = demo.job_title

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.updated, result.skipped) == (0, 0, 1)
    assert result.skip_reasons["EXISTING_DEMO_JOB"] == 1
    assert demo.is_demo is True
    assert demo.job_title == original_title


@pytest.mark.parametrize("key", ["ncss-shanghai", "sbs-jobs", "unlisted"])
def test_unregistered_or_mismatched_source_adapter_cannot_import(session, key):
    source = _source(session, key=key)
    run = _run(session, source, [_candidate()])
    with pytest.raises(ValueError, match="试点|白名单|允许"):
        import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert session.query(Job).count() == 0


@pytest.mark.parametrize("change", [
    {"status": "暂停"},
    {"library_tier": "A"},
    {"is_enabled": True},
    {"adapter_key": "sbs_jobs"},
])
def test_changed_or_suspended_source_cannot_import(session, change):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    for key, value in change.items():
        setattr(source, key, value)
    session.commit()
    with pytest.raises(ValueError):
        import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert session.query(Job).count() == 0


@pytest.mark.parametrize("state", ["running", "failed", "empty", "interrupted"])
def test_only_finished_completed_or_partial_runs_can_import(session, state):
    source = _source(session)
    run = _run(session, source, [_candidate()], state=state)
    with pytest.raises(ValueError, match="轮次|状态|结束"):
        import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert session.query(Job).count() == 0


def test_partial_run_can_import_and_run_must_belong_to_source(session):
    source = _source(session)
    other = _source(session, key="sjtu-internships")
    run = _run(session, source, [_candidate()], state="partial")
    with pytest.raises(ValueError, match="轮次|归属"):
        import_trial_run(session, other.id, run.run_id, None, now=NOW)
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert result.created == 1


@pytest.mark.parametrize("field,value", [
    ("adapter_version", "campus-v2"),
    ("rule_version", "source-candidate-v1"),
])
def test_run_versions_must_match_current_rules(session, field, value):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    setattr(run, field, value)
    session.commit()
    with pytest.raises(ValueError, match="版本"):
        import_trial_run(session, source.id, run.run_id, None, now=NOW)


@pytest.mark.parametrize("role,active", [("member", True), ("reviewer", True), ("admin", False)])
def test_only_active_admin_or_local_admin_can_import(session, role, active):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    actor = User(
        username=f"{role}-{active}", display_name="测试操作员", password_hash="test",
        role=role, is_active=active,
    )
    session.add(actor)
    session.commit()
    with pytest.raises(ValueError, match="管理员"):
        import_trial_run(session, source.id, run.run_id, actor.id, now=NOW)
    assert session.query(Job).count() == 0


@pytest.mark.parametrize("change,reason", [
    ({"deadline": "2026-10-04"}, "APPLICATION_EXPIRED"),
    ({"location_category": "其他地区", "location_detail": "浙江省"}, "LOCATION_NOT_SHANGHAI"),
    ({"evidence_text": "", "application_evidence": ""}, "BODY_MISSING"),
    ({"application_evidence": ""}, "APPLICATION_MISSING"),
    ({"title": "招聘活动"}, "NOT_OPEN_RECRUITMENT"),
])
def test_sample_is_rechecked_at_import_time(session, change, reason):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    sample = session.query(SourceTrialSample).one()
    for key, value in change.items():
        setattr(sample, key, value)
    _rehash(sample)
    session.commit()
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons[reason] == 1
    assert session.query(Job).count() == 0


def test_tampered_body_hash_or_sample_identity_is_skipped(session):
    source = _source(session)
    run = _run(session, source, [_candidate(1), _candidate(2)])
    samples = session.query(SourceTrialSample).order_by(SourceTrialSample.id).all()
    samples[0].evidence_text += " 改写"
    samples[1].identity_key = "another:role"
    session.commit()
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert (result.created, result.skipped) == (0, 2)
    assert result.skip_reasons["CONTENT_HASH_MISMATCH"] == 1
    assert result.skip_reasons["IDENTITY_MISMATCH"] == 1


def test_only_audience_unclear_review_only_lead_enters_as_pending_b(session):
    source = _source(session)
    candidate = _candidate(
        title="数据分析岗位",
        evidence_text="岗位职责：整理数据。任职要求：熟悉 Excel，工作地点：上海。官方投递邮箱：apply@example.com",
    )
    run = _run(session, source, [candidate])
    assert session.query(SourceTrialSample).one().decision == "needs_evidence"
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert result.created == 1
    job = session.query(Job).one()
    assert (job.intake_grade, job.intake_route, job.status) == ("B", "普通待核验", "待核验")
    assert "TARGET_AUDIENCE_UNCLEAR" in job.intake_reason


def test_needs_evidence_sample_cannot_become_qualified_by_changing_unhashed_title(session):
    source = _source(session)
    unclear = _candidate(
        evidence_text="岗位职责：整理数据。任职要求：熟悉 Excel，工作地点：上海。官方投递邮箱：apply@example.com",
    )
    run = _run(session, source, [unclear])
    sample = session.query(SourceTrialSample).one()
    assert sample.decision == "needs_evidence"
    sample.title = "2027届数据分析岗位"
    session.commit()

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons["TRIAL_DECISION_CHANGED"] == 1


@pytest.mark.parametrize("body,reason", [
    ("岗位职责：整理数据，仅限本校学生，工作地点：上海。官方投递邮箱：apply@example.com", "SCHOOL_EXCLUSIVE"),
    ("岗位职责：整理数据，要求2年以上工作经验，工作地点：上海。官方投递邮箱：apply@example.com", "UNSUITABLE_FOR_STUDENT_REVIEW"),
    ("岗位职责：整理数据，工作地点：上海。", "APPLICATION_MISSING"),
])
def test_other_needs_evidence_is_not_imported(session, body, reason):
    source = _source(session)
    run = _run(session, source, [_candidate(evidence_text=body)])
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons[reason] == 1


def test_qualified_student_evidence_screened_c_is_imported_as_b(session):
    source = _source(session)
    candidate = _candidate(
        evidence_text="岗位职责：整理数据。任职要求：接受2027届毕业生，工作地点：上海。官方投递邮箱：apply@example.com",
    )
    run = _run(session, source, [candidate])
    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)
    assert result.created == 1
    assert session.query(Job).one().intake_grade == "B"


def test_selected_sample_import_does_not_touch_unselected_samples(session):
    source = _source(session)
    run = _run(session, source, [_candidate(1), _candidate(2)])
    first, second = session.query(SourceTrialSample).order_by(SourceTrialSample.id).all()

    result = import_trial_run(
        session, source.id, run.run_id, None, now=NOW, sample_ids=(first.id,)
    )

    assert (result.created, result.skipped) == (1, 0)
    assert session.query(Job).one().fingerprint.endswith(first.identity_key)
    assert not session.query(Job).filter(Job.fingerprint.like(f"%{second.identity_key}")).count()


def test_selected_sample_must_belong_to_the_requested_run(session):
    source = _source(session)
    run = _run(session, source, [_candidate(1)])
    other = _run(session, source, [_candidate(2)], run_id="other-run")
    foreign_id = session.query(SourceTrialSample).filter_by(run_id=other.run_id).one().id

    with pytest.raises(ValueError, match="样本.*不属于"):
        import_trial_run(session, source.id, run.run_id, None, now=NOW, sample_ids=(foreign_id,))
    assert session.query(Job).count() == 0


def test_explicit_top_school_requirement_is_not_imported(session):
    source = _source(session)
    body = (
        "岗位职责：协助投资研究。任职要求：国内外Top院校本科及以上在读学生，"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert result.created == 0
    assert result.skip_reasons["SCHOOL_EXCLUSIVE"] == 1


@pytest.mark.parametrize("restriction", [
    "只招本校在校生",
    "仅接收本校学生",
    "不对外校开放",
    "仅接收上海财经大学学生",
    "只招上财在校生",
])
def test_b_pilot_rejects_explicit_school_only_restrictions(session, restriction):
    source = _source(session)
    body = (
        f"岗位职责：整理数据。任职要求：接受2027届毕业生；{restriction}。"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons["SCHOOL_EXCLUSIVE"] == 1


@pytest.mark.parametrize("requirement", [
    "要求2年工作经验",
    "须具备两年相关工作经验",
    "至少2年相关经验",
    "工作经验2年以上",
])
def test_b_pilot_rejects_required_two_year_experience(session, requirement):
    source = _source(session)
    body = (
        f"岗位职责：整理数据。任职要求：接受2027届毕业生；{requirement}。"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons["UNSUITABLE_FOR_STUDENT_REVIEW"] == 1


@pytest.mark.parametrize("school_tier", ["Top院校", "985高校", "211大学"])
def test_b_pilot_keeps_hard_elite_school_gate(session, school_tier):
    source = _source(session)
    body = (
        f"岗位职责：整理数据。任职要求：接受2027届毕业生，{school_tier}本科及以上。"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (0, 1)
    assert result.skip_reasons["SCHOOL_EXCLUSIVE"] == 1


@pytest.mark.parametrize("preference", [
    "有2年工作经验者优先",
    "2年以上相关经验优先",
    "985院校优先",
    "985高校的毕业生优先",
    "Top院校背景者优先",
])
def test_b_pilot_does_not_treat_preferences_as_hard_requirements(session, preference):
    source = _source(session)
    body = (
        f"岗位职责：整理数据。任职要求：接受2027届毕业生，{preference}。"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (1, 0)


@pytest.mark.parametrize("open_statement", [
    "不限本校学生，外校学生均可报名",
    "不对外校设置限制，外校学生可申请",
])
def test_b_pilot_accepts_explicitly_open_school_language(session, open_statement):
    source = _source(session)
    body = (
        f"岗位职责：整理数据。任职要求：接受2027届毕业生；{open_statement}。"
        "工作地点：上海。官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(evidence_text=body)])

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert (result.created, result.skipped) == (1, 0)


def test_each_call_creates_at_most_ten_new_jobs_and_can_continue_later(session):
    source = _source(session)
    run = _run(session, source, [_candidate(index) for index in range(12)])
    ids = tuple(session.scalars(
        select(SourceTrialSample.id).where(SourceTrialSample.run_id == run.run_id)
        .order_by(SourceTrialSample.id)
    ).all())
    first = import_trial_run(
        session, source.id, run.run_id, None, now=NOW, max_jobs=20, sample_ids=ids[:10]
    )
    assert (first.created, first.skipped, session.query(Job).count()) == (10, 0, 10)
    second = import_trial_run(
        session, source.id, run.run_id, None, now=NOW, sample_ids=ids[10:]
    )
    assert (second.created, second.unchanged, session.query(Job).count()) == (2, 0, 12)


def test_service_import_requires_explicit_nonempty_selection(session):
    source = _source(session)
    run = _run(session, source, [_candidate()])
    with pytest.raises(ValueError, match="选择|样本"):
        _import_trial_run(session, source.id, run.run_id, None, now=NOW)
    with pytest.raises(ValueError, match="选择|样本"):
        _import_trial_run(session, source.id, run.run_id, None, now=NOW, sample_ids=())
    assert session.query(Job).count() == 0


def test_new_trial_content_resets_old_verification_and_manual_c_to_b(session):
    source = _source(session)
    old = _candidate(evidence_text="岗位职责：整理数据。任职要求：熟悉 Excel，工作地点：上海。官方投递邮箱：apply@example.com")
    first_run = _run(session, source, [old], run_id="run-first")
    import_trial_run(session, source.id, first_run.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.intake_grade = "B"
    job.status = "已审核"
    job.verification_checks = '{"manual": true}'
    job.verification_version = job.version
    old_version = job.version
    session.commit()
    new = replace(old, evidence_text=old.evidence_text + " 新增岗位说明。")
    second_run = _run(session, source, [new], run_id="run-second")
    result = import_trial_run(session, source.id, second_run.run_id, None, now=NOW)
    assert result.updated == 1
    assert session.query(Job).count() == 1
    assert (job.status, job.intake_grade, job.verification_checks, job.verification_version) == (
        "待核验", "B", "{}", 0
    )
    assert "TARGET_AUDIENCE_UNCLEAR" in job.intake_reason
    assert job.version == old_version + 1


def test_older_trial_cannot_replace_newer_import_after_human_clears_verification_time(session):
    source = _source(session)
    initial = _candidate()
    older = _run(session, source, [initial], run_id="old-run")
    import_trial_run(session, source.id, older.run_id, None, now=NOW)
    revised = replace(initial, evidence_text=initial.evidence_text + " 最新岗位要求。")
    newer = _run(session, source, [revised], run_id="new-run")
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()
    import_trial_run(session, source.id, newer.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.last_verified_at = None  # Human re-review intentionally clears verification status.
    session.commit()

    result = import_trial_run(session, source.id, older.run_id, None, now=NOW)

    assert result.skip_reasons == {"STALE_TRIAL_RUN": 1}
    assert job.evidence_text == revised.evidence_text


def test_newer_out_of_city_trial_invalidates_previously_reviewed_job(session):
    source = _source(session)
    initial = _candidate()
    older = _run(session, source, [initial], run_id="valid-run")
    import_trial_run(session, source.id, older.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.status = "已审核"
    job.verification_checks = '{"manual": true}'
    session.commit()
    elsewhere = replace(initial, location_category="其他地区", location_detail="浙江省杭州市")
    newer = _run(session, source, [elsewhere], run_id="elsewhere-run")
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()

    result = import_trial_run(session, source.id, newer.run_id, None, now=NOW)

    assert (result.updated, result.skipped) == (1, 0)
    assert result.invalidated == 1
    assert (job.status, job.intake_grade, job.verification_checks, job.verification_version) == (
        "待核验", "D", "{}", 0
    )
    assert "LOCATION_NOT_SHANGHAI" in job.intake_reason
    assert session.query(AuditEvent).filter_by(action="source_trial.invalidate").count() == 1
    stale = import_trial_run(session, source.id, older.run_id, None, now=NOW)
    assert stale.skip_reasons == {"STALE_TRIAL_RUN": 1}
    assert job.intake_grade == "D"


def test_real_c_to_b_promotion_cannot_erase_newer_trial_watermark(session):
    source = _source(session)
    initial = _candidate(
        evidence_text="岗位职责：整理数据。任职要求：熟悉 Excel，工作地点：上海。官方投递邮箱：apply@example.com"
    )
    older = _run(session, source, [initial], run_id="promote-old")
    import_trial_run(session, source.id, older.run_id, None, now=NOW)
    revised = replace(initial, evidence_text=initial.evidence_text + " 新增职责说明。")
    newer = _run(session, source, [revised], run_id="promote-new")
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()
    import_trial_run(session, source.id, newer.run_id, None, now=NOW)
    job = session.query(Job).one()
    job.notice_type = "新招聘"
    session.commit()
    promote_review_only_job(
        session, job.id, actor_id=None, expected_version=job.version,
        expected_fingerprint=job.content_fingerprint,
        evidence_url="https://career.sufe.edu.cn/career/zpxx/detail/1",
        evidence_excerpt="接受2027届本科毕业生申请",
        note="人工补证后确认面向学生", now=NOW,
    )

    result = import_trial_run(session, source.id, older.run_id, None, now=NOW)

    assert result.skip_reasons == {"STALE_TRIAL_RUN": 1}
    assert job.evidence_text == revised.evidence_text
    assert job.intake_grade == "B"


def test_old_qualified_trial_cannot_import_after_newer_excluded_sample_exists(session):
    source = _source(session)
    old = _candidate()
    older = _run(session, source, [old], run_id="superseded-old")
    newer = _run(
        session, source,
        [replace(old, location_category="其他地区", location_detail="浙江省杭州市")],
        run_id="superseding-new",
    )
    newer.finished_at = datetime(2026, 10, 4, 1)
    session.commit()

    result = import_trial_run(session, source.id, older.run_id, None, now=NOW)

    assert result.created == 0
    assert result.skip_reasons == {"SUPERSEDED_TRIAL_SAMPLE": 1}
    assert session.query(Job).count() == 0


def test_outdated_trial_is_not_importable_even_if_no_newer_trial_exists(session):
    source = _source(session)
    old = _run(session, source, [_candidate()], run_id="stale-standalone")
    old.finished_at = datetime(2026, 8, 31, 1)
    session.commit()

    result = import_trial_run(session, source.id, old.run_id, None, now=NOW)

    assert result.created == 0
    assert result.skip_reasons == {"TRIAL_TOO_OLD": 1}


def test_old_campus_location_classification_is_rechecked_from_raw_evidence(session):
    source = _source(session)
    body = (
        "招聘公告：数据分析实习生招聘\n"
        "发布时间：2026-10-01\n"
        "正文：岗位职责：整理数据。接受2027届毕业生。工作地点：上海。\n"
        "招聘单位：示例科技有限公司\n"
        "单位地址：北京市朝阳区示例路1号\n"
        "岗位名称：数据分析实习生\n"
        "岗位地点：天心区\n"
        "官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(
        title="数据分析实习生", evidence_text=body,
        location_category="明确上海", location_detail="上海市 · 天心区",
    )], run_id="old-location-parser")

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert result.created == 0
    assert result.skip_reasons == {"LOCATION_EVIDENCE_MISMATCH": 1}


def test_matching_shanghai_unit_address_is_enough_for_cityless_campus_position(session):
    source = _source(session)
    body = (
        "招聘公告：量化实习生招聘\n"
        "发布时间：2026-10-01\n"
        "正文：岗位职责：协助投研。接受2027届毕业生。\n"
        "招聘单位：示例科技有限公司\n"
        "单位地址：中国（上海）自由贸易试验区泳耀路511弄2号\n"
        "岗位名称：量化实习生\n"
        "岗位地点：泳耀路511弄6号楼\n"
        "官方投递邮箱：apply@example.com"
    )
    run = _run(session, source, [_candidate(
        title="量化实习生", evidence_text=body,
        location_category="明确上海", location_detail="上海市 · 泳耀路511弄6号楼",
    )], run_id="confirmed-location")

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert result.created == 1


def test_ambiguous_famous_school_requirement_is_flagged_for_human_review(session):
    source = _source(session)
    candidate = _candidate(evidence_text=(
        "岗位职责：整理数据。任职要求：国内外知名高校本科及以上在读生，"
        "接受2027届毕业生。工作地点：上海。官方投递邮箱：apply@example.com"
    ))
    run = _run(session, source, [candidate], run_id="school-fit-unclear")

    result = import_trial_run(session, source.id, run.run_id, None, now=NOW)

    assert result.created == 1
    assert "院校资格待人工确认" in session.query(Job).one().evidence_note
