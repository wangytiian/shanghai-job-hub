from dataclasses import dataclass
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


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label}不能为空")
    return normalized


def _normalize_deadline(value: str) -> str:
    return value.strip() or UNSPECIFIED_DEADLINE


def _validate_input(data: StructuringInput) -> None:
    _required(data.official_url, "官方报名链接")
    _required(data.target_audience, "适合人群")
    if urlparse(data.official_url.strip()).scheme not in {"http", "https"}:
        raise ValueError("官方报名链接必须以 http:// 或 https:// 开头")
    if data.posting_scope == "attachment_pending":
        raise ValueError("附件尚未核验，补齐岗位明细后才能进入待审核")
    if data.posting_scope in {"insufficient_information", "non_job_notice"}:
        raise ValueError("该公告信息尚不完整，补充事实后才能进入待审核")
    if data.posting_scope == "multi_role_announcement" and data.attachment_status != "checked":
        raise ValueError("多岗位公告必须先完成附件核验")
    if data.application_method == "email" and not data.application_contact.strip():
        raise ValueError("邮箱投递必须填写报名邮箱")
    if not 0 <= data.quality_score <= 100:
        raise ValueError("质量分必须在0到100之间")


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
    ):
        setattr(job, field_name, getattr(data, field_name).strip())
    job.deadline = normalized_deadline
    job.quality_score = data.quality_score
    job.risk_flags = "待最终人工审核：结构化字段已由运营人员补齐"
    job.status = "待审核"
    job.last_change_summary = "人工完成公告结构化，等待最终审核"
    session.add(
        ReviewLog(
            job_id=job.id,
            action="结构化完成",
            note=data.note.strip(),
            operator_name=operator_name,
        )
    )
    session.commit()
    return job
