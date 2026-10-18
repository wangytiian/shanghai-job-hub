"""The module-level ASGI app must never initialise the operator's database in tests."""

import os
from pathlib import Path
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[1]


def test_global_app_uses_explicit_test_database(tmp_path):
    database = tmp_path / "isolated.sqlite"
    env = dict(os.environ, APP_ENV="test", DATABASE_URL=f"sqlite:///{database.as_posix()}",
               SECRET_BACKEND="file", SCREENSHOT_EXECUTOR_ENABLED="0")
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import app; print(app.state.session_factory.kw['bind'].url)"],
        cwd=PROJECT, env=env, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert str(database).replace("\\", "/") in result.stdout.replace("\\", "/")


def test_test_settings_default_to_memory(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.config import load_settings
    assert load_settings().database_url.endswith(":memory:")


def test_test_environment_rejects_operator_database():
    from app.main import DEFAULT_DATABASE_URL, create_app
    from app.config import Settings, SettingsError
    import pytest
    with pytest.raises(SettingsError, match="业务|operator|production"):
        create_app(settings=Settings(app_env="test", database_url=DEFAULT_DATABASE_URL))
