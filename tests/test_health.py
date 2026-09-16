from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_cloud_health_endpoints_do_not_require_login():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").json()["status"] == "ready"
