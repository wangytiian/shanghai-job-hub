"""Bailian-only image transcription; retry scheduling belongs to the work queue."""

import base64
from io import BytesIO
import re
from time import monotonic

import httpx
from PIL import Image

from app.services.screenshot_contracts import (
    MAX_BASE64_BYTES, OCR_CONNECT_TIMEOUT, OCR_READ_TIMEOUT, OcrResult, ScreenshotError,
)


OCR_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
OCR_TIMEOUT = httpx.Timeout(45.0, connect=OCR_CONNECT_TIMEOUT, read=OCR_READ_TIMEOUT)
OCR_MAX_BASE64_BYTES = MAX_BASE64_BYTES
# Qwen-VL-OCR's OpenAI-compatible text-recognition task uses this exact prompt.
# https://help.aliyun.com/zh/model-studio/qwen-vl-ocr
OCR_PROMPT = "Please output only the text content from the image without any additional descriptions or formatting."
OCR_ERROR_MESSAGES = {
    "ocr_not_configured": "图片识别尚未配置或启用，请前往 AI 模型配置检查百炼密钥与 OCR 开关。",
    "invalid_image": "图片格式或大小不符合识别要求，请重新上传或拆分图片。",
    "provider_auth": "识别服务鉴权失败，请管理员检查百炼密钥和模型权限后重试。",
    "provider_rate_limit": "识别服务请求过多，将稍后重试；也可稍后手动重试。",
    "provider_timeout": "识别服务超时，将稍后重试；可拆小图片后重试。",
    "provider_unavailable": "暂时无法连接识别服务，将稍后重试；请检查服务状态。",
    "ocr_empty": "图片未返回可读文字，请补充清晰截图或粘贴正文。",
    "ocr_no_text": "识别服务仅返回了坐标，没有可读文字，请检查 OCR 模型配置或补充正文。",
    "ocr_truncated": "识别结果被截断，请拆分图片或补充缺失正文后重试。",
    "invalid_response": "识别服务返回了无法解析的响应，请检查模型配置或稍后手动重试。",
}
TRANSIENT_OCR_ERRORS = {"provider_rate_limit", "provider_timeout", "provider_unavailable"}


class OcrError(ScreenshotError):
    """Public, safe error; never carries HTTP bodies, headers or image data."""

    def __init__(self, code: str):
        safe_code = code if code in OCR_ERROR_MESSAGES else "invalid_response"
        super().__init__(safe_code, OCR_ERROR_MESSAGES[safe_code], retryable=safe_code in TRANSIENT_OCR_ERRORS)


class BailianOcrClient:
    def recognize(self, api_key: str, model: str, image_bytes: bytes, mime_type: str) -> OcrResult:
        if not api_key or not model:
            raise OcrError("ocr_not_configured")
        if not isinstance(image_bytes, bytes) or not image_bytes or mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise OcrError("invalid_image")
        # Check the encoded length before allocating an oversized Base64 string.
        if 4 * ((len(image_bytes) + 2) // 3) > OCR_MAX_BASE64_BYTES:
            raise OcrError("invalid_image")
        # A long screenshot can have an entirely blank middle piece. Only skip
        # exact uniform pixels: even a one-channel, one-pixel change is retained.
        # Upload validation owns decoding failures; never infer blank from them.
        try:
            with Image.open(BytesIO(image_bytes)) as source:
                with source.convert("RGBA") as pixels:
                    if all(low == high for low, high in pixels.getextrema()):
                        return OcrResult("", model, "stop", 0, None)
        except (OSError, ValueError):
            pass
        image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": OCR_PROMPT},
            ]}],
            "max_tokens": 4096,
        }
        started = monotonic()
        try:
            response = httpx.post(OCR_ENDPOINT, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=OCR_TIMEOUT)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise OcrError("provider_timeout") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                code = "provider_auth"
            elif status == 429:
                code = "provider_rate_limit"
            elif status >= 500:
                code = "provider_unavailable"
            elif status in {400, 413, 415, 422}:
                code = "invalid_image"
            else:
                code = "invalid_response"
            raise OcrError(code) from None
        except httpx.RequestError:
            raise OcrError("provider_unavailable") from None
        try:
            data = response.json()
            choice = data["choices"][0]
            reason = choice["finish_reason"]
            if reason == "length":
                raise OcrError("ocr_truncated")
            if reason != "stop":
                raise OcrError("invalid_response")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise OcrError("invalid_response")
            if not content.strip():
                raise OcrError("ocr_empty")
            if content.count(",") >= 3 and re.fullmatch(r"[\s\d,.+\-\[\]()]+", content):
                raise OcrError("ocr_no_text")
            returned_model = data.get("model", model)
            if not isinstance(returned_model, str) or not returned_model.strip():
                raise OcrError("invalid_response")
            raw_usage = data.get("usage")
            if raw_usage is not None and not isinstance(raw_usage, dict):
                raise OcrError("invalid_response")
            usage = None if raw_usage is None else {
                key: value for key, value in raw_usage.items()
                if isinstance(key, str) and type(value) is int and value >= 0
            }
        except OcrError:
            raise
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise OcrError("invalid_response") from None
        return OcrResult(content.strip(), returned_model.strip(), reason, int((monotonic() - started) * 1000), usage)
