import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models import AuditEvent, Job, ReviewLog, Source, User
from app.services.intake_promotion import promote_review_only_job
from app.services.real_collection import _save_detail
from app.services.review_intake import REVIEW_NOTE


NOW = datetime(2026, 10, 5, 10, 0)
EVIDENCE_URL = "https://employer.example.com/jobs/123"
EVIDENCE_EXCERPT = "接受 2027 届本科毕业生申请"
NOTE = "已核对企业官方岗位页，确认外校应届生可申请"
BODY = "职位描述：负责市场资料整理与项目分析。官方投递邮箱：jobs@example.com。截止日期：2026-12-31"


def _user(session, *, role="member", can_review=True, is_active=True):
    user = User(
        username=f"actor-{role}-{can_review}-{is_active}",
        display_name="审核同学",
        password_hash="not-used-in-service-test",
        role=role,
        can_review=can_review,
        is_active=is_active,
    )
    session.add(user)
    session.commit()
    return user


def _job(session, **changes):
    values = dict(
        fingerprint="上海商学院就业网|review-only-123",
        employer_name="上海示例服务有限公司",
        announcement_title="策划专员招聘",
        job_title="策划专员",
        job_family="工商运营",
        recruitment_type="社会招聘",
        location_category="明确上海",
        location_detail="上海市",
        target_audience="待人工判断",
        direction_tags="市场运营",
        deadline="2026-12-31",
        official_url=EVIDENCE_URL,
        source_url="https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=123",
        evidence_text=BODY,
        quality_score=82,
        ai_suggested_score=79,
        ai_score_status="已建议",
        ai_score_reason="旧原文建议",
        ai_score_breakdown='{"source": 20}',
        ai_score_confidence="中",
        ai_scored_at=NOW,
        risk_flags="旧人工核验",
        is_demo=False,
        content_fingerprint="original-fingerprint",
        last_verified_at=NOW,
        status="待核验",
        notice_type="新招聘",
        verification_checks='{"source_checked": true}',
        verification_version=3,
        student_fit_level="核心适配",
        distribution_recommendation="进入学生分发审核",
        ai_rationale="旧 AI 判断",
        ai_confidence="高",
        intake_grade="C",
        intake_route="人工复核",
        intake_reason=REVIEW_NOTE,
        version=3,
        row_version=4,
    )
    values.update(changes)
    job = Job(**values)
    session.add(job)
    session.commit()
    return job


def _promote(session, job, actor_id, **changes):
    values = dict(
        actor_id=actor_id,
        expected_version=job.version,
        expected_fingerprint=job.content_fingerprint,
        evidence_url=EVIDENCE_URL,
        evidence_excerpt=EVIDENCE_EXCERPT,
        note=NOTE,
        request_id="req-promotion-1",
        now=NOW,
    )
    values.update(changes)
    return promote_review_only_job(session, job.id, **values)


def test_review_only_promotion_stays_pending_and_records_both_audits(session):
    reviewer = _user(session)
    job = _job(session)

    result = _promote(session, job, reviewer.id)

    assert (result.intake_grade, result.intake_route, result.status) == ("B", "普通待核验", "待核验")
    assert (result.version, result.row_version) == (4, 5)
    assert result.quality_score == 0
    assert (result.verification_checks, result.verification_version, result.last_verified_at) == ("{}", 0, None)
    assert (result.ai_suggested_score, result.ai_score_status, result.ai_score_reason, result.ai_scored_at) == (0, "待建议", "", None)
    assert (result.student_fit_level, result.distribution_recommendation) == ("待人工判断", "仅保留资料库")
    assert "尚未人工核验" in result.risk_flags
    review = session.query(ReviewLog).filter_by(job_id=job.id).one()
    event = session.query(AuditEvent).filter_by(entity_type="job", entity_id=str(job.id)).one()
    for payload in (json.loads(review.note), json.loads(event.changes)):
        assert payload["old_grade"] == "C"
        assert payload["new_grade"] == "B"
        assert payload["evidence_url"] == EVIDENCE_URL
        assert payload["evidence_excerpt"] == EVIDENCE_EXCERPT
        assert payload["note"] == NOTE
        assert payload["content_fingerprint"] == "original-fingerprint"
    assert (review.actor_user_id, event.actor_user_id, event.request_id) == (reviewer.id, reviewer.id, "req-promotion-1")


def test_pending_b_audience_confirmation_stays_b_and_requires_later_review(session):
    reviewer = _user(session)
    job = _job(session, intake_grade="B", intake_route="普通待核验")

    result = _promote(session, job, reviewer.id)

    assert (result.intake_grade, result.status) == ("B", "待核验")
    assert "TARGET_AUDIENCE_UNCLEAR" not in result.intake_reason
    assert result.verification_checks == "{}"
    assert result.quality_score == 0
    review = session.query(ReviewLog).filter_by(job_id=job.id).one()
    event = session.query(AuditEvent).filter_by(entity_id=str(job.id)).one()
    assert review.action == "受众确认：B"
    assert json.loads(event.changes)["old_grade"] == "B"
    assert json.loads(event.changes)["new_grade"] == "B"


@pytest.mark.parametrize("original_grade", ["B", "C"])
def test_confirmed_audience_removes_stale_marker_but_preserves_other_evidence_notes(session, original_grade):
    reviewer = _user(session)
    job = _job(
        session,
        intake_grade=original_grade,
        evidence_note=f"发现 1 个公开附件入口，需单独核验。 {REVIEW_NOTE} 来源页已归档。",
    )

    result = _promote(session, job, reviewer.id)

    assert result.evidence_note == "发现 1 个公开附件入口，需单独核验。 来源页已归档。"
    assert REVIEW_NOTE not in result.evidence_note


