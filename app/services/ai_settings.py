from copy import deepcopy
from datetime import datetime
import re
from threading import Lock
from typing import Callable, Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiProviderSetting
from app.services.screenshot_contracts import OcrResult
from app.services.screenshot_ocr import BailianOcrClient, OcrError

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
TEXT_COMPLETION_TIMEOUT = httpx.Timeout(45.0, read=120.0)
SCREENSHOT_MAX_OUTPUT_TOKENS = 8192


def _screenshot_fallback_payload(response: httpx.Response, payload: dict) -> dict | None:
    """Only explicitly unsupported optional arguments permit one compatibility retry."""
    if response.status_code not in {400, 422}:
        return None
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            return None
        message = str(error.get("message", "")).lower()
        code = str(error.get("code", "")).lower()
        param = str(error.get("param") or "").lower()
    except (ValueError, AttributeError):
        return None
    unsupported = any(marker in f"{code} {message}" for marker in (
        "unsupported", "not supported", "does not support", "unknown parameter",
        "unrecognized parameter", "unexpected keyword", "not permitted",
    ))
    if not unsupported:
        return None

    def names(*aliases: str) -> bool:
        # Error param is authoritative when supplied; prose is a fallback for
        # compatible gateways which omit that field. Match whole parameter names.
        source = param or message
        return any(re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", source) for alias in aliases)

    fallback = deepcopy(payload)
    if names("strict"):
        container = fallback.get("response_format", {}).get("json_schema") or fallback.get("text", {}).get("format", {})
        container.pop("strict", None)
    elif names("response_format", "json_schema", "schema", "text.format"):
        fallback.pop("response_format", None)
        fallback.pop("text", None)
    elif names("reasoning", "reasoning_effort", "reasoning.effort"):
        fallback.pop("reasoning", None)
        fallback.pop("reasoning_effort", None)
    elif names("temperature"):
        fallback.pop("temperature", None)
    elif names("max_completion_tokens"):
        fallback.pop("max_completion_tokens", None)
    return fallback if fallback != payload else None


def _post_screenshot_text(endpoint: str, api_key: str, payload: dict) -> dict:
    """Keep bounded retries and sanitized transport errors local to screenshots."""
    for attempt in range(2):
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=TEXT_COMPLETION_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise type(exc)("截图字段提取服务连接失败") from None
        if response.is_error:
            fallback = _screenshot_fallback_payload(response, payload) if attempt == 0 else None
            if fallback is not None:
                payload = fallback
                continue
            # Retain status-based classification without retaining raw provider
            # bodies, configured URLs or reflected credentials in the exception.
            safe_request = httpx.Request("POST", "https://text-provider.invalid")
            safe_response = httpx.Response(response.status_code, request=safe_request)
            raise httpx.HTTPStatusError(
                f"截图字段提取服务请求失败（HTTP {response.status_code}）",
                request=safe_request,
                response=safe_response,
            ) from None
        try:
            return response.json()
        except ValueError:
            raise ValueError("截图字段提取服务未返回可解析的响应") from None
    raise RuntimeError("截图字段提取服务请求失败")  # pragma: no cover


def _screenshot_chat_payload(model: str, prompt: str, schema: dict) -> dict:
    return {
        "model": model,
        "messages": [{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": deepcopy(schema)},
        "max_tokens": SCREENSHOT_MAX_OUTPUT_TOKENS,
    }


def _screenshot_chat_text(payload: dict) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("截图字段提取服务未返回可解析的文本结果") from None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("截图字段提取服务未返回可解析的文本结果")
    return content.strip()


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
            timeout=TEXT_COMPLETION_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def complete_screenshot(self, api_key: str, model: str, prompt: str, schema: dict) -> str:
        payload = _screenshot_chat_payload(model, prompt, schema)
        return _screenshot_chat_text(_post_screenshot_text(BAILIAN_CHAT_COMPLETIONS_URL, api_key, payload))


class OpenAICompatibleClient:
    def __init__(self, validate_outbound: Callable[[str], str] | None = None):
        self.validate_outbound = validate_outbound

    @staticmethod
    def _endpoint_url(base_url: str, api_mode: str) -> str:
        cleaned = base_url.strip().rstrip("/")
        suffix = "/responses" if api_mode == OPENAI_API_MODE_RESPONSES else "/chat/completions"
        return cleaned if cleaned.endswith(suffix) else f"{cleaned}{suffix}"

    def _safe_endpoint_url(self, base_url: str, api_mode: str) -> str:
        endpoint = self._endpoint_url(base_url, api_mode)
        return self.validate_outbound(endpoint) if self.validate_outbound else endpoint

    def test_connection(self, api_key: str, base_url: str, model: str) -> None:
        response = httpx.post(
            self._safe_endpoint_url(base_url, OPENAI_API_MODE_CHAT_COMPLETIONS),
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
            self._safe_endpoint_url(base_url, api_mode),
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=TEXT_COMPLETION_TIMEOUT,
        )
        response.raise_for_status()
        response_payload = response.json()
        if api_mode == OPENAI_API_MODE_RESPONSES:
            return self._responses_text(response_payload)
        content = response_payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Chat Completions 未返回可解析的文本结果")
        return content.strip()

    def complete_screenshot(
        self,
        api_key: str,
        base_url: str,
        model: str,
        prompt: str,
        schema: dict,
        api_mode: str = OPENAI_API_MODE_CHAT_COMPLETIONS,
    ) -> str:
        if api_mode not in ALLOWED_OPENAI_API_MODES:
            raise ValueError("不支持的 GPT 接口模式")
        if api_mode == OPENAI_API_MODE_RESPONSES:
            payload = {
                "model": model,
                "instructions": "Return only valid JSON.",
                "input": prompt,
                "text": {"format": {"type": "json_schema", **deepcopy(schema)}},
                "max_output_tokens": SCREENSHOT_MAX_OUTPUT_TOKENS,
            }
            if model == "gpt-5.6-terra":
                payload["reasoning"] = {"effort": "none"}
        else:
            payload = _screenshot_chat_payload(model, prompt, schema)
            if model == "gpt-5.6-terra":
                payload["reasoning_effort"] = "none"
                payload["max_completion_tokens"] = payload.pop("max_tokens")
        response_payload = _post_screenshot_text(self._safe_endpoint_url(base_url, api_mode), api_key, payload)
        if api_mode == OPENAI_API_MODE_RESPONSES:
            return self._responses_text(response_payload)
        return _screenshot_chat_text(response_payload)


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
    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        bailian_client=None,
        openai_client=None,
        credential_store_factory: Callable[[str], CredentialStore] | None = None,
        ocr_client=None,
        credential_revision: str = "",
    ):
        self.credential_store = credential_store or WindowsCredentialStore(BAILIAN_PROVIDER)
        self.credential_store_factory = credential_store_factory
        self.bailian_client = bailian_client or BailianClient()
        self.openai_client = openai_client or OpenAICompatibleClient()
        self.ocr_client = ocr_client or BailianOcrClient()
        self.credential_revision = credential_revision
        self._ocr_status_lock = Lock()
        self._ocr_recent_status: dict[tuple, tuple[str, str]] = {}

    def _credential_store_for(self, provider: str) -> CredentialStore:
        if self.credential_store_factory is not None:
            return self.credential_store_factory(provider)
        return WindowsCredentialStore(provider) if isinstance(self.credential_store, WindowsCredentialStore) else self.credential_store

    def get_setting(self, session: Session, provider: str = BAILIAN_PROVIDER, *, commit: bool = True) -> AiProviderSetting:
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
            if commit:
                session.commit()
                session.refresh(setting)
            else:
                session.flush()
        return setting

    def get_all_settings(self, session: Session, *, commit: bool = True) -> tuple[AiProviderSetting, AiProviderSetting]:
        bailian = self.get_setting(session, BAILIAN_PROVIDER, commit=commit)
        openai = self.get_setting(session, OPENAI_COMPATIBLE_PROVIDER, commit=commit)
        if not session.scalar(select(AiProviderSetting).where(AiProviderSetting.is_active_text_provider.is_(True))):
            bailian.is_active_text_provider = True
            self._bump_revision(bailian)
            if commit:
                session.commit()
                session.refresh(bailian)
            else:
                session.flush()
        return bailian, openai

    def get_active_text_setting(self, session: Session, *, commit: bool = True) -> AiProviderSetting:
        bailian, openai = self.get_all_settings(session, commit=commit)
        return openai if openai.is_active_text_provider else bailian

    def is_active_text_provider_ready(self, session: Session) -> bool:
        setting = self.get_active_text_setting(session)
        if not setting.text_enabled or not self._credential_store_for(setting.provider).has_secret():
            return False
        return setting.provider == BAILIAN_PROVIDER or bool(setting.base_url and setting.text_model)

    def _activate(self, session: Session, provider: str) -> None:
        self.get_setting(session, provider)
        for setting in session.scalars(select(AiProviderSetting)):
            active = setting.provider == provider
            if setting.is_active_text_provider != active:
                setting.is_active_text_provider = active
                self._bump_revision(setting)

    @staticmethod
    def _bump_revision(setting: AiProviderSetting) -> None:
        # The increment is executed by the database; stale settings forms must
        # not reuse another writer's revision and invalidate a frozen-call check.
        setting.row_version = AiProviderSetting.row_version + 1
        setting.updated_at = datetime.now()

    def ocr_config_snapshot(self, session: Session) -> dict:
        setting = self.get_setting(session, BAILIAN_PROVIDER, commit=False)
        return {
            "provider": BAILIAN_PROVIDER,
            "model": setting.ocr_model,
            "base_url": BAILIAN_CHAT_COMPLETIONS_URL,
            "api_mode": OPENAI_API_MODE_CHAT_COMPLETIONS,
            "enabled": setting.ocr_enabled,
            "row_version": setting.row_version,
            "credential_revision": self.credential_revision,
        }

    def text_config_snapshot(self, session: Session) -> dict:
        setting = self.get_active_text_setting(session, commit=False)
        return {
            "provider": setting.provider,
            "model": setting.text_model,
            "base_url": BAILIAN_CHAT_COMPLETIONS_URL if setting.provider == BAILIAN_PROVIDER else setting.base_url,
            "api_mode": OPENAI_API_MODE_CHAT_COMPLETIONS if setting.provider == BAILIAN_PROVIDER else setting.api_mode,
            "enabled": setting.text_enabled,
            "active": setting.is_active_text_provider,
            "row_version": setting.row_version,
            "credential_revision": self.credential_revision,
        }

    def is_ocr_provider_ready(self, session: Session) -> bool:
        setting = self.get_setting(session, BAILIAN_PROVIDER, commit=False)
        return bool(setting.ocr_enabled and setting.ocr_model and self._credential_store_for(BAILIAN_PROVIDER).has_secret())

    def ocr_provider_status(self, session: Session) -> dict:
        """Recent OCR requests are separate from the persistent text ping status.

        On restart, configured OCR is accurately shown as not_tested until an
        actual image request succeeds. No OCR result changes configuration.
        """
        snapshot = self.ocr_config_snapshot(session)
        if not snapshot["enabled"]:
            status, code = "disabled", "ocr_not_configured"
        elif not snapshot["model"] or not self._credential_store_for(BAILIAN_PROVIDER).has_secret():
            status, code = "not_configured", "ocr_not_configured"
        else:
            with self._ocr_status_lock:
                status, code = self._ocr_recent_status.get(tuple(sorted(snapshot.items())), ("not_tested", ""))
        messages = {"not_tested": "OCR 已配置，尚未完成图片识别验证。", "ready": "最近一次图片识别成功。", "disabled": "图片识别已关闭，请在 AI 模型配置中启用。", "not_configured": "请先配置百炼图片识别密钥和模型。"}
        return {"status": status, "message": str(OcrError(code)) if status == "error" else messages[status], "last_error_code": code}

    def recognize_image(self, session: Session, image_bytes: bytes, mime_type: str) -> OcrResult:
        return self.freeze_ocr_request(session)(image_bytes, mime_type)

    def freeze_ocr_request(self, session: Session) -> Callable[[bytes, str], OcrResult]:
        """Resolve credentials/config now; the callable keeps no session open."""
        setting = self.get_setting(session, BAILIAN_PROVIDER, commit=False)
        if not setting.ocr_enabled or not setting.ocr_model:
            raise OcrError("ocr_not_configured")
        api_key = self._credential_store_for(BAILIAN_PROVIDER).get_secret()
        if not api_key:
            raise OcrError("ocr_not_configured")
        fingerprint = tuple(sorted(self.ocr_config_snapshot(session).items()))
        model = setting.ocr_model

        def call(image_bytes: bytes, mime_type: str) -> OcrResult:
            try:
                result = self.ocr_client.recognize(api_key, model, image_bytes, mime_type)
            except OcrError as exc:
                with self._ocr_status_lock:
                    self._ocr_recent_status[fingerprint] = ("error", exc.code)
                raise
            with self._ocr_status_lock:
                self._ocr_recent_status[fingerprint] = ("ready", "")
            return result
        return call

    def save_api_key(self, session: Session, value: str, provider: str = BAILIAN_PROVIDER) -> AiProviderSetting:
        key = value.strip()
        if not key:
            raise ValueError("API Key cannot be empty")
        self._credential_store_for(provider).set_secret(key)
        setting = self.get_setting(session, provider)
        setting.key_masked = mask_api_key(key)
        setting.connection_status = "not_tested"
        setting.last_error_summary = ""
        self._bump_revision(setting)
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
        normalized_url = _normalize_base_url(base_url)
        setting = self.save_api_key(session, api_key, OPENAI_COMPATIBLE_PROVIDER)
        setting.base_url = normalized_url
        setting.text_model = model
        setting.api_mode = api_mode
        setting.text_enabled = text_enabled
        setting.ocr_enabled = False
        self._bump_revision(setting)
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
        return self.freeze_text_request(session)(prompt)

    def freeze_text_request(self, session: Session) -> Callable[[str], str]:
        setting = self.get_active_text_setting(session, commit=False)
        if not setting.text_enabled:
            raise TextProviderNotReadyError("当前文本模型未启用，请前往 AI 模型配置启用后再试")
        api_key = self._credential_store_for(setting.provider).get_secret()
        if not api_key:
            raise CredentialNotConfiguredError("当前文本模型尚未保存 API Key")
        model = setting.text_model
        if setting.provider == BAILIAN_PROVIDER:
            return lambda prompt: self.bailian_client.complete(api_key, model, prompt)
        if not setting.base_url or not setting.text_model:
            raise TextProviderNotReadyError("当前 GPT 配置缺少 API Base URL 或模型名")
        base_url, api_mode = setting.base_url, setting.api_mode
        return lambda prompt: self.openai_client.complete(api_key, base_url, model, prompt, api_mode)

    def freeze_screenshot_text_request(self, session: Session, schema: dict) -> Callable[[str], str]:
        """Freeze screenshot schema/config/credentials without retaining a session."""
        setting = self.get_active_text_setting(session, commit=False)
        if not setting.text_enabled:
            raise TextProviderNotReadyError("当前文本模型未启用，请前往 AI 模型配置启用后再试")
        api_key = self._credential_store_for(setting.provider).get_secret()
        if not api_key:
            raise CredentialNotConfiguredError("当前文本模型尚未保存 API Key")
        model, frozen_schema = setting.text_model, deepcopy(schema)
        if setting.provider == BAILIAN_PROVIDER:
            return lambda prompt: self.bailian_client.complete_screenshot(api_key, model, prompt, frozen_schema)
        if not setting.base_url or not setting.text_model:
            raise TextProviderNotReadyError("当前 GPT 配置缺少 API Base URL 或模型名")
        base_url, api_mode = setting.base_url, setting.api_mode
        return lambda prompt: self.openai_client.complete_screenshot(api_key, base_url, model, prompt, frozen_schema, api_mode)

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
        changed = (setting.text_model, setting.ocr_model, setting.text_enabled, setting.ocr_enabled) != (text_model, ocr_model, text_enabled, ocr_enabled)
        setting.text_model, setting.ocr_model = text_model, ocr_model
        setting.text_enabled, setting.ocr_enabled = text_enabled, ocr_enabled
        if changed:
            self._bump_revision(setting)
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
