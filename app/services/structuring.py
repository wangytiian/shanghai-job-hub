from dataclasses import dataclass
import json
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import Job, ReviewLog
from app.services.jobs import UNSPECIFIED_DEADLINE


@dataclass(frozen=True)
class StructuringInput:
    employer_name: str
    job_title: str
    job_family: str
    recruitment_type: str
    location_category: str
    location_detail: str
    target_audience: str
    direction_tags: str
    deadline: str
    official_url: str
    posting_scope: str = "single_role"
    attachment_status: str = "not_required"
    application_method: str = "official_page"
    application_contact: str = ""
    quality_score: int = 0
    note: str = ""
    student_fit_level: str = "待人工判断"
    distribution_recommendation: str = "仅保留资料库"
    ai_rationale: str = ""
    ai_confidence: str = "低"
    verification_checks: dict[str, bool] | None = None


class StructuringValidationError(ValueError):
    """Validation failures that can be rendered beside their matching form field."""

    def __init__(self, field_errors: dict[str, str]):
        self.field_errors = field_errors
        super().__init__("；".join(field_errors.values()))


def _normalize_deadline(value: str) -> str:
    return value.strip() or UNSPECIFIED_DEADLINE


def _validate_input(data: StructuringInput) -> None:
    errors: dict[str, str] = {}

    for field_name, label in (
        ("employer_name", "招聘单位标准名称"),
        ("job_title", "岗位名称"),
        ("job_family", "岗位族"),
        ("recruitment_type", "招聘类型"),
        ("location_category", "地点分类"),
        ("location_detail", "工作地点"),
        ("target_audience", "适合人群"),
        ("direction_tags", "专业方向"),
        ("official_url", "官方报名链接"),
    ):
        if not getattr(data, field_name).strip():
            errors[field_name] = f"{label}不能为空"

    if data.official_url.strip() and urlparse(data.official_url.strip()).scheme not in {"http", "https"}:
        errors["official_url"] = "官方报名链接必须以 http:// 或 https:// 开头"
    if data.posting_scope == "attachment_pending":
        errors["posting_scope"] = "附件尚未核验，补齐岗位明细后才能进入待审核"
    if data.posting_scope in {"insufficient_information", "non_job_notice"}:
        errors["posting_scope"] = "该公告信息尚不完整，补充事实后才能进入待审核"
    if data.posting_scope == "multi_role_announcement" and data.attachment_status != "checked":
        errors["attachment_status"] = "多岗位公告必须先完成附件核验"
    if data.application_method == "email" and not data.application_contact.strip():
        errors["application_contact"] = "邮箱投递必须填写报名邮箱"
    if not 0 <= data.quality_score <= 100:
        errors["quality_score"] = "质量分必须在0到100之间"
    if data.student_fit_level not in {"核心适配", "补充适配", "不适合核心学生用户", "待人工判断"}:
        errors["student_fit_level"] = "学生适配选择无效"
    if data.distribution_recommendation not in {"进入学生分发审核", "仅保留资料库", "不进入学生分发"}:
        errors["distribution_recommendation"] = "分发建议选择无效"
    if data.ai_confidence not in {"高", "中", "低"}:
        errors["ai_confidence"] = "AI 判断置信度无效"
    checks = data.verification_checks or {}
    required_checks = {
        "source_checked": "原始来源",
        "scope_checked": "岗位或公告范围",
        "audience_checked": "面向学生人群",
        "location_checked": "工作地点",
        "application_checked": "官方投递入口",
        "timeliness_checked": "时效",
    }
    missing_checks = [label for key, label in required_checks.items() if not checks.get(key)]
    if missing_checks:
        errors["verification_checks"] = f"请确认：{'、'.join(missing_checks)}"
    if errors:
        raise StructuringValidationError(errors)


def structure_job(
    session: Session,
    job_id: int,
    data: StructuringInput,
    operator_name: str,
) -> Job:
    _validate_input(data)
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    if job.status != "待核验":
        raise ValueError("只有待核验公告可以结构化")
    if job.notice_type != "新招聘":
        raise ValueError("只有确认为新招聘的公告可以结构化")

    normalized_deadline = _normalize_deadline(data.deadline)
    for field_name in (
        "employer_name",
        "job_title",
        "job_family",
        "recruitment_type",
        "location_category",
        "location_detail",
        "target_audience",
        "direction_tags",
        "official_url",
        "posting_scope",
        "attachment_status",
        "application_method",
        "application_contact",
        "student_fit_level",
        "distribution_recommendation",
        "ai_rationale",
        "ai_confidence",
    ):
        setattr(job, field_name, getattr(data, field_name).strip())
    job.deadline = normalized_deadline
    job.quality_score = data.quality_score
    job.verification_checks = json.dumps(data.verification_checks or {}, ensure_ascii=False, sort_keys=True)
    job.risk_flags = "待最终人工审核：结构化字段已由运营人员补齐"
    job.status = "待审核"
    job.last_change_summary = "人工完成公告结构化，等待最终审核"
    session.add(
        ReviewLog(
            job_id=job.id,
            action="人工核验清单已确认",
            note=job.verification_checks,
            operator_name=operator_name,
        )
    )
    session.add(
        ReviewLog(
            job_id=job.id,
            action="结构化完成",
            note=data.note.strip(),
            operator_name=operator_name,
        )
    )
    session.add(
        ReviewLog(
            job_id=job.id,
            action="AI建议已确认",
            note=(
                f"学生适配：{job.student_fit_level}；分发建议：{job.distribution_recommendation}；"
                f"置信度：{job.ai_confidence}；依据：{job.ai_rationale}"
            ),
            operator_name=operator_name,
        )
    )
    session.commit()
    return job
