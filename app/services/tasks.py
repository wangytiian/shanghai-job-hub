from dataclasses import dataclass

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import TaskRun
from app.seed import seed_demo_data


@dataclass(frozen=True)
class TaskRunResult:
    created_jobs: int
    updated_jobs: int


def mark_interrupted_task_runs(session: Session) -> int:
    """Make single-process interruption visible after the local app restarts."""
    interrupted = session.scalars(select(TaskRun).where(TaskRun.status == "处理中")).all()
    for task in interrupted:
        task.status = "中断"
        task.message = (
            f"{task.message} 应用在任务完成前退出；请检查已生成结果，并重新处理失败或待建议记录。"
        )
    if interrupted:
        session.commit()
    return len(interrupted)


def run_demo_collection(session: Session) -> TaskRunResult:
    result = seed_demo_data(session)
    session.add(
        TaskRun(
            task_name="模拟采集",
            status="完成",
            message=f"新增 {result.created_jobs} 条，更新 {result.updated_jobs} 条演示岗位。",
        )
    )
    session.commit()
    return TaskRunResult(
        created_jobs=result.created_jobs,
        updated_jobs=result.updated_jobs,
    )
