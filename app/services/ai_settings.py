from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AiProviderSetting

BAILIAN_PROVIDER = "bailian"
OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"
DEFAULT_TEXT_MODEL = "qwen3.7-flash"
DEFAULT_OCR_MODEL = "qwen-vl-ocr"
DEFAULT_OPENAI_MODEL = ""
OPENAI_API_MODE_CHAT_COMPLETIONS = "chat_completions"
OPENAI_API_MODE_RESPONSES = "responses"
ALLOWED_OPENAI_API_MODES = {OPENAI_API_MODE_CHAT_COMPLETIONS, OPENAI_API_MODE_RESPONSES}
ALLOWED_TEXT_MODELS = {DEFAULT_TEXT_MODEL}
ALLOWED_OCR_MODELS = {DEFAULT_OCR_MODEL}
BAILIAN_CHAT_COMPLETIONS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class CredentialNotConfiguredError(RuntimeError):
    pass


class TextProviderNotReadyError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def set_secret(self, value: str) -> None: ...
    def get_secret(self) -> str | None: ...
    def has_secret(self) -> bool: ...


class WindowsCredentialStore:
    """Stores each local provider key in Windows Credential Manager through keyring."""

    service_name = "lixin-recruiting-ops"
    account_names = {
        BAILIAN_PROVIDER: "bailian-api-key",
        OPENAI_COMPATIBLE_PROVIDER: "openai-compatible-api-key",
    }

    def __init__(self, provider: str = BAILIAN_PROVIDER):
        self.provider = provider
        self.account_name = self.account_names[provider]

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Windows credential support is not installed") from exc
        return keyring

    def set_secret(self, value: str) -> None:
        self._keyring().set_password(self.service_name, self.account_name, value)

    def get_secret(self) -> str | None:
        return self._keyring().get_password(self.service_name, self.account_name)

    def has_secret(self) -> bool:
        return bool(self.get_secret())


class BailianClient:
    def test_connection(self, api_key: str, model: str) -> None:
        response = httpx.post(
            BAILIAN_CHAT_COMPLETIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=15.0,
        )
        response.raise_for_status()

    def complete(self, api_key: str, model: str, prompt: str) -> str:
        response = httpx.post(
            BAILIAN_CHAT_COMPLETIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": prompt}], "temperature": 0.1},
            timeout=45.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


class OpenAICompatibleClient:
    @staticmethod
    def _endpoint_url(base_url: str, api_mode: str) -> str:
        cleaned = base_url.strip().rstrip("/")
        suffix = "/responses" if api_mode == OPENAI_API_MODE_RESPONSES else "/chat/completions"
        return cleaned if cleaned.endswith(suffix) else f"{cleaned}{suffix}"

    def test_connection(self, api_key: str, base_url: str, model: str) -> None:
        response = httpx.post(
            self._endpoint_url(base_url, OPENAI_API_MODE_CHAT_COMPLETIONS),
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=15.0,
        )
        response.raise_for_status()

    @staticmethod
    def _responses_text(payload: dict) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for output in payload.get("output", []):
            for content in output.get("content", []):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        raise ValueError("Responses API 未返回可解析的文本结果")

    def complete(
        self,
        api_key: str,
        base_url: str,
        model: str,
        prompt: str,
        api_mode: str = OPENAI_API_MODE_CHAT_COMPLETIONS,
    ) -> str:
        if api_mode not in ALLOWED_OPENAI_API_MODES:
            raise ValueError("不支持的 GPT 接口模式")
        if api_mode == OPENAI_API_MODE_RESPONSES:
            payload = {
                "model": model,
                "instructions": "Return only valid JSON.",
                "input": prompt,
            }
        else:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            }
        response = httpx.post(
            self._endpoint_url(base_url, api_mode),
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=45.0,
        )
        response.raise_for_status()
        response_payload = response.json()
        if api_mode == OPENAI_API_MODE_RESPONSES:
            return self._responses_text(response_payload)
        content = response_payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Chat Completions 未返回可解析的文本结果")
        return content.strip()


