from datetime import date
import json

from app.models import Job
from app.services.deadline_policy import job_application_deadline
from app.services.screenshot_review_policy import has_current_review_confirmation


UNSPECIFIED_DEADLINE = "公告未明确统一截止时间"


PLACEHOLDER_VALUES = {
    "以公告原文为准",
    "待人工判断",
    "待分类",
    "待核验",
    "原文待人工确认",
    "待人工分类",
    "地区待定",
    "原文未明确",
}

REQUIRED_VERIFICATION_CHECKS = {
    "source_checked": "原始来源",
    "scope_checked": "岗位或公告范围",
    "audience_checked": "面向学生人群",
    "location_checked": "工作地点",
    "application_checked": "官方投递入口",
    "timeliness_checked": "时效",
}


def verification_checks_complete(value: str) -> bool:
    try:
        checks = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(checks, dict) and all(checks.get(key) is True for key in REQUIRED_VERIFICATION_CHECKS)


def validate_publishable(job: Job, today: date | None = None) -> list[str]:
    errors: list[str] = []
    if job.intake_grade not in {"A", "B"}:
        errors.append("只有 A/B 分级岗位可以发布")
    if "[TARGET_AUDIENCE_UNCLEAR]" in (job.intake_reason or ""):
        errors.append("学生受众尚未人工确认")
    if not job.source_url.strip():
        errors.append("缺少来源链接")
    if not job.official_url.strip():
        errors.append("缺少官方链接")
    if not job.evidence_text.strip():
        errors.append("缺少原文证据")
    guided_current = has_current_review_confirmation(job)
    try:
        snapshot = json.loads(job.confirmed_content_json or "{}")
        guided_present = isinstance(snapshot, dict) and "review_confirmation" in snapshot
    except (ValueError, TypeError):
        guided_present = False
    if guided_present and not guided_current:
        errors.append("截图一次终审证明已失效，请按当前事实重新核验")
    if job.quality_score < 70 and not guided_current:
        errors.append("质量分不足70")
    if any(flag in job.risk_flags for flag in ("尚未人工核验", "不得对外发布", "未解决")):
        errors.append("存在尚未人工核验或未解决风险")
    if job.posting_scope in {"attachment_pending", "insufficient_information", "non_job_notice"}:
        errors.append("公告范围不满足发布条件")
    if job.attachment_status == "pending":
        errors.append("附件尚未核验")
    if job.posting_scope == "multi_role_announcement" and job.attachment_status != "checked":
        try:
            details_checked = job.role_details_status == "checked" and bool(json.loads(job.role_details_evidence or "[]"))
        except (ValueError, TypeError):
            details_checked = False
        if job.attachment_status != "not_required" or not details_checked:
            errors.append("多岗位明细尚未核验")
    if job.confirmed_content_json and job.confirmed_content_version != job.version:
        errors.append("已确认内容快照已过期，请重新核验")
    if not verification_checks_complete(job.verification_checks):
        errors.append("人工核验清单未完整确认")
    elif job.verification_version != job.version:
        errors.append("核验结果已过期，请按最新公告事实重新确认")
    if job.student_fit_level not in {"核心适配", "补充适配", "不适合核心学生用户"} or job.distribution_recommendation not in {
        "进入学生分发审核",
        "仅保留资料库",
        "不进入学生分发",
    }:
        errors.append("学生适配或分发建议未确认")
    for value in (job.job_title, job.target_audience, job.location_detail, job.deadline):
        if value.strip() in PLACEHOLDER_VALUES:
            errors.append("存在占位字段")
            break
    if job.posting_scope == "single_role" and not job.job_title.strip():
        errors.append("缺少明确岗位名称")
    if job.application_method == "email" and not job.application_contact.strip():
        errors.append("缺少报名邮箱")
    deadline = job_application_deadline(job)
    if deadline is not None and deadline < (today or date.today()):
        errors.append("报名已截止")
    return errors
