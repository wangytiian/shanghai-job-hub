import json

import pytest

from app.services.intake_screening import screen_intake, screen_intake_with_ai


def test_progress_notice_is_filtered_to_grade_d():
    result = screen_intake("体检通知", "请进入体检环节的考生按时参加体检。")

    assert result.grade == "D"
    assert result.route == "过滤留档"


def test_student_internship_is_grade_a():
    result = screen_intake("财务实习生", "面向2027届本科生招聘财务实习生。")

    assert result.grade == "A"
    assert result.route == "优先待核验"


def test_uncertain_notice_is_grade_c_for_human_review():
    result = screen_intake("招聘公告", "请查看附件了解具体岗位安排。")

    assert result.grade == "C"
    assert result.route == "人工复核"


def test_ai_screening_uses_constrained_grade_when_no_hard_filter_matches():
    result = screen_intake_with_ai(
        "银行管培生招聘",
        "面向2027届应届毕业生招聘管理培训生，工作地点上海。",
        lambda prompt: '{"grade":"A","reason":"面向应届毕业生","evidence":"2027届应届毕业生","confidence":"高"}',
    )

    assert result.grade == "A"
    assert result.route == "优先待核验"
    assert result.evidence == "2027届应届毕业生"


def test_ai_screening_failure_falls_back_to_grade_c():
    def unavailable(_prompt):
        raise RuntimeError("model unavailable")

    result = screen_intake_with_ai("招聘公告", "请查看公告了解具体要求。", unavailable)

    assert result.grade == "C"
    assert result.route == "人工复核"
    assert "AI 初筛不可用" in result.reason


@pytest.fixture(params=[False, True], ids=["rules", "ai"])
def screen_student_recruitment(request):
    def screen(title, evidence_text):
        if not request.param:
            return screen_intake(title, evidence_text)
        return screen_intake_with_ai(
            title,
            evidence_text,
            lambda _: json.dumps(
                {
                    "grade": "A",
                    "reason": "明确面向应届毕业生开放投递",
                    "evidence": "应届毕业生",
                    "confidence": "高",
                }
            ),
        )

    return screen


@pytest.mark.parametrize(
    "progress_term", ["体检", "面试通知", "拟录取", "录用公示", "录取公示", "入职报到"]
)
def test_progress_title_stays_filtered_even_with_campus_application_context(
    screen_student_recruitment, progress_term
):
    result = screen_student_recruitment(
        f"2027年度校园招聘{progress_term}",
        "本次校园招聘面向应届毕业生。原简历投递截止日期为10月18日。",
    )

    assert result.grade == "D"
    assert result.evidence == progress_term


def test_open_campus_recruitment_with_later_exam_schedule_is_grade_a(
    screen_student_recruitment,
):
    result = screen_student_recruitment(
        "交通银行上海市分行2027年度校园招聘",
        "面向2027届应届毕业生，工作地点上海。校园宣讲：9-10月，"
        "简历投递截止日期：10月18日，综合评估：10-11月，体检通知：11-12月，发放offer。",
    )

    assert result.grade == "A"
    assert result.route == "优先待核验"


@pytest.mark.parametrize("hard_term", ["三年以上", "3年以上", "高级职称", "总监", "收费招聘", "培训贷"])
def test_senior_and_fee_requirements_stay_filtered_with_campus_context(
    screen_student_recruitment, hard_term
):
    result = screen_student_recruitment(
        "某银行招聘公告",
        f"本岗位要求：{hard_term}。旁文：校园招聘面向应届毕业生，简历投递截止日期为10月18日。",
    )

    assert result.grade == "D"
    assert result.evidence == hard_term


def test_body_progress_without_open_application_signal_stays_filtered(
    screen_student_recruitment,
):
    result = screen_student_recruitment(
        "2027年度校园招聘相关安排",
        "应届毕业生请按照体检通知参加检查。",
    )

    assert result.grade == "D"
