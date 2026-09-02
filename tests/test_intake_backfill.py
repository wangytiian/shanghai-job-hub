from app.models import Job
from app.services.intake_backfill import backfill_unscreened_intake_jobs


def test_backfill_regrades_legacy_real_jobs_without_screening_evidence(session):
    job = Job(
        fingerprint="旧线索补分级|实习|2026-09-02|上海|公告",
        employer_name="上海测试单位",
        job_title="2027届财务实习生招聘",
        job_family="待分类",
        recruitment_type="待核验",
        location_category="明确上海",
        location_detail="上海",
        target_audience="待人工判断",
        direction_tags="待人工分类",
        deadline="公告未明确统一截止时间",
        official_url="https://example.com/apply",
        source_url="https://example.com/source",
        evidence_text="面向2027届应届毕业生招聘财务实习生。",
        quality_score=0,
        risk_flags="待人工核验",
        is_demo=False,
        status="待核验",
        intake_grade="C",
        intake_route="人工复核",
        intake_reason="",
        intake_evidence="",
    )
    session.add(job)
    session.commit()

    changed = backfill_unscreened_intake_jobs(session)

    assert changed == 1
    assert job.intake_grade == "A"
    assert job.intake_route == "优先待核验"
    assert job.intake_evidence == "实习"
