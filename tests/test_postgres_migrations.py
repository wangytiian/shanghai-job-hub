from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


def test_initial_migration_creates_the_shared_workspace_schema(tmp_path: Path):
    database_path = tmp_path / "migration.db"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")

    from sqlalchemy import create_engine
    tables = set(inspect(create_engine(f"sqlite:///{database_path.as_posix()}")).get_table_names())
    assert {
        "users", "auth_sessions", "audit_events", "work_items", "jobs",
        "source_diagnostics", "source_trial_runs", "source_trial_samples",
    } <= tables
    from sqlalchemy import create_engine
    with create_engine(f"sqlite:///{database_path.as_posix()}").connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003_source_trials"
