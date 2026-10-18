from dataclasses import replace
import json

from app.services.jobs import REQUIRED_VERIFICATION_CHECKS
from app.services.structuring import StructuringInput, structure_job


def complete_input():
    return StructuringInput(
        employer_name="测试单位", job_title="财务实习", job_family="财会审计", recruitment_type="实习",
        location_category="明确上海", location_detail="上海", target_audience="大三实习", direction_tags="财务",
        deadline="2099-12-31", official_url="https://example.com/jobs", quality_score=80,
        student_fit_level="核心适配", distribution_recommendation="进入学生分发审核",
        verification_checks={key: True for key in REQUIRED_VERIFICATION_CHECKS},
    )


def test_structure_can_participate_in_an_outer_transaction(session, demo_job):
    demo_job.status = "待核验"
    demo_job.notice_type = "新招聘"
    session.commit()
    structure_job(session, demo_job.id, complete_input(), "测试", commit=False)
    session.rollback()
    assert session.get(type(demo_job), demo_job.id).status == "待核验"


def test_multi_role_screenshot_does_not_pretend_to_be_an_attachment(session, demo_job):
    demo_job.status = "待核验"
    demo_job.notice_type = "新招聘"
    data = replace(complete_input(), posting_scope="multi_role_announcement", attachment_status="not_required",
                   role_details_status="checked", role_details_evidence=json.dumps([{"quote":"财务实习"}]))
    job = structure_job(session, demo_job.id, data, "测试")
    assert job.status == "待审核"
    assert job.attachment_status == "not_required"
