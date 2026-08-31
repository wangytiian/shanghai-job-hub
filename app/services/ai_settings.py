from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiProviderSetting

BAILIAN_PROVIDER = "bailian"
DEFAULT_TEXT_MODEL = "qwen3.7-flash"
DEFAULT_OCR_MODEL = "qwen-vl-ocr"
ALLOWED_TEXT_MODELS = {DEFAULT_TEXT_MODEL}
ALLOWED_OCR_MODELS = {DEFAULT_OCR_MODEL}
BAILIAN_CHAT_COMPLETIONS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class CredentialNotConfiguredError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def set_secret(self, value: str) -> None: ...

    def get_secret(self) -> str | None: ...

    def has_secret(self) -> bool: ...


class WindowsCredentialStore:
    """Stores the local key in Windows Credential Manager through keyring."""

    service_name = "lixin-recruiting-ops"
    account_name = "bailian-api-key"

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency setup failure
            raise RuntimeError("Windows credential support is not installed") from exc
        return keyring

    def set_secret(self, value: str) -> None:
        self._keyring().set_password(self.service_name, self.account_name, value)

    def get_secret(self) -> str | None:
        return self._keyring().get_password(self.service_name, self.account_name)

    def has_secret(self) -> bool:
        return bool(self.get_secret())


class BailianClient:
    """Small server-side client used only to verify the configured text model."""

    def test_connection(self, api_key: str, model: str) -> None:
        response = httpx.post(
            BAILIAN_CHAT_COMPLETIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=15.0,
        )
        response.raise_for_status()

    def complete(self, api_key: str, model: str, prompt: str) -> str:
        response = httpx.post(BAILIAN_CHAT_COMPLETIONS_URL, headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "messages": [{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": prompt}], "temperature": 0.1}, timeout=45.0)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def mask_api_key(value: str) -> str:
    compact = value.strip()
    return f"****{compact[-4:]}" if len(compact) >= 4 else "****"


class AiSettingsService:
    def __init__(self, credential_store: CredentialStore, bailian_client=None):
        self.credential_store = credential_store
        self.bailian_client = bailian_client or BailianClient()

    def get_setting(self, session: Session) -> AiProviderSetting:
        setting = session.scalar(
            select(AiProviderSetting).where(AiProviderSetting.provider == BAILIAN_PROVIDER)
        )
        if setting is None:
            setting = AiProviderSetting(
                provider=BAILIAN_PROVIDER,
                text_model=DEFAULT_TEXT_MODEL,
                ocr_model=DEFAULT_OCR_MODEL,
            )
            session.add(setting)
            session.commit()
            session.refresh(setting)
        return setting

    def save_api_key(self, session: Session, value: str) -> AiProviderSetting:
        key = value.strip()
        if not key:
            raise ValueError("API Key cannot be empty")
        self.credential_store.set_secret(key)
        setting = self.get_setting(session)
        setting.key_masked = mask_api_key(key)
        setting.connection_status = "not_tested"
        setting.last_error_summary = ""
        session.commit()
        session.refresh(setting)
        return setting

    def test_connection(self, session: Session) -> AiProviderSetting:
        api_key = self.credential_store.get_secret()
        if not api_key:
            raise CredentialNotConfiguredError("Save an API Key before testing the connection")
        setting = self.get_setting(session)
        try:
            self.bailian_client.test_connection(api_key, setting.text_model)
        except Exception as exc:
            setting.connection_status = "error"
            setting.last_error_summary = self._sanitize_error(str(exc), api_key)
        else:
            setting.connection_status = "ready"
            setting.last_error_summary = ""
        setting.last_tested_at = datetime.now()
        session.commit()
        session.refresh(setting)
        return setting

    @staticmethod
    def _sanitize_error(message: str, api_key: str) -> str:
        cleaned = message.replace(api_key, "[redacted]").strip()
        return cleaned[:300] or "The provider did not return a readable error message."

    def save_models(
        self,
        session: Session,
        text_model: str,
        ocr_model: str,
        text_enabled: bool,
        ocr_enabled: bool,
    ) -> AiProviderSetting:
        if text_model not in ALLOWED_TEXT_MODELS:
            raise ValueError("Unsupported text model")
        if ocr_model not in ALLOWED_OCR_MODELS:
            raise ValueError("Unsupported OCR model")
        setting = self.get_setting(session)
        setting.text_model = text_model
        setting.ocr_model = ocr_model
        setting.text_enabled = text_enabled
        setting.ocr_enabled = ocr_enabled
        setting.updated_at = datetime.now()
        session.commit()
        session.refresh(setting)
        return setting
