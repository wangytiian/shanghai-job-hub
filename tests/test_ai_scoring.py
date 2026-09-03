from datetime import datetime

from app.models import Job
from app.services.ai_scoring import suggest_job_score


def make_job(**overrides) -> Job:
    values = {
        "fingerprint": "score-test",
        "employer_name": "上海示例银行",
        "job_title": "暑期实习生",
        "job_family": "待分类",
        "recruitment_type": "暑期实习",
        "location_category": "明确上海",
        "location_detail": "上海",
        "target_audience": "待人工判断",
        "direction_tags": "待人工分类",
        "deadline": "原文待人工确认",
        "official_url": "",
        "source_url": "https://careers.example.com/job/1",
        "evidence_text": "面向2027届应届生的上海暑期实习招聘，欢迎金融和会计专业学生申请。",
        "quality_score": 0,
        "risk_flags": "真实线索：尚未人工核验，不得对外发布",
        "is_demo": False,
        "collected_at": datetime.now(),
        "status": "待核验",
        "notice_type": "新招聘",
        "intake_grade": "A",
    }
    values.update(overrides)
    return Job(**values)


def test_ai_suggested_score_combines_rule_and_evidence_backed_ai_points():
    job = make_job()

    result = suggest_job_score(
        job,
        complete=lambda _prompt: '{"student_fit_points":24,"value_points":8,"reason":"面向应届生的上海实习岗位。","evidence":"面向2027届应届生","confidence":"高"}',
    )

    assert result.eligible is True
    assert result.status == "AI建议"
    assert result.score >= 80
    assert result.breakdown["学生适配"] == 24
    assert result.breakdown["岗位价值"] == 8


def test_d_grade_is_not_sent_to_ai_scoring():
    job = make_job(intake_grade="D", evidence_text="体检通知")

    result = suggest_job_score(job, complete=lambda _prompt: (_ for _ in ()).throw(AssertionError()))

    assert result.eligible is False
    assert result.status == "不适用"
    assert result.score == 0


def test_invalid_ai_evidence_falls_back_to_rule_suggestion():
    job = make_job()

    result = suggest_job_score(
        job,
        complete=lambda _prompt: '{"student_fit_points":25,"value_points":10,"reason":"无依据","evidence":"原文不存在"}',
    )

    assert result.eligible is True
    assert result.status == "规则建议"
    assert result.confidence == "低"
    assert result.breakdown["学生适配"] == 0


def test_ai_suggested_score_accepts_multiple_verifiable_evidence_fragments():
    job = make_job()

    result = suggest_job_score(
        job,
        complete=lambda _prompt: '{"student_fit_points":20,"value_points":8,"reason":"原文存在学生实习信号。","evidence":["面向2027届应届生","上海暑期实习"],"confidence":"高"}',
    )

    assert result.status == "AI建议"
    assert result.breakdown["学生适配"] == 20


def test_c_grade_suggested_score_is_capped_below_priority_threshold():
    job = make_job(intake_grade="C")

    result = suggest_job_score(
        job,
        complete=lambda _prompt: '{"student_fit_points":25,"value_points":10,"reason":"原文适配。","evidence":"应届生","confidence":"中"}',
    )

    assert result.score == 69
