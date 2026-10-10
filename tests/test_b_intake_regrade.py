"""Historical C-to-B regrade must be narrow, reviewable and reversible."""

import json
from datetime import datetime
import pytest

from app.models import AuditEvent, Job, ReviewLog, Source
from app.services.b_intake_regrade import apply_b_regrade, preview_b_regrade
from app.services.real_collection import REAL_RISK_FLAG, _content_fingerprint
from app.services.review_intake import REVIEW_NOTE


NOW = datetime(2026, 10, 10, 10, 0)
BODY = (
    "岗位职责：负责活动策划与市场资料整理。任职要求：本科，相关经验者优先。"
    "工作地点：上海市。官方投递邮箱：jobs@example.com。报名截止：2026-12-31。"
)


def _source(session, *, key="sbs-jobs", name="上海商学院就业网"):
    source = Source(
        name=name,
        source_key=key,
        url="https://jiuye.sbs.edu.cn/PositionList.aspx/",
        level="一级",
        source_type="高校",
        library_tier="A",
        is_enabled=True,
    )
    session.add(source)
    session.commit()
    return source


def _job(session, source, **changes):
    identity = f"position-{session.query(Job).count() + 101}"
    detail_url = f"https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid={identity}"
    values = dict(
        fingerprint=f"{source.name}|{identity}",
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
        official_url=detail_url,
        source_url=detail_url,
        evidence_text=BODY,
        attachment_links="[]",
        quality_score=0,
        ai_suggested_score=79,
        ai_score_status="已建议",
        ai_score_reason="旧原文建议",
        ai_score_breakdown='{"source": 20}',
        ai_score_confidence="中",
        ai_scored_at=NOW,
        risk_flags=REAL_RISK_FLAG,
        is_demo=False,
        collected_at=NOW,
        published_at=NOW,
        last_verified_at=NOW,
        lifecycle_status="正常",
        status="待核验",
        notice_type="新招聘",
        verification_checks="{}",
        verification_version=0,
        student_fit_level="待人工判断",
        distribution_recommendation="仅保留资料库",
        intake_grade="C",
        intake_route="人工复核",
        intake_reason=REVIEW_NOTE,
        version=3,
        row_version=4,
    )
    values.update(changes)
    values.setdefault(
        "content_fingerprint",
        _content_fingerprint(f"{values['evidence_text']}\n{values['attachment_links']}"),
    )
    job = Job(**values)
    session.add(job)
    session.commit()
    return job


def _decisions(session):
    return {item["id"]: item for item in preview_b_regrade(session, NOW)}


def test_preview_only_marks_current_complete_sbs_lead_eligible(session):
    source = _source(session)
    good = _job(session, source)
    expired = _job(
        session, source,
        deadline="2026-10-09",
        evidence_text=BODY.replace("2026-12-31", "2026-10-09"),
    )
    uncertain_notice = _job(session, source, notice_type="待判断")
    no_application = _job(
        session, source,
        evidence_text=BODY.replace("官方投递邮箱：jobs@example.com。", "请查看原公告。"),
    )
    other_reason = _job(session, source, intake_reason="岗位正文缺失")
    stale_fingerprint = _job(session, source, content_fingerprint="old-fingerprint")
    other_source = _source(session, key="sspu-news", name="上海第二工业大学就业网")
    from_other_source = _job(session, other_source)

    decisions = _decisions(session)

    assert decisions[good.id]["decision"] == "eligible"
    assert {
        job_id for job_id, item in decisions.items() if item["decision"] == "eligible"
    } == {good.id}
    assert other_reason.id not in decisions
    for job in (expired, uncertain_notice, no_application, stale_fingerprint, from_other_source):
        if job.id in decisions:
            assert decisions[job.id]["decision"] != "eligible"
            assert decisions[job.id]["reason"]


