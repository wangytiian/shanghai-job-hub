import json
from dataclasses import dataclass


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


def build_structuring_prompt(title: str, source_url: str, evidence_text: str) -> str:
    return f"""你是招聘公告审核助手。只根据给出的标题、来源和原文提取信息，不得猜测或补充原文没有的事实。\n\n返回纯 JSON，不要 Markdown。字段：notice_type, employer_name, job_title, job_family, recruitment_type, location_category, location_detail, target_audience, direction_tags, deadline, official_url, posting_scope, attachment_status, application_method, application_contact, evidence, uncertainties。notice_type 只能是 新招聘、招聘进度通知、非招聘信息、待判断。posting_scope 只能是 single_role、multi_role_announcement、attachment_pending、insufficient_information、non_job_notice。attachment_status 只能是 not_required、pending、checked。application_method 只能是 official_page、email、official_platform、on_site、other、unknown。无法确认的字段填空字符串；岗位明细在附件但附件正文未提供时，posting_scope 填 attachment_pending，且不得把公告标题当作具体岗位名称。\n\n标题：{title}\n来源：{source_url}\n原文：{evidence_text[:6000]}"""


def parse_ai_draft(content: str) -> AnnouncementDraft:
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
    return AnnouncementDraft(**allowed)
