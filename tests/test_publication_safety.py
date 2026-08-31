from app.models import Job
from app.services.publication_safety import return_unsafe_publishable_jobs


def test_return_unsafe_publishable_jobs_moves_legacy_record_back_to_verification(session):
    job = Job(
        fingerprint="旧数据保护|测试", employer_name="测试单位", job_title="招聘公告", job_family="待分类",
        recruitment_type="待核验", location_category="地区待定", location_detail="以公告原文为准",
        target_audience="待人工判断", direction_tags="待人工分类", deadline="原文待人工确认",
        official_url="https://example.com", source_url="https://example.com/source", evidence_text="原文",
        quality_score=0, risk_flags="尚未人工核验，不得对外发布", is_demo=False, status="可发布",
    )
    session.add(job)
    session.commit()

    changed = return_unsafe_publishable_jobs(session)

    assert changed == 1
    assert job.status == "待核验"
    assert "发布保护" in job.last_change_summary