def test_apply_only_explicit_eligible_id_and_preserves_pending_marker(session):
    source = _source(session)
    selected = _job(session, source)
    not_selected = _job(session, source)

    applied = apply_b_regrade(session, NOW, [selected.id])

    session.refresh(selected)
    session.refresh(not_selected)
    assert applied == 1
    assert (selected.intake_grade, selected.intake_route, selected.status) == (
        "B", "普通待核验", "待核验",
    )
    assert selected.intake_reason == REVIEW_NOTE
    assert (selected.quality_score, selected.ai_suggested_score, selected.ai_score_status) == (0, 0, "待建议")
    assert (selected.verification_checks, selected.verification_version, selected.last_verified_at) == (
        "{}", 0, NOW,
    )
    assert (selected.version, selected.row_version) == (4, 5)
    assert not_selected.intake_grade == "C"


def test_apply_records_one_review_log_and_one_audit_event(session):
    source = _source(session)
    job = _job(session, source)

    assert apply_b_regrade(session, NOW, [job.id]) == 1

    reviews = session.query(ReviewLog).filter_by(job_id=job.id).all()
    events = session.query(AuditEvent).filter_by(entity_type="job", entity_id=str(job.id)).all()
    assert len(reviews) == len(events) == 1
    assert "B" in reviews[0].action
    changes = json.loads(events[0].changes)
    assert (changes["old_grade"], changes["new_grade"]) == ("C", "B")
    assert changes["content_fingerprint"] == job.content_fingerprint


def test_apply_rechecks_eligibility_and_is_idempotent(session):
    source = _source(session)
    eligible = _job(session, source)
    no_application = _job(
        session, source,
        evidence_text=BODY.replace("官方投递邮箱：jobs@example.com。", "请查看原公告。"),
    )

    assert apply_b_regrade(session, NOW, [eligible.id, no_application.id]) == 1
    assert apply_b_regrade(session, NOW, [eligible.id, no_application.id]) == 0

    session.refresh(eligible)
    session.refresh(no_application)
    assert (eligible.intake_grade, no_application.intake_grade) == ("B", "C")
    assert session.query(ReviewLog).count() == session.query(AuditEvent).count() == 1


def test_strict_apply_rejects_mixed_ids_without_partial_migration(session):
    source = _source(session)
    eligible = _job(session, source)
    invalid = _job(session, source, notice_type="待判断")

    with pytest.raises(ValueError, match="不在当前可重判预览"):
        apply_b_regrade(session, NOW, [eligible.id, invalid.id], strict=True)

    session.refresh(eligible)
    assert eligible.intake_grade == "C"
    assert session.query(ReviewLog).count() == 0


@pytest.mark.parametrize(
    "edited_field",
    [
        {"quality_score": 82},
        {"verification_checks": '{"source_checked": true}'},
        {"verification_version": 3},
        {"student_fit_level": "核心适配"},
        {"distribution_recommendation": "进入学生分发审核"},
        {"risk_flags": "待最终人工审核：结构化字段已由运营人员补齐"},
    ],
)
def test_preview_skips_prior_human_work_even_when_status_returned_to_pending(session, edited_field):
    source = _source(session)
    job = _job(session, source, **edited_field)

    decision = _decisions(session)[job.id]

    assert decision["decision"] == "skip"
    assert "人工" in decision["reason"]
    assert apply_b_regrade(session, NOW, [job.id]) == 0
    session.refresh(job)
    assert job.intake_grade == "C"
    for key, value in edited_field.items():
        assert getattr(job, key) == value


def test_preview_allows_notice_classification_but_skips_substantive_review_log(session):
    source = _source(session)
    classified = _job(session, source)
    reviewed = _job(session, source)
    session.add_all([
        ReviewLog(job_id=classified.id, action="分类：新招聘", operator_name="运营"),
        ReviewLog(job_id=reviewed.id, action="退回", operator_name="审核员"),
    ])
    session.commit()

    decisions = _decisions(session)

    assert decisions[classified.id]["decision"] == "eligible"
    assert decisions[reviewed.id]["decision"] == "skip"
    assert "人工" in decisions[reviewed.id]["reason"]
