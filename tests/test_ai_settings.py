import httpx
import pytest

from app.database import create_database
from app.models import AiProviderSetting
from app.services.ai_settings import (
    AiSettingsService,
    BailianClient,
    CredentialNotConfiguredError,
    OPENAI_API_MODE_RESPONSES,
    OPENAI_COMPATIBLE_PROVIDER,
    OpenAICompatibleClient,
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

    def complete(
        self, api_key: str, base_url: str, model: str, prompt: str, api_mode: str = "chat_completions"
    ) -> str:
        self.calls.append((api_key, base_url, model, prompt, api_mode))
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
        (
            "sk-private-gpt-key-ABCD",
            "https://gateway.example.test/v1",
            "gpt-5.5",
            "Reply with exactly: OK",
            "chat_completions",
        )
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
        ("sk-private-gpt-key-ABCD", "https://gateway.example.test/v1", "gpt-5.5", "extract facts", "chat_completions")
    ]


def test_openai_compatible_provider_persists_responses_api_mode():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    store = FakeCredentialStore()
    service = AiSettingsService(store, openai_client=FakeOpenAICompatibleClient())

    with session_factory() as session:
        saved = service.save_openai_settings(
            session,
            api_key="sk-private-gpt-key-ABCD",
            base_url="https://gateway.example.test",
            text_model="gpt-5.6-terra",
            api_mode=OPENAI_API_MODE_RESPONSES,
            text_enabled=True,
            make_active=True,
        )

    assert saved.api_mode == OPENAI_API_MODE_RESPONSES


