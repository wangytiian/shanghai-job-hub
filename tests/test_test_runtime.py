from app.config import Settings
from app.main import create_app
from app.services.credentials import FileCredentialStore


def test_test_runtime_can_use_file_backed_empty_credentials_without_windows_keyring():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        settings=Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            secret_backend="file",
        ),
    )

    store = app.state.ai_settings_service._credential_store_for("bailian")

    assert isinstance(store, FileCredentialStore)
    assert store.has_secret() is False
