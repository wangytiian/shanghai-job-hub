from app.database import create_database
from app.models import Job
from app.services.notice_classification import (
    confirm_suggested_new_recruitments,
    suggest_notice_type,
)


def test_physical_exam_notice_is_suggested_as_progress_notice():
    assert suggest_notice_type("某单位公开招聘体检通知", "请参加体检") == "招聘进度通知"


def test_campus_recruitment_is_suggested_as_new_recruitment():
    assert suggest_notice_type("某集团2027届校园招聘公告", "现公开招聘实习生") == "新招聘"


def _pending_job(fingerprint: str, title: str, *, is_demo: bool = False) -> Job:
    return Job(
        fingerprint=fingerprint,
        employer_name="待人工核验（测试单位）",
        job_title=title,
        job_family="待分类",
        recruitment_type="待核验",
        location_category="地区待定",
        location_detail="以公告原文为准",
        target_audience="待人工判断",
        direction_tags="待人工分类",
        deadline="原文待人工确认",
        official_url="",
        source_url="https://example.com/source",
        evidence_text=title,
        quality_score=0,
        risk_flags="真实线索：尚未人工核验，不得对外发布",
        is_demo=is_demo,
        status="待核验",
        notice_type="待判断",
    )


def test_batch_confirmation_only_classifies_pending_real_suggested_recruitment():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        eligible = _pending_job("eligible", "某集团2027届校园招聘公告")
        progress = _pending_job("progress", "某单位公开招聘体检通知")
        demo = _pending_job("demo", "某企业实习生招聘", is_demo=True)
        session.add_all([eligible, progress, demo])
        session.commit()

        assert confirm_suggested_new_recruitments(session, "测试管理员") == 1
        assert session.get(Job, eligible.id).notice_type == "新招聘"
        assert session.get(Job, progress.id).notice_type == "待判断"
        assert session.get(Job, demo.id).notice_type == "待判断"
