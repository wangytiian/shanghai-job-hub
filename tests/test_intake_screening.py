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
