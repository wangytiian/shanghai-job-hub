from app.auth.service import create_user
from app.database import create_database
from app.models import WorkItem
from app.services.work_queue import enqueue
from app.worker import run_once


def test_worker_marks_unregistered_job_needs_review():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        user = create_user(session, "member", "A secure password", "成员")
        work = enqueue(
            session, kind="unknown_kind", target_type="job", target_id=1, expected_row_version=1,
            expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="unknown:1",
            requested_by=user.id,
        )
        work_id = work.id
        session.commit()

    assert run_once(session_factory) is True
    with session_factory() as session:
        assert session.get(WorkItem, work_id).status == "needs_review"


def test_worker_executes_rule_score_job_and_completes_work_item():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        from app.seed import seed_demo_data
        from app.models import Job, TaskRun

        seed_demo_data(session)
        job = session.query(Job).first()
        job.is_demo = False
        job.status = "待核验"
        job.notice_type = "新招聘"
        job.intake_grade = "A"
        job.quality_score = 0
        user = create_user(session, "member", "A secure password", "成员")
        batch = TaskRun(task_name="AI建议分批次", status="已入队", message="等待处理")
        session.add(batch)
        session.flush()
        work = enqueue(
            session, kind="score_job", target_type="job", target_id=job.id, expected_row_version=job.row_version,
            expected_fact_version=job.version, config_revision="r1", template_version="", dedupe_key=f"score:{job.id}",
            requested_by=user.id, batch_id=batch.id,
        )
        work_id = work.id
        job_id = job.id
        batch_id = batch.id
        session.commit()

    assert run_once(session_factory) is True
    with session_factory() as session:
        assert session.get(WorkItem, work_id).status == "succeeded"
        assert session.get(Job, job_id).ai_score_status in {"规则建议", "不适用"}
        assert session.get(TaskRun, batch_id).status == "完成"


def test_worker_requeues_transient_handler_failure(monkeypatch):
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        user = create_user(session, "member", "A secure password", "成员")
        work = enqueue(session, kind="temporary_failure", target_type="job", target_id=1, expected_row_version=1, expected_fact_version=1, config_revision="r1", template_version="", dedupe_key="retry:1", requested_by=user.id)
        work_id = work.id
        session.commit()

    def fail(_session, _item):
        raise TimeoutError("provider timed out")

    monkeypatch.setitem(__import__("app.worker", fromlist=["HANDLERS"]).HANDLERS, "temporary_failure", fail)
    assert run_once(session_factory) is True
    with session_factory() as session:
        assert session.get(WorkItem, work_id).status == "queued"
