from app.models import Job


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


def validate_publishable(job: Job) -> list[str]:
    errors: list[str] = []
    if not job.source_url.strip():
        errors.append("缺少来源链接")
    if not job.official_url.strip():
        errors.append("缺少官方链接")
    if not job.evidence_text.strip():
        errors.append("缺少原文证据")
    if job.quality_score < 70:
        errors.append("质量分不足70")
    if any(flag in job.risk_flags for flag in ("尚未人工核验", "不得对外发布", "未解决")):
        errors.append("存在尚未人工核验或未解决风险")
    if job.posting_scope in {"attachment_pending", "insufficient_information", "non_job_notice"}:
        errors.append("公告范围不满足发布条件")
    if job.attachment_status == "pending":
        errors.append("附件尚未核验")
    for value in (job.job_title, job.target_audience, job.location_detail, job.deadline):
        if value.strip() in PLACEHOLDER_VALUES:
            errors.append("存在占位字段")
            break
    if job.posting_scope == "single_role" and not job.job_title.strip():
        errors.append("缺少明确岗位名称")
    if job.application_method == "email" and not job.application_contact.strip():
        errors.append("缺少报名邮箱")
    return errors
