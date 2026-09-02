from app.models import Job
from app.services.ai_content_draft import build_content_prompt, parse_content_draft


def _job() -> Job:
    return Job(
        fingerprint="ai-content-1",
        employer_name="示例商业银行",
        job_title="金融分析实习生",
        job_family="财务分析",
        recruitment_type="暑期实习",
        location_category="明确上海",
        location_detail="上海",
        target_audience="大三实习",
        direction_tags="金融银行、会计审计",
        deadline="2026-11-27",
        official_url="https://example.com/apply",
        source_url="https://example.com/source",
        evidence_text="示例商业银行现招聘金融分析实习生，工作地点上海。面向2027届本科及硕士在读学生，金融、会计相关专业优先。",
        quality_score=90,
        risk_flags="",
    )


def test_content_prompt_requires_only_evidence_backed_facts():
    prompt = build_content_prompt(_job())

    assert "不得补造" in prompt
    assert "薪资" in prompt
    assert "金融分析实习生" in prompt


def test_content_draft_discards_sensitive_unsupported_claims():
    draft = parse_content_draft(
        '{"company_intro":"示例商业银行提供20k薪资和上海落户。",'
        '"role_summary":"金融分析实习生在上海参与分析工作。",'
        '"eligibility":"面向2027届本科及硕士在读学生，金融、会计相关专业优先。",'
        '"career_advice":"投递前准备好与金融分析相关的项目经历。",'
        '"apply_tip":"请以官方页面要求为准。"}',
        _job(),
    )

    assert draft.company_intro == ""
    assert "金融分析实习生" in draft.role_summary
    assert "2027届" in draft.eligibility
    assert draft.career_advice
