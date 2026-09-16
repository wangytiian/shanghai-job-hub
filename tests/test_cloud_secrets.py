from pathlib import Path

import pytest

from app.services.credentials import FileCredentialStore, SecretProvider
from app.services.ai_settings import AiSettingsService
from app.auth.service import create_user
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_file_credential_store_reads_only_its_own_provider_secret(tmp_path: Path):
    bailian = tmp_path / "bailian.key"
    compatible = tmp_path / "compatible.key"
    bailian.write_text("bailian-secret\n", encoding="utf-8")
    compatible.write_text("compatible-secret\n", encoding="utf-8")
    provider = SecretProvider({"bailian": bailian, "openai_compatible": compatible})

    assert provider.for_provider("bailian").get_secret() == "bailian-secret"
    assert provider.for_provider("openai_compatible").get_secret() == "compatible-secret"


def test_cloud_file_secret_cannot_be_written_from_the_application(tmp_path: Path):
    secret = tmp_path / "model.key"
    secret.write_text("existing", encoding="utf-8")

    with pytest.raises(PermissionError):
        FileCredentialStore(secret).set_secret("replacement")


def test_ai_settings_uses_a_separate_store_for_each_provider(tmp_path: Path):
    bailian = tmp_path / "bailian.key"
    compatible = tmp_path / "compatible.key"
    bailian.write_text("bailian-secret", encoding="utf-8")
    compatible.write_text("compatible-secret", encoding="utf-8")
    provider = SecretProvider({"bailian": bailian, "openai_compatible": compatible})
    service = AiSettingsService(credential_store_factory=provider.for_provider)

    assert service._credential_store_for("bailian").get_secret() == "bailian-secret"
    assert service._credential_store_for("openai_compatible").get_secret() == "compatible-secret"


def test_cloud_rejects_browser_key_submission_and_explains_secret_file_management():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(app_env="cloud", database_url="postgresql://example.invalid/test"),
    )
    with app.state.session_factory() as session:
        create_user(session, "owner", "A secure password", "管理员", role="admin", must_change_password=False)
    client = TestClient(app, base_url="https://testserver")
    client.get("/login")
    client.post(
        "/login",
        data={
            "username": "owner",
            "password": "A secure password",
            "csrf_token": client.cookies.get("recruiting_login_csrf"),
        },
        follow_redirects=False,
    )
    csrf_token = client.cookies.get("recruiting_csrf")

    response = client.post(
        "/settings/ai/key",
        data={"api_key": "should-not-be-stored", "csrf_token": csrf_token},
    )

    assert response.status_code == 403
    page = client.get("/settings/ai")
    assert "云端密钥由部署端受控 Secret 文件管理" in page.text
    assert 'name="api_key"' not in page.text
