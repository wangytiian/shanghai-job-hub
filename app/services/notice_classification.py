from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DistributionItem, Job, ReviewLog

PROGRESS_KEYWORDS = ("体检", "面试", "录用", "公示", "拟录用", "成绩", "资格复审", "笔试")
RECRUITMENT_KEYWORDS = ("招聘", "招录", "校园", "实习", "应聘")


def suggest_notice_type(title: str, evidence_text: str) -> str:
    if any(keyword in title for keyword in PROGRESS_KEYWORDS):
        return "招聘进度通知"
    text = f"{title} {evidence_text}"[:2000]
    if any(keyword in text for keyword in RECRUITMENT_KEYWORDS):
        return "新招聘"
    return "待判断"


def suggested_new_recruitment_jobs(session: Session) -> list[Job]:
    candidates = session.scalars(
        select(Job).where(
            Job.is_demo.is_(False),
            Job.status == "待核验",
            Job.notice_type == "待判断",
        )
    ).all()
    return [
        job
        for job in candidates
        if suggest_notice_type(job.job_title, job.evidence_text) == "新招聘"
    ]


def confirm_suggested_new_recruitments(session: Session, operator_name: str) -> int:
    candidates = suggested_new_recruitment_jobs(session)
    for job in candidates:
        job.notice_type = "新招聘"
        session.add(
            ReviewLog(
                job_id=job.id,
                action="批量分类：新招聘",
                note="根据公告类型规则建议，经人工批量确认",
                operator_name=operator_name,
            )
        )
    session.commit()
    return len(candidates)


def classify_job(session: Session, job_id: int, notice_type: str, operator_name: str) -> Job:
    if notice_type not in {"新招聘", "招聘进度通知", "非招聘信息"}:
        raise ValueError("公告类型无效")
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError("岗位不存在")
    job.notice_type = notice_type
    if notice_type != "新招聘":
        job.status = "仅归档" if notice_type == "招聘进度通知" else "淘汰"
        session.query(DistributionItem).filter_by(job_id=job.id).delete()
    session.add(ReviewLog(job_id=job.id, action=f"分类：{notice_type}", note="人工确认公告类型", operator_name=operator_name))
    session.commit()
    return job
