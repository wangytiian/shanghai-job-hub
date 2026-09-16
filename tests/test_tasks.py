from app.models import TaskRun
from app.services.tasks import mark_interrupted_task_runs


def test_startup_marks_unfinished_batch_runs_as_interrupted(session):
    session.add(TaskRun(task_name="AI建议分批次", status="处理中", message="正在逐条处理。"))
    session.commit()

    changed = mark_interrupted_task_runs(session)

    task = session.query(TaskRun).filter_by(task_name="AI建议分批次").one()
    assert changed == 1
    assert task.status == "中断"
    assert "重新处理失败或待建议记录" in task.message
