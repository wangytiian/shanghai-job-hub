import pytest

from app.config import Settings, SettingsError, load_settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_cloud_requires_an_explicit_non_sqlite_database_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "cloud")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SettingsError, match="DATABASE_URL"):
        load_settings()


def test_cloud_rejects_sqlite_database_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "cloud")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/recruiting_local.db")

    with pytest.raises(SettingsError, match="PostgreSQL"):
        load_settings()


def test_local_can_use_an_explicit_sqlite_database_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

    assert load_settings().database_url == "sqlite+pysqlite:///:memory:"


def test_settings_reject_unknown_environment():
    with pytest.raises(SettingsError, match="APP_ENV"):
        Settings(app_env="unknown", database_url="sqlite:///:memory:")


def test_cloud_application_uses_the_configured_database_url_when_not_overridden(monkeypatch):
    monkeypatch.setenv("APP_ENV", "cloud")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/recruiting")
    from app.main import create_app

    app = create_app()

    assert str(app.state.session_factory.kw["bind"].url) == "postgresql+psycopg://example.invalid/recruiting"


def test_local_application_keeps_the_project_absolute_default_database_path(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.main import DEFAULT_DATABASE_URL, create_app

    app = create_app()

    assert str(app.state.session_factory.kw["bind"].url) == DEFAULT_DATABASE_URL


def test_cloud_rejects_unlisted_host_header_when_allowed_hosts_are_configured():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(
            app_env="cloud",
            database_url="postgresql://example.invalid/test",
            allowed_hosts=("jobs.example.com",),
        ),
    )
    client = TestClient(app, base_url="https://jobs.example.com")

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live", headers={"host": "attacker.example"}).status_code == 400


def test_cloud_adds_basic_browser_security_headers():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    response = TestClient(app, base_url="https://testserver").get("/health/live")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_cloud_does_not_expose_openapi_schema():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )

    assert app.openapi_url is None
    assert TestClient(app, base_url="https://testserver").get("/openapi.json", follow_redirects=False).status_code == 303