def mask_api_key(value: str) -> str:
    compact = value.strip()
    return f"****{compact[-4:]}" if len(compact) >= 4 else "****"


def _normalize_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API Base URL 必须是完整的 http 或 https 地址")
    return cleaned


class AiSettingsService:
    def __init__(self, credential_store: CredentialStore | None = None, bailian_client=None, openai_client=None):
        self.credential_store = credential_store or WindowsCredentialStore(BAILIAN_PROVIDER)
        self.bailian_client = bailian_client or BailianClient()
        self.openai_client = openai_client or OpenAICompatibleClient()

    def _credential_store_for(self, provider: str) -> CredentialStore:
        return WindowsCredentialStore(provider) if isinstance(self.credential_store, WindowsCredentialStore) else self.credential_store

    def get_setting(self, session: Session, provider: str = BAILIAN_PROVIDER) -> AiProviderSetting:
        setting = session.scalar(select(AiProviderSetting).where(AiProviderSetting.provider == provider))
        if setting is None:
            setting = AiProviderSetting(
                provider=provider,
                base_url="",
                api_mode=OPENAI_API_MODE_CHAT_COMPLETIONS,
                text_model=DEFAULT_TEXT_MODEL if provider == BAILIAN_PROVIDER else DEFAULT_OPENAI_MODEL,
                ocr_model=DEFAULT_OCR_MODEL,
                text_enabled=provider == BAILIAN_PROVIDER,
                ocr_enabled=provider == BAILIAN_PROVIDER,
                is_active_text_provider=provider == BAILIAN_PROVIDER,
            )
            session.add(setting)
            session.commit()
            session.refresh(setting)
        return setting

    def get_all_settings(self, session: Session) -> tuple[AiProviderSetting, AiProviderSetting]:
        bailian = self.get_setting(session, BAILIAN_PROVIDER)
        openai = self.get_setting(session, OPENAI_COMPATIBLE_PROVIDER)
        if not session.scalar(select(AiProviderSetting).where(AiProviderSetting.is_active_text_provider.is_(True))):
            bailian.is_active_text_provider = True
            session.commit()
            session.refresh(bailian)
        return bailian, openai

    def get_active_text_setting(self, session: Session) -> AiProviderSetting:
        bailian, openai = self.get_all_settings(session)
        return openai if openai.is_active_text_provider else bailian

    def is_active_text_provider_ready(self, session: Session) -> bool:
        setting = self.get_active_text_setting(session)
        if not setting.text_enabled or not self._credential_store_for(setting.provider).has_secret():
            return False
        return setting.provider == BAILIAN_PROVIDER or bool(setting.base_url and setting.text_model)

    def _activate(self, session: Session, provider: str) -> None:
        session.execute(update(AiProviderSetting).values(is_active_text_provider=False))
        self.get_setting(session, provider).is_active_text_provider = True

    def save_api_key(self, session: Session, value: str, provider: str = BAILIAN_PROVIDER) -> AiProviderSetting:
        key = value.strip()
        if not key:
            raise ValueError("API Key cannot be empty")
        self._credential_store_for(provider).set_secret(key)
        setting = self.get_setting(session, provider)
        setting.key_masked = mask_api_key(key)
        setting.connection_status = "not_tested"
        setting.last_error_summary = ""
        session.commit()
        session.refresh(setting)
        return setting

    def save_openai_settings(
        self,
        session: Session,
        *,
        api_key: str,
        base_url: str,
        text_model: str,
        api_mode: str = OPENAI_API_MODE_CHAT_COMPLETIONS,
        text_enabled: bool,
        make_active: bool,
    ) -> AiProviderSetting:
        model = text_model.strip()
        if not model:
            raise ValueError("GPT 模型名不能为空")
        if api_mode not in ALLOWED_OPENAI_API_MODES:
            raise ValueError("不支持的 GPT 接口模式")
        setting = self.save_api_key(session, api_key, OPENAI_COMPATIBLE_PROVIDER)
        setting.base_url = _normalize_base_url(base_url)
        setting.text_model = model
        setting.api_mode = api_mode
        setting.text_enabled = text_enabled
        setting.ocr_enabled = False
        if make_active:
            self._activate(session, OPENAI_COMPATIBLE_PROVIDER)
        setting.updated_at = datetime.now()
        session.commit()
        session.refresh(setting)
        return setting

    def test_connection(self, session: Session, provider: str = BAILIAN_PROVIDER) -> AiProviderSetting:
        setting = self.get_setting(session, provider)
        api_key = self._credential_store_for(provider).get_secret()
        if not api_key:
            raise CredentialNotConfiguredError("请先保存该提供方的 API Key，再测试连接")
        try:
            if provider == BAILIAN_PROVIDER:
                self.bailian_client.test_connection(api_key, setting.text_model)
            else:
                if not setting.base_url or not setting.text_model:
                    raise TextProviderNotReadyError("请先填写 GPT API Base URL 和模型名")
                test_result = self.openai_client.complete(
                    api_key,
                    setting.base_url,
                    setting.text_model,
                    "Reply with exactly: OK",
                    setting.api_mode,
                )
                if not test_result.strip():
                    raise ValueError("模型未返回可识别的测试结果")
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

    def complete_text(self, session: Session, prompt: str) -> str:
        setting = self.get_active_text_setting(session)
        if not setting.text_enabled:
            raise TextProviderNotReadyError("当前文本模型未启用，请前往 AI 模型配置启用后再试")
        api_key = self._credential_store_for(setting.provider).get_secret()
        if not api_key:
            raise CredentialNotConfiguredError("当前文本模型尚未保存 API Key")
        if setting.provider == BAILIAN_PROVIDER:
            return self.bailian_client.complete(api_key, setting.text_model, prompt)
        if not setting.base_url or not setting.text_model:
            raise TextProviderNotReadyError("当前 GPT 配置缺少 API Base URL 或模型名")
        return self.openai_client.complete(
            api_key, setting.base_url, setting.text_model, prompt, setting.api_mode
        )

    @staticmethod
    def _sanitize_error(message: str, api_key: str) -> str:
        cleaned = message.replace(api_key, "[redacted]").strip()
        return cleaned[:300] or "服务未返回可识别的错误信息。"

    def save_models(self, session: Session, text_model: str, ocr_model: str, text_enabled: bool, ocr_enabled: bool) -> AiProviderSetting:
        if text_model not in ALLOWED_TEXT_MODELS:
            raise ValueError("Unsupported text model")
        if ocr_model not in ALLOWED_OCR_MODELS:
            raise ValueError("Unsupported OCR model")
        setting = self.get_setting(session, BAILIAN_PROVIDER)
        setting.text_model, setting.ocr_model = text_model, ocr_model
        setting.text_enabled, setting.ocr_enabled = text_enabled, ocr_enabled
        setting.updated_at = datetime.now()
        session.commit()
        session.refresh(setting)
        return setting

    def set_active_text_provider(self, session: Session, provider: str) -> AiProviderSetting:
        if provider not in {BAILIAN_PROVIDER, OPENAI_COMPATIBLE_PROVIDER}:
            raise ValueError("不支持的文本模型提供方")
        setting = self.get_setting(session, provider)
        if not setting.text_enabled:
            raise TextProviderNotReadyError("该文本模型尚未启用")
        if not self._credential_store_for(provider).has_secret():
            raise CredentialNotConfiguredError("请先保存该文本模型的 API Key")
        if provider == OPENAI_COMPATIBLE_PROVIDER and (not setting.base_url or not setting.text_model):
            raise TextProviderNotReadyError("请先填写 GPT API Base URL 和模型名")
        self._activate(session, provider)
        session.commit()
        session.refresh(setting)
        return setting
