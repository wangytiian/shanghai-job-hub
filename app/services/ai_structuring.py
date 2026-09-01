import json
from dataclasses import dataclass

from app.services.student_fit import recommend_student_fit


@dataclass(frozen=True)
class AnnouncementDraft:
    notice_type: str = "待判断"
    employer_name: str = ""
    job_title: str = ""
    job_family: str = ""
    recruitment_type: str = ""
    location_category: str = ""
    location_detail: str = ""
    target_audience: str = ""
    direction_tags: str = ""
    deadline: str = ""
    official_url: str = ""
    posting_scope: str = "single_role"
    attachment_status: str = "not_required"
    application_method: str = "official_page"
    application_contact: str = ""
    evidence: str = ""
    uncertainties: str = ""
    student_fit_level: str = "待人工判断"
    distribution_recommendation: str = "仅保留资料库"
    rationale: str = ""
    confidence: str = "低"


def _derive_job_family(context: str, suggested: str) -> str:
    if suggested.strip():
        return suggested.strip()
    families: list[str] = []
    if any(term in context for term in ("教学", "科研", "教师", "讲师", "学术")):
        families.append("教育科研")
    if any(term in context for term in ("公共管理", "政府", "事业单位", "团校", "党校", "行政")):
        families.append("公共管理")
    if any(term in context for term in ("会计", "审计", "财务")):
        families.append("财会审计")
    if any(term in context for term in ("银行", "信贷", "金融")):
        families.append("金融银行")
    if any(term in context for term in ("数据", "算法", "开发", "测试", "技术")):
        families.append("数据技术")
    if any(term in context for term in ("法律", "法学", "合规", "法务")):
        families.append("法律合规")
    return "、".join(families[:2])


def _derive_location(context: str, category: str, detail: str) -> tuple[str, str]:
    normalized_category = category.strip()
    normalized_detail = detail.strip()
    if not normalized_category and "上海" in context:
        normalized_category = "明确上海"
    if normalized_category == "明确上海" and not normalized_detail:
        normalized_detail = "上海（具体地点以原文为准）"
    return normalized_category, normalized_detail


def build_structuring_prompt(title: str, source_url: str, evidence_text: str) -> str:
    return f"""你是招聘公告审核助手。先提取原文事实，再做运营分类建议。不得编造原文没有的薪资、编制、转正、户口、具体地址或截止日期。\n\n返回纯 JSON，不要 Markdown。字段：notice_type, employer_name, job_title, job_family, recruitment_type, location_category, location_detail, target_audience, direction_tags, deadline, official_url, posting_scope, attachment_status, application_method, application_contact, evidence, uncertainties, student_fit_level, distribution_recommendation, rationale, confidence。\n\n事实字段：岗位名称、资格、地点、截止日期、报名方式必须只来自原文。岗位族、地点分类、适合人群、学生适配和分发建议是运营建议。岗位族从 财会审计、金融银行、证券保险、税务评估、工商运营、数据技术、法律合规、公共管理、教育科研、综合职能 中选择最多两项。location_category 只能是 明确上海、可选上海、全国、其他地区、原文未明确。target_audience 可包含 大三实习、大四/应届生、毕业两年内、社会人员、博士毕业生/博士后、高级专业技术人员、需按具体岗位判断。student_fit_level 只能是 核心适配、补充适配、不适合核心学生用户、待人工判断；distribution_recommendation 只能是 进入学生分发审核、仅保留资料库、不进入学生分发；confidence 只能是 高、中、低。\n\n若出现博士后、副教授、高级职称、负责人或三年以上经验，学生适配不得写核心适配，分发建议不得进入学生分发审核。无法确认的事实字段填空字符串；岗位明细在附件但附件正文未提供时，posting_scope 填 attachment_pending，且不得把公告标题当作具体岗位名称。\n\n标题：{title}\n来源：{source_url}\n原文：{evidence_text[:6000]}"""


def parse_ai_draft(content: str, evidence_text: str = "") -> AnnouncementDraft:
    cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("AI 返回格式无效，请继续手工填写") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI 返回格式无效，请继续手工填写")
    allowed = {field: str(payload.get(field) or "").strip() for field in AnnouncementDraft.__dataclass_fields__}
    if allowed["notice_type"] not in {"新招聘", "招聘进度通知", "非招聘信息", "待判断"}:
        allowed["notice_type"] = "待判断"
    if allowed["posting_scope"] not in {"single_role", "multi_role_announcement", "attachment_pending", "insufficient_information", "non_job_notice"}:
        allowed["posting_scope"] = "insufficient_information"
    if allowed["attachment_status"] not in {"not_required", "pending", "checked"}:
        allowed["attachment_status"] = "pending"
    if allowed["application_method"] not in {"official_page", "email", "official_platform", "on_site", "other", "unknown"}:
        allowed["application_method"] = "unknown"
    context = f"{allowed['job_title']}\n{evidence_text}"
    allowed["job_family"] = _derive_job_family(context, allowed["job_family"])
    (
        allowed["location_category"],
        allowed["location_detail"],
    ) = _derive_location(context, allowed["location_category"], allowed["location_detail"])
    recommendation = recommend_student_fit(
        evidence_text,
        allowed["target_audience"],
        allowed["student_fit_level"],
        allowed["distribution_recommendation"],
        allowed["rationale"],
        allowed["confidence"],
    )
    allowed["student_fit_level"] = recommendation.student_fit_level
    allowed["distribution_recommendation"] = recommendation.distribution_recommendation
    allowed["rationale"] = recommendation.rationale
    allowed["confidence"] = recommendation.confidence
    return AnnouncementDraft(**allowed)
