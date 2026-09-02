import pytest

from app.database import create_database
from app.models import AiProviderSetting
from app.services.ai_settings import (
    AiSettingsService,
    CredentialNotConfiguredError,
    OPENAI_COMPATIBLE_PROVIDER,
    mask_api_key,
)


class FakeCredentialStore:
    def __init__(self):
        self.secret = None

    def set_secret(self, value: str) -> None:
        self.secret = value

    def get_secret(self) -> str | None:
        return self.secret

    def has_secret(self) -> bool:
        return bool(self.secret)


class FakeBailianClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def test_connection(self, api_key: str, model: str) -> None:
        self.calls.append((api_key, model))
        if self.error:
            raise self.error


class FakeOpenAICompatibleClient:
    def __init__(self):
        self.calls = []

    def test_connection(self, api_key: str, base_url: str, model: str) -> None:
        self.calls.append((api_key, base_url, model))

    def complete(self, api_key: str, base_url: str, model: str, prompt: str) -> str:
        self.calls.append((api_key, base_url, model, prompt))
        return '{"ok": true}'


def test_mask_api_key_exposes_only_last_four_characters():
    assert mask_api_key("sk-1234567890ABCD") == "****ABCD"
    assert mask_api_key("123") == "****"


def test_save_api_key_uses_credential_store_and_only_masked_value_in_database():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    service = AiSettingsService(store)

    with session_factory() as session:
        saved = service.save_api_key(session, "sk-a-very-private-key-ABCD")
        database_row = session.get(AiProviderSetting, saved.id)

    assert store.secret == "sk-a-very-private-key-ABCD"
    assert database_row.key_masked == "****ABCD"
    assert "private" not in database_row.key_masked
    assert "api_key" not in AiProviderSetting.__table__.columns


def test_save_api_key_rejects_empty_value_without_replacing_existing_secret():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    service = AiSettingsService(store)

    with session_factory() as session:
        service.save_api_key(session, "sk-existing-key-ABCD")
        with pytest.raises(ValueError, match="API Key"):
            service.save_api_key(session, "   ")

    assert store.secret == "sk-existing-key-ABCD"


def test_save_models_rejects_model_outside_allowlist():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    service = AiSettingsService(FakeCredentialStore())

    with session_factory() as session:
        with pytest.raises(ValueError, match="text model"):
            service.save_models(session, "other-model", "qwen-vl-ocr", True, True)
        with pytest.raises(ValueError, match="OCR model"):
            service.save_models(session, "qwen3.7-flash", "other-model", True, True)


def test_connection_requires_saved_key_without_calling_provider():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    client = FakeBailianClient()
    service = AiSettingsService(FakeCredentialStore(), client)

    with session_factory() as session:
        with pytest.raises(CredentialNotConfiguredError):
            service.test_connection(session)

    assert client.calls == []


def test_active_text_provider_is_not_ready_until_its_key_is_saved():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    service = AiSettingsService(FakeCredentialStore())

    with session_factory() as session:
        assert service.is_active_text_provider_ready(session) is False
        service.save_api_key(session, "sk-private-test-key-ABCD")
        assert service.is_active_text_provider_ready(session) is True


def test_connection_marks_setting_ready_after_success():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    client = FakeBailianClient()
    service = AiSettingsService(store, client)

    with session_factory() as session:
        service.save_api_key(session, "sk-private-test-key-ABCD")
        saved = service.test_connection(session)

    assert client.calls == [("sk-private-test-key-ABCD", "qwen3.7-flash")]
    assert saved.connection_status == "ready"
    assert saved.last_error_summary == ""
    assert saved.last_tested_at is not None


def test_connection_sanitizes_secret_from_provider_failure():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    secret = "sk-private-test-key-ABCD"
    client = FakeBailianClient(RuntimeError(f"authorization failed for {secret}"))
    service = AiSettingsService(store, client)

    with session_factory() as session:
        service.save_api_key(session, secret)
        saved = service.test_connection(session)

    assert saved.connection_status == "error"
    assert secret not in saved.last_error_summary
    assert "authorization failed" in saved.last_error_summary


def test_openai_compatible_provider_keeps_key_out_of_database_and_tests_its_config():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    client = FakeOpenAICompatibleClient()
    service = AiSettingsService(store, openai_client=client)

    with session_factory() as session:
        saved = service.save_openai_settings(
            session,
            api_key="sk-private-gpt-key-ABCD",
            base_url="https://gateway.example.test/v1/",
            text_model="gpt-5.5",
            text_enabled=True,
            make_active=True,
        )
        tested = service.test_connection(session, OPENAI_COMPATIBLE_PROVIDER)
        database_row = session.get(AiProviderSetting, saved.id)

    assert database_row.base_url == "https://gateway.example.test/v1"
    assert database_row.key_masked == "****ABCD"
    assert database_row.is_active_text_provider is True
    assert "private" not in database_row.key_masked
    assert tested.connection_status == "ready"
    assert client.calls == [
        ("sk-private-gpt-key-ABCD", "https://gateway.example.test/v1", "gpt-5.5")
    ]


def test_complete_text_uses_current_openai_compatible_provider():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    client = FakeOpenAICompatibleClient()
    service = AiSettingsService(store, openai_client=client)

    with session_factory() as session:
        service.save_openai_settings(
            session,
            api_key="sk-private-gpt-key-ABCD",
            base_url="https://gateway.example.test/v1",
            text_model="gpt-5.5",
            text_enabled=True,
            make_active=True,
        )
        output = service.complete_text(session, "extract facts")

    assert output == '{"ok": true}'
    assert client.calls == [
        ("sk-private-gpt-key-ABCD", "https://gateway.example.test/v1", "gpt-5.5", "extract facts")
    ]
