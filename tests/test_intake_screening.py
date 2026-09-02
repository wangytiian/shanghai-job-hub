from app.services.intake_screening import screen_intake


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