@pytest.mark.parametrize("changes", [
    {"intake_grade": "D"},
    {"is_demo": True},
    {"status": "待审核"},
    {"notice_type": "待判断"},
    {"intake_reason": "正文或投递证据不足"},
    {"deadline": "2026-10-04"},
    {"lifecycle_status": "已截止"},
])
def test_only_current_real_new_review_note_c_jobs_are_eligible(session, changes):
    reviewer = _user(session)
    job = _job(session, **changes)

    with pytest.raises(ValueError):
        _promote(session, job, reviewer.id)

    assert session.query(ReviewLog).count() == 0
    assert session.query(AuditEvent).count() == 0
    assert session.get(Job, job.id).intake_grade == changes.get("intake_grade", "C")


@pytest.mark.parametrize("evidence", [
    {"evidence_url": ""},
    {"evidence_url": "file:///jobs/123"},
    {"evidence_url": "http://"},
    {"evidence_excerpt": "  "},
    {"note": "  "},
])
def test_promotion_requires_official_http_evidence_excerpt_and_explanation(session, evidence):
    reviewer = _user(session)
    job = _job(session)

    with pytest.raises(ValueError):
        _promote(session, job, reviewer.id, **evidence)

    assert session.get(Job, job.id).intake_grade == "C"
    assert session.query(ReviewLog).count() == session.query(AuditEvent).count() == 0


def test_expired_original_text_blocks_promotion_even_if_job_deadline_is_later(session):
    reviewer = _user(session)
    job = _job(
        session,
        deadline="2026-12-31",
        evidence_text="职位描述：负责市场分析。报名截止：2026-10-04。官方投递邮箱：jobs@example.com",
    )

    with pytest.raises(ValueError, match="截止"):
        _promote(session, job, reviewer.id)

    assert job.intake_grade == "C"
    assert session.query(ReviewLog).count() == 0


@pytest.mark.parametrize("actor_kwargs", [
    {"can_review": False},
    {"is_active": False},
])
def test_member_without_review_rights_or_disabled_actor_cannot_promote(session, actor_kwargs):
    actor = _user(session, **actor_kwargs)
    job = _job(session)

    with pytest.raises(ValueError, match="审核|权限"):
        _promote(session, job, actor.id)

    assert session.get(Job, job.id).intake_grade == "C"


def test_admin_and_local_admin_can_promote(session):
    admin = _user(session, role="admin", can_review=False)
    first = _job(session)
    second = _job(session, fingerprint="上海商学院就业网|review-only-456")

    assert _promote(session, first, admin.id).intake_grade == "B"
    assert _promote(session, second, None).intake_grade == "B"
    local_event = session.query(AuditEvent).filter_by(entity_id=str(second.id)).one()
    assert (local_event.actor_user_id, local_event.actor_label) == (None, "本地管理员")


def test_stale_version_or_fingerprint_and_duplicate_submission_are_rejected(session):
    reviewer = _user(session)
    job = _job(session)
    original_version, original_fingerprint = job.version, job.content_fingerprint

    with pytest.raises(ValueError, match="版本"):
        _promote(session, job, reviewer.id, expected_version=original_version - 1)
    with pytest.raises(ValueError, match="指纹"):
        _promote(session, job, reviewer.id, expected_fingerprint="stale-fingerprint")
    assert session.query(ReviewLog).count() == 0

    _promote(session, job, reviewer.id)
    with pytest.raises(ValueError):
        _promote(session, job, reviewer.id, expected_version=original_version, expected_fingerprint=original_fingerprint)
    assert session.query(ReviewLog).count() == session.query(AuditEvent).count() == 1


def test_audit_failure_rolls_back_promotion_and_review_log(session, monkeypatch):
    reviewer = _user(session)
    job = _job(session)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr("app.services.intake_promotion.record_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        _promote(session, job, reviewer.id)

    session.refresh(job)
    assert (job.intake_grade, job.version, job.row_version) == ("C", 3, 4)
    assert session.query(ReviewLog).count() == session.query(AuditEvent).count() == 0


def test_changed_original_text_returns_promoted_job_to_unconfirmed_b(session):
    reviewer = _user(session)
    source = Source(name="上海商学院就业网", url="https://jiuye.sbs.edu.cn", level="一级", source_type="高校")
    session.add(source)
    session.commit()
    job = _job(session, fingerprint=f"{source.name}|review-only-123")
    _promote(session, job, reviewer.id)
    detail = SimpleNamespace(
        identity_key="review-only-123",
        detail_url=job.source_url,
        title=job.job_title,
        employer_name=job.employer_name,
        evidence_text=BODY + "\n补充公告：申请入口已更新。",
        official_url=job.official_url,
        location_category=job.location_category,
        location_detail=job.location_detail,
        deadline=job.deadline,
    )

    outcome = _save_detail(session, source, detail, datetime(2026, 10, 6), review_note=REVIEW_NOTE)
    session.commit()

    assert outcome == "updated"
    assert (job.intake_grade, job.intake_reason, job.status) == ("B", REVIEW_NOTE, "待核验")
    assert (job.verification_checks, job.verification_version) == ("{}", 0)
