from argparse import Namespace

from app.cli import create_admin
from app.database import create_database
from app.models import Source, User


def test_create_admin_creates_one_active_admin(monkeypatch):
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr("app.cli.create_session_factory", lambda _url: session_factory)
    monkeypatch.setattr("app.cli.getpass", lambda _prompt: "A secure password")

    create_admin(Namespace(username="owner", display_name="负责人", database_url="sqlite+pysqlite:///:memory:"))

    with session_factory() as session:
        user = session.query(User).one()
    assert (user.username, user.role, user.must_change_password) == ("owner", "admin", False)


def test_source_trial_cli_resolves_only_a_registered_source_key_and_uses_bounded_budget(monkeypatch, capsys):
    from app.cli import source_trial_command

    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        source = Source(
            name="上海交通大学就业网（待专用适配）",
            source_key="sjtu-internships",
            url="https://www.job.sjtu.edu.cn/",
            level="一级",
            source_type="高校就业平台",
            adapter_key="sjtu_internship_json",
            library_tier="B",
            is_enabled=False,
        )
        session.add(source)
        session.commit()
    captured = {}
    monkeypatch.setattr("app.cli.create_session_factory", lambda _url: session_factory)
    monkeypatch.setattr(
        "app.cli.run_source_trial",
        lambda sf, client, source_id, requested_by, budget, idempotency_key: captured.update(
            source_id=source_id,
            requested_by=requested_by,
            budget=budget,
            idempotency_key=idempotency_key,
        ) or "run-123",
    )

    source_trial_command(
        Namespace(
            database_url="sqlite+pysqlite:///:memory:",
            source_key="sjtu-internships",
            requested_by="operator",
            pages=1,
            list_limit=10,
            detail_limit=3,
            total_seconds=120,
            idempotency_key="cli-task-1",
        )
    )

    assert captured["source_id"] == source.id
    assert captured["budget"].detail_limit == 3
    assert captured["idempotency_key"] == "cli-task-1"
    assert "run-123" in capsys.readouterr().out