def test_responses_api_posts_to_responses_and_reads_output_text(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": '{"employer_name":"上海示例单位"}'}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr("app.services.ai_settings.httpx.post", fake_post)

    output = OpenAICompatibleClient().complete(
        "sk-private-gpt-key-ABCD",
        "https://gateway.example.test",
        "gpt-5.6-terra",
        "提取招聘字段",
        OPENAI_API_MODE_RESPONSES,
    )

    assert captured["url"] == "https://gateway.example.test/responses"
    assert captured["json"]["input"] == "提取招聘字段"
    assert output == '{"employer_name":"上海示例单位"}'


@pytest.mark.parametrize(
    ("client", "args"),
    [
        (BailianClient(), ("sk-test", "qwen3.7-flash", "extract facts")),
        (OpenAICompatibleClient(), ("sk-test", "https://gateway.example.test/v1", "gpt-test", "extract facts")),
    ],
)
def test_text_completion_allows_120_second_read_without_changing_other_timeouts(monkeypatch, client, args):
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.services.ai_settings.httpx.post", fake_post)

    assert client.complete(*args) == "{}"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 120.0
    assert timeout.connect == timeout.write == timeout.pool == 45.0


def test_configuration_revisions_change_on_edits_but_not_connection_test():
    service = AiSettingsService(FakeCredentialStore(), FakeBailianClient())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        setting = service.get_setting(session)
        version = setting.row_version
        service.save_api_key(session, "fake-key")
        assert setting.row_version == version + 1
        version = setting.row_version
        service.test_connection(session)
        assert setting.row_version == version
        service.save_models(session, setting.text_model, setting.ocr_model, False, True)
        assert setting.row_version == version + 1
        version = setting.row_version
        service.save_models(session, setting.text_model, setting.ocr_model, False, True)
        assert setting.row_version == version


def test_active_provider_changes_increment_both_affected_revisions():
    service = AiSettingsService(FakeCredentialStore())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        bailian, openai = service.get_all_settings(session)
        before = (bailian.row_version, openai.row_version)
        service.save_openai_settings(session, api_key="fake-key", base_url="https://example.test", text_model="fake-model", text_enabled=True, make_active=True)
        assert bailian.row_version == before[0] + 1
        assert openai.row_version > before[1]
        before = (bailian.row_version, openai.row_version)
        service.set_active_text_provider(session, "bailian")
        assert (bailian.row_version, openai.row_version) == (before[0] + 1, before[1] + 1)
        service.set_active_text_provider(session, "bailian")
        assert (bailian.row_version, openai.row_version) == (before[0] + 1, before[1] + 1)


def test_ocr_is_separate_from_text_readiness_and_connection_test():
    service = AiSettingsService(FakeCredentialStore(), FakeBailianClient())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        assert not service.is_ocr_provider_ready(session)
        assert service.ocr_provider_status(session)["status"] == "not_configured"
        service.save_api_key(session, "fake-key")
        service.test_connection(session)
        assert service.is_ocr_provider_ready(session)
        assert service.ocr_provider_status(session)["status"] == "not_tested"
        setting = service.get_setting(session)
        service.save_models(session, setting.text_model, setting.ocr_model, True, False)
        assert not service.is_ocr_provider_ready(session)
        assert service.ocr_provider_status(session)["status"] == "disabled"


def test_ocr_uses_bailian_even_when_openai_text_is_active_and_tracks_recent_result():
    from app.services.screenshot_contracts import OcrResult
    from app.services.screenshot_ocr import OcrError

    class FakeOcrClient:
        calls = []
        error = None

        def recognize(self, *args):
            self.calls.append(args)
            if self.error:
                raise self.error
            return OcrResult("原文", "qwen-vl-ocr", "stop", 7, None)

    stores = {"bailian": FakeCredentialStore(), "openai_compatible": FakeCredentialStore()}
    client = FakeOcrClient()
    service = AiSettingsService(credential_store_factory=stores.__getitem__, ocr_client=client)
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        service.save_api_key(session, "bailian-fake-key")
        service.save_openai_settings(session, api_key="openai-fake-key", base_url="https://example.test", text_model="fake-model", text_enabled=True, make_active=True)
        before = service.ocr_config_snapshot(session)
        assert service.recognize_image(session, b"image", "image/png").text == "原文"
        assert client.calls == [("bailian-fake-key", "qwen-vl-ocr", b"image", "image/png")]
        assert service.ocr_provider_status(session)["status"] == "ready"
        assert service.ocr_config_snapshot(session) == before
        client.error = OcrError("provider_auth")
        with pytest.raises(OcrError):
            service.recognize_image(session, b"image", "image/png")
        assert service.ocr_provider_status(session)["status"] == "error"
        assert service.ocr_provider_status(session)["last_error_code"] == "provider_auth"
        service.save_api_key(session, "changed-fake-key")
        assert service.ocr_provider_status(session)["status"] == "not_tested"


def test_config_snapshots_are_non_sensitive_and_detect_key_and_model_changes():
    service = AiSettingsService(FakeCredentialStore())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        initial = service.ocr_config_snapshot(session)
        initial_text = service.text_config_snapshot(session)
        service.save_api_key(session, "fake-secret-ABCD")
        assert service.ocr_config_snapshot(session) != initial
        assert service.text_config_snapshot(session) != initial_text
        snapshot = service.ocr_config_snapshot(session)
        assert snapshot["provider"] == "bailian"
        assert snapshot["model"] == "qwen-vl-ocr"
        assert "fake-secret" not in repr(snapshot)
        assert "ABCD" not in repr(snapshot)
        service.save_openai_settings(session, api_key="fake-gpt-key", base_url="https://example.test", text_model="fake-model", text_enabled=True, make_active=True)
        assert service.text_config_snapshot(session)["provider"] == "openai_compatible"


def test_runtime_factory_reuses_file_or_windows_credentials_without_reading_them(monkeypatch):
    from app.config import Settings
    from app.services.ai_runtime import create_ai_settings_service

    monkeypatch.setattr("app.services.credentials.FileCredentialStore.get_secret", lambda self: pytest.fail("factory read secret"))
    monkeypatch.setattr("app.services.ai_settings.WindowsCredentialStore.get_secret", lambda self: pytest.fail("factory read secret"))
    file_service = create_ai_settings_service(Settings(app_env="test", database_url="sqlite://", secret_backend="file"))
    assert file_service.credential_store_factory is not None
    assert file_service.openai_client.validate_outbound is not None
    windows_service = create_ai_settings_service(Settings(app_env="local", database_url="sqlite://"))
    assert windows_service.credential_store_factory is None


def test_frozen_ocr_call_survives_session_close_and_does_not_follow_later_configuration():
    from app.services.screenshot_contracts import OcrResult

    calls = []

    class Client:
        def recognize(self, *args):
            calls.append(args)
            return OcrResult("原文", "qwen-vl-ocr", "stop", 1, None)

    service = AiSettingsService(FakeCredentialStore(), ocr_client=Client())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        service.save_api_key(session, "old-fake-key")
        frozen = service.freeze_ocr_request(session)
        service.save_api_key(session, "new-fake-key")
    assert frozen(b"image", "image/png").text == "原文"
    assert calls == [("old-fake-key", "qwen-vl-ocr", b"image", "image/png")]


def test_frozen_text_call_uses_provider_and_model_selected_before_session_closes():
    client = FakeOpenAICompatibleClient()
    service = AiSettingsService(FakeCredentialStore(), openai_client=client)
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        service.save_openai_settings(session, api_key="fake-key", base_url="https://example.test", text_model="old-model", text_enabled=True, make_active=True)
        frozen = service.freeze_text_request(session)
        service.get_active_text_setting(session).text_model = "new-model"
        session.commit()
    assert frozen("prompt") == '{"ok": true}'
    assert client.calls == [("fake-key", "https://example.test", "old-model", "prompt", "chat_completions")]


@pytest.mark.parametrize("snapshot_method", ["ocr_config_snapshot", "text_config_snapshot"])
def test_snapshots_do_not_commit_callers_transaction_when_defaults_are_missing(snapshot_method):
    service = AiSettingsService(FakeCredentialStore())
    with create_database("sqlite+pysqlite:///:memory:")() as session:
        getattr(service, snapshot_method)(session)
        session.rollback()
        assert session.query(AiProviderSetting).count() == 0


def test_concurrent_stale_settings_sessions_still_increment_revision_monotonically():
    factory = create_database("sqlite+pysqlite:///:memory:")
    service = AiSettingsService(FakeCredentialStore())
    with factory() as session:
        service.get_setting(session)
    with factory() as first, factory() as second:
        one = service.get_setting(first)
        two = service.get_setting(second)
        initial = one.row_version
        assert two.row_version == initial
        service.save_models(first, one.text_model, one.ocr_model, False, True)
        service.save_models(second, two.text_model, two.ocr_model, True, False)
    with factory() as session:
        assert service.get_setting(session).row_version == initial + 2
