from typing import Literal
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Job, ReviewLog
from app.services.jobs import validate_publishable


ReviewAction = Literal["approve", "return", "reject"]


def review_job(
    session: Session,
    job_id: int,
    action: ReviewAction,
    note: str,
    operator_name: str,
) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    if job.status == "淘汰":
        raise ValueError("淘汰岗位不能再次变更")

    if action == "approve":
        if job.status != "待审核":
            raise ValueError("只有待审核岗位可以通过")
        if not note.strip():
            raise ValueError("通过前必须填写核验备注")
        errors = validate_publishable(job)
        if errors:
            raise ValueError("；".join(errors))
        job.status = "可发布"
        job.last_verified_at = datetime.now()
        log_action = "通过"
    elif action == "return":
        if job.status != "待审核":
            raise ValueError("只有待审核岗位可以退回")
        job.status = "待核验"
        log_action = "退回"
    elif action == "reject":
        if job.status not in {"待审核", "待核验"}:
            raise ValueError("当前状态不能淘汰")
        job.status = "淘汰"
        log_action = "淘汰"
    else:
        raise ValueError("未知审核动作")

    session.add(
        ReviewLog(
            job_id=job.id,
            action=log_action,
            note=note,
            operator_name=operator_name,
        )
    )
    session.commit()
    return job
