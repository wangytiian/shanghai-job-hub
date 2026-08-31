from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import TaskRun
from app.seed import seed_demo_data


@dataclass(frozen=True)
class TaskRunResult:
    created_jobs: int
    updated_jobs: int


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
