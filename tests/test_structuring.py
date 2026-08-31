import pytest

from app.models import Job, ReviewLog
from app.services.structuring import StructuringInput, structure_job


def _pending_verification_job(session) -> Job:
    job = Job(
        fingerprint="结构化测试|公告|2026-08-26|上海|公告",
        employer_name="待人工核验（测试公告）",
        job_title="测试招聘公告",
        job_family="待分类",
        recruitment_type="待核验",
        location_category="地区待定",
        location_detail="以公告原文为准",
        target_audience="待人工判断",
        direction_tags="待人工分类",
        deadline="原文待人工确认",
        official_url="",
        source_url="https://example.com/source",
        evidence_text="这是公开招聘公告的原文证据。",
        quality_score=0,
        is_demo=False,
        risk_flags="真实线索：尚未人工核验，不得对外发布",
        status="待核验",
        notice_type="新招聘",
    )
    session.add(job)
    session.commit()
    return job


def _valid_input(**overrides) -> StructuringInput:
    values = {
        "employer_name": "测试单位",
        "job_title": "财务分析实习生",
        "job_family": "财务分析",
        "recruitment_type": "实习",
        "location_category": "明确上海",
        "location_detail": "上海市浦东新区",
        "target_audience": "大三实习",
        "direction_tags": "会计审计、金融银行",
        "deadline": "2026-09-30",
        "official_url": "https://example.com/apply",
        "posting_scope": "single_role",
        "attachment_status": "not_required",
        "application_method": "official_page",
        "application_contact": "",
        "quality_score": 82,
        "note": "已按公开原文核验。",
    }
    values.update(overrides)
    return StructuringInput(**values)


def test_structure_job_requires_official_link_and_audience(session):
    job = _pending_verification_job(session)

    with pytest.raises(ValueError, match="官方报名链接"):
        structure_job(session, job.id, _valid_input(official_url=""), "本地管理员")
    with pytest.raises(ValueError, match="适合人群"):
        structure_job(session, job.id, _valid_input(target_audience=""), "本地管理员")


def test_structure_job_allows_blank_deadline_as_explicitly_unstated(session):
    job = _pending_verification_job(session)

    result = structure_job(session, job.id, _valid_input(deadline=""), "本地管理员")

    assert result.deadline == "公告未明确统一截止时间"


def test_structure_job_saves_fields_marks_pending_review_and_writes_log(session):
    job = _pending_verification_job(session)

    result = structure_job(session, job.id, _valid_input(), "本地管理员")

    assert result.status == "待审核"
    assert result.employer_name == "测试单位"
    assert result.official_url == "https://example.com/apply"
    assert result.quality_score == 82
    assert "待最终人工审核" in result.risk_flags
    assert session.query(ReviewLog).filter_by(job_id=job.id, action="结构化完成").count() == 1


def test_structure_job_only_accepts_pending_verification_jobs(session):
    job = _pending_verification_job(session)
    job.status = "待审核"
    session.commit()

    with pytest.raises(ValueError, match="只有待核验公告"):
        structure_job(session, job.id, _valid_input(), "本地管理员")


def test_structure_job_keeps_attachment_pending_announcement_in_verification(session):
    job = _pending_verification_job(session)

    with pytest.raises(ValueError, match="附件尚未核验"):
        structure_job(
            session,
            job.id,
            _valid_input(posting_scope="attachment_pending", attachment_status="pending"),
            "本地管理员",
        )
