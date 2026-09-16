from datetime import timedelta

import pytest

from app.auth.service import (
    AuthenticationError,
    create_user,
    issue_session,
    resolve_session,
    revoke_user_sessions,
)
from app.config import Settings
from app.main import create_app
from app.models import AuditEvent, User
from fastapi.testclient import TestClient


def cloud_login(client: TestClient, username: str, password: str):
    client.get("/login")
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": client.cookies.get("recruiting_login_csrf")},
        follow_redirects=False,
    )


def test_issued_session_resolves_to_active_user(session):
    user = create_user(session, "operator", "A secure password", "运营同学")

    raw_token, _csrf_token = issue_session(session, user)

    assert resolve_session(session, raw_token).id == user.id


def test_revoking_user_sessions_invalidates_existing_token(session):
    user = create_user(session, "operator", "A secure password", "运营同学")
    raw_token, _csrf_token = issue_session(session, user)

    revoke_user_sessions(session, user.id)

    with pytest.raises(AuthenticationError):
        resolve_session(session, raw_token)


def test_inactive_user_cannot_resolve_existing_session(session):
    user = create_user(session, "operator", "A secure password", "运营同学")
    raw_token, _csrf_token = issue_session(session, user)
    user.is_active = False
    session.commit()

    with pytest.raises(AuthenticationError):
        resolve_session(session, raw_token)


def test_usernames_are_normalized_and_unique(session):
    create_user(session, " Operator ", "A secure password", "运营同学")

    with pytest.raises(ValueError, match="用户名"):
        create_user(session, "operator", "Another secure password", "另一个")


def test_expired_session_is_rejected(session):
    user = create_user(session, "operator", "A secure password", "运营同学")
    raw_token, _csrf_token = issue_session(session, user, absolute_lifetime=timedelta(seconds=-1))

    with pytest.raises(AuthenticationError):
        resolve_session(session, raw_token)


def test_cloud_redirects_anonymous_business_requests_and_accepts_login():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/", follow_redirects=False).status_code == 303
    response = cloud_login(client, "owner", "A secure password")

    assert response.status_code == 303
    home = client.get("/")
    assert home.status_code == 200
    assert "管理员" in home.text
    assert "成员账号" in home.text


def test_cloud_rejects_member_access_to_ai_settings():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A secure password")

    assert client.get("/settings/ai").status_code == 403


def test_cloud_rejects_post_without_csrf_and_accepts_matching_header():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A secure password")

    assert client.post("/tasks/demo-collection").status_code == 403
    token = client.cookies.get("recruiting_csrf")
    assert client.post("/tasks/demo-collection", headers={"X-CSRF-Token": token}, follow_redirects=False).status_code == 303


def test_cloud_admin_can_create_an_invited_reviewer_account():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "owner", "A secure password")

    response = client.post(
        "/users",
        data={
            "username": "reviewer", "display_name": "审核同学", "temporary_password": "A temporary password",
            "can_review": "on", "csrf_token": client.cookies.get("recruiting_csrf"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app.state.session_factory() as database_session:
        reviewer = database_session.query(User).filter_by(username="reviewer").one()
    assert reviewer.can_review is True
    assert reviewer.must_change_password is True


def test_admin_can_disable_member_and_their_existing_session_stops_working():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        owner = create_user(database_session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
        member = create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
        member_id = member.id
    owner_client = TestClient(app, base_url="https://testserver")
    member_client = TestClient(app, base_url="https://testserver")
    cloud_login(owner_client, "owner", "A secure password")
    cloud_login(member_client, "member", "A secure password")

    response = owner_client.post(
        f"/users/{member_id}/disable",
        data={"csrf_token": owner_client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert member_client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_admin_cannot_disable_the_last_active_admin():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        owner = create_user(database_session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
        owner_id = owner.id
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "owner", "A secure password")

    response = client.post(
        f"/users/{owner_id}/disable",
        data={"csrf_token": client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app.state.session_factory() as database_session:
        assert database_session.get(User, owner_id).is_active is True


def test_admin_password_reset_revokes_old_session_and_requires_new_password_change():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
        member = create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
        member_id = member.id
    owner_client = TestClient(app, base_url="https://testserver")
    member_client = TestClient(app, base_url="https://testserver")
    cloud_login(owner_client, "owner", "A secure password")
    cloud_login(member_client, "member", "A secure password")

    response = owner_client.post(
        f"/users/{member_id}/reset-password",
        data={"temporary_password": "Another secure password", "csrf_token": owner_client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert member_client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert cloud_login(member_client, "member", "Another secure password").status_code == 303
    assert member_client.get("/", follow_redirects=False).headers["location"] == "/account/password"
    with app.state.session_factory() as database_session:
        event = database_session.query(AuditEvent).filter_by(action="account.password_reset").one()
        assert event.actor_label == "管理员"
        assert "Another secure password" not in event.changes
        assert '"must_change_password": true' in event.changes


def test_invited_user_must_change_temporary_password_before_using_workbench():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "member", "A temporary password", "成员", must_change_password=True)
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A temporary password")

    assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"


def test_cloud_logout_revokes_the_server_session_and_clears_browser_cookies():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A secure password")
    session_token = client.cookies.get("recruiting_session")

    response = client.post(
        "/logout",
        data={"csrf_token": client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    with app.state.session_factory() as database_session:
        with pytest.raises(AuthenticationError):
            resolve_session(database_session, session_token)


def test_cloud_score_batch_enqueues_work_without_scoring_in_the_web_request():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        from app.seed import seed_demo_data
        from app.models import Job, WorkItem

        seed_demo_data(database_session)
        job = database_session.query(Job).first()
        job.is_demo = False
        job.status = "待核验"
        job.notice_type = "新招聘"
        job.intake_grade = "A"
        job.quality_score = 0
        create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
        database_session.commit()
        job_id = job.id
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A secure password")

    response = client.post(
        "/jobs/scoring/suggest-batch",
        data={"csrf_token": client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app.state.session_factory() as database_session:
        work = database_session.query(WorkItem).filter_by(target_id=job_id, kind="score_job").one()
        assert work.status == "queued"
        assert database_session.get(Job, job_id).ai_score_status == "处理中"
        assert response.headers["location"] == f"/tasks/{work.batch_id}"


def test_member_can_read_a_task_batch_detail_without_task_submission_permission_bypass():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as database_session:
        from app.models import TaskRun, WorkItem

        member = create_user(database_session, "member", "A secure password", "成员", must_change_password=False)
        batch = TaskRun(task_name="AI建议分批次", status="已入队", message="等待 worker")
        database_session.add(batch)
        database_session.flush()
        database_session.add(
            WorkItem(
                batch_id=batch.id, kind="score_job", target_type="job", target_id=1,
                expected_row_version=1, expected_fact_version=1, config_revision="r1", template_version="",
                dedupe_key="detail:1", requested_by=member.id, status="queued",
            )
        )
        database_session.commit()
        batch_id = batch.id
    client = TestClient(app, base_url="https://testserver")
    cloud_login(client, "member", "A secure password")

    response = client.get(f"/tasks/{batch_id}")

    assert response.status_code == 200
    assert "score_job" in response.text
    assert "排队中" in response.text
