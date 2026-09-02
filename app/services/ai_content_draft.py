import json
from dataclasses import dataclass

from app.models import Job


@dataclass(frozen=True)
class ContentDraft:
    company_intro: str = ""
    role_summary: str = ""
    eligibility: str = ""
    career_advice: str = ""
    apply_tip: str = ""


SENSITIVE_UNSUPPORTED_TERMS = ("薪资", "工资", "编制", "转正", "落户", "户口", "五险一金", "专业不限")


def build_content_prompt(job: Job) -> str:
    return f'''你是面向上海大学生的招聘内容编辑。只根据“已核验字段”和“公开原文证据”提炼公众号内容；不要输出 HTML，不要增加没有写明的事实。

必须返回 JSON 对象，字段固定为：company_intro、role_summary、eligibility、career_advice、apply_tip。每个值都是 0-100 字的中文字符串；原文没有依据时填空字符串。

事实边界：不得补造薪资、编制、转正、落户、户口、五险一金、专业不限、截止时间、学历、届别、工作地点或企业背景。career_advice 只能给通用投递准备建议，不得冒充企业要求。

已核验字段：
招聘单位：{job.employer_name}
岗位：{job.job_title}
类型：{job.recruitment_type}
地点：{job.location_detail}
适合人群：{job.target_audience}
专业方向：{job.direction_tags}
截止：{job.deadline}
投递方式：{job.application_method}

公开原文证据：
{job.evidence_text}
'''


def _clean(value: object) -> str:
    return value.strip()[:100] if isinstance(value, str) else ""


def _safe_fact(value: str, evidence: str) -> str:
    if not value or any(term in value for term in SENSITIVE_UNSUPPORTED_TERMS):
        return ""
    # A fact paragraph must carry at least one meaningful phrase from the supplied evidence.
    compact = "".join(character for character in value if character.isalnum() or "\u4e00" <= character <= "\u9fff")
    anchors = (compact[index:index + size] for size in range(8, 2, -1) for index in range(max(0, len(compact) - size + 1)))
    if any(anchor in evidence for anchor in anchors):
        return value
    return ""


def parse_content_draft(content: str, job: Job) -> ContentDraft:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return ContentDraft()
    if not isinstance(payload, dict):
        return ContentDraft()
    evidence = "\n".join((job.employer_name, job.job_title, job.evidence_text, job.target_audience, job.direction_tags))
    company_intro = _safe_fact(_clean(payload.get("company_intro")), evidence)
    role_summary = _safe_fact(_clean(payload.get("role_summary")), evidence)
    eligibility = _safe_fact(_clean(payload.get("eligibility")), evidence)
    career_advice = _clean(payload.get("career_advice"))
    if any(term in career_advice for term in SENSITIVE_UNSUPPORTED_TERMS):
        career_advice = ""
    apply_tip = _clean(payload.get("apply_tip"))
    if any(term in apply_tip for term in SENSITIVE_UNSUPPORTED_TERMS):
        apply_tip = ""
    return ContentDraft(company_intro, role_summary, eligibility, career_advice, apply_tip)
