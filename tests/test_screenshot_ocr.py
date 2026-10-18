import base64
import importlib
from io import BytesIO

import httpx
import pytest
from PIL import Image


IMAGE = b"\x89PNG\r\n\x1a\nsynthetic-test-image"


def ocr_module():
    return importlib.import_module("app.services.screenshot_ocr")


def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://example.test"))


def success_payload(**overrides):
    return {"model": "qwen-vl-ocr", "choices": [{"finish_reason": "stop", "message": {"content": "财务实习生\n本科；投递 hr@example.test"}}], "usage": {"input_tokens": 12, "output_tokens": 20}, **overrides}


def test_ocr_posts_image_and_literal_transcription_prompt_to_bailian(monkeypatch):
    captured = []

    def post(url, **kwargs):
        captured.append((url, kwargs))
        return response(success_payload())

    monkeypatch.setattr(httpx, "post", post)
    result = ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    url, request = captured[0]
    assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer fake-key"}
    assert request["json"]["model"] == "qwen-vl-ocr"
    assert request["json"]["max_tokens"] == 4096
    messages = request["json"]["messages"]
    assert [message["role"] for message in messages] == ["user"]
    assert messages[0]["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(IMAGE).decode("ascii")}}
    assert messages[0]["content"][1]["text"] == "Please output only the text content from the image without any additional descriptions or formatting."
    assert request["timeout"].connect == 10.0
    assert request["timeout"].read == 120.0
    assert result.text == "财务实习生\n本科；投递 hr@example.test"
    assert result.model == "qwen-vl-ocr"
    assert result.finish_reason == "stop"
    assert result.elapsed_ms >= 0
    assert result.usage == {"input_tokens": 12, "output_tokens": 20}


@pytest.mark.parametrize(("status", "code", "retryable"), [(400, "invalid_image", False), (401, "provider_auth", False), (403, "provider_auth", False), (429, "provider_rate_limit", True), (500, "provider_unavailable", True), (503, "provider_unavailable", True), (404, "invalid_response", False)])
def test_provider_http_errors_are_safe_classified_and_never_retried_in_adapter(monkeypatch, status, code, retryable):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return response({"error": {"message": "fake-key raw-provider-details"}}, status)

    monkeypatch.setattr(httpx, "post", post)
    module = ocr_module()
    with pytest.raises(module.OcrError) as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    assert failure.value.code == code
    assert failure.value.retryable is retryable
    assert "fake-key" not in str(failure.value)
    assert "raw-provider-details" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize(("error", "code"), [(httpx.ReadTimeout("fake-key"), "provider_timeout"), (httpx.ConnectTimeout("fake-key"), "provider_timeout"), (httpx.ConnectError("fake-key"), "provider_unavailable")])
def test_transient_network_failure_is_classified_without_leaking_details(monkeypatch, error, code):
    def post(*args, **kwargs):
        raise error

    monkeypatch.setattr(httpx, "post", post)
    module = ocr_module()
    with pytest.raises(module.OcrError) as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    assert failure.value.code == code
    assert failure.value.retryable is True
    assert "fake-key" not in str(failure.value)


@pytest.mark.parametrize(("payload", "code"), [({}, "invalid_response"), ({"choices": []}, "invalid_response"), (success_payload(choices=[{"finish_reason": "length", "message": {"content": "部分文字"}}]), "ocr_truncated"), (success_payload(choices=[{"finish_reason": "stop", "message": {"content": "  "}}]), "ocr_empty"), (success_payload(choices=[{"message": {"content": "文字"}}]), "invalid_response"), (success_payload(choices=[{"finish_reason": "content_filter", "message": {"content": "文字"}}]), "invalid_response"), (success_payload(choices=[{"finish_reason": "stop", "message": {"content": [{"text": "文字"}]}}]), "invalid_response")])
def test_incomplete_or_unknown_response_cannot_be_a_success(monkeypatch, payload, code):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(payload))
    module = ocr_module()
    with pytest.raises(module.OcrError) as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    assert failure.value.code == code
    assert failure.value.retryable is False


def test_non_json_provider_response_is_invalid_response(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, text="private invalid content", request=httpx.Request("POST", "https://example.test")))
    module = ocr_module()
    with pytest.raises(module.OcrError, match="响应") as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    assert failure.value.code == "invalid_response"


@pytest.mark.parametrize(("image", "mime"), [(b"", "image/png"), (IMAGE, "image/svg+xml"), (b"x" * 6_000_001, "image/png")], ids=["empty", "unsupported-mime", "over-base64-budget"])
def test_invalid_image_payload_never_calls_provider(monkeypatch, image, mime):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: pytest.fail("invalid image reached provider"))
    module = ocr_module()
    with pytest.raises(module.OcrError) as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", image, mime)
    assert failure.value.code == "invalid_image"


def test_absent_usage_stays_none(monkeypatch):
    payload = success_payload()
    payload.pop("usage")
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(payload))
    assert ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png").usage is None


def test_ocr_error_uses_shared_screenshot_error_contract():
    from app.services.screenshot_contracts import ScreenshotError

    failure = ocr_module().OcrError("provider_auth")
    assert isinstance(failure, ScreenshotError)
    assert failure.details == {}


@pytest.mark.parametrize("content", ["328,58,43,607,90\n200,128,33,349,90", "[[328,58,43,607,90],[200,128,33,349,90]]", "(328, 58, 43, 607, 90)"])
def test_coordinate_only_response_is_not_successful_transcription(monkeypatch, content):
    payload = success_payload(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(payload))
    module = ocr_module()
    with pytest.raises(module.OcrError) as failure:
        module.BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png")
    assert failure.value.code == "ocr_no_text"
    assert failure.value.retryable is False


def test_coordinates_with_actual_text_are_preserved(monkeypatch):
    content = "328,58,43,607,90,示例企业招聘\n200,128,33,349,90,财务实习生"
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(success_payload(choices=[{"finish_reason": "stop", "message": {"content": content}}])))
    assert ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", IMAGE, "image/png").text == content


@pytest.mark.parametrize("color", ["white", "black", (128, 128, 128)])
def test_exactly_uniform_piece_returns_empty_text_without_api(monkeypatch, color):
    image = BytesIO()
    Image.new("RGB", (1080, 2000), color).save(image, "PNG")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(True) or response(success_payload()))
    result = ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", image.getvalue(), "image/png")
    assert calls == [], "an exactly blank piece must not invoke the provider"
    assert result.text == "" and result.finish_reason == "stop"
    assert result.model == "qwen-vl-ocr" and result.usage is None


def test_one_pixel_difference_is_never_skipped_as_blank(monkeypatch):
    image = BytesIO()
    source = Image.new("RGB", (10, 10), "white")
    source.putpixel((9, 9), (254, 255, 255))
    source.save(image, "PNG")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(True) or response(success_payload()))
    result = ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", image.getvalue(), "image/png")
    assert calls == [True]
    assert "财务实习生" in result.text


@pytest.mark.parametrize("all_blank", [False, True], ids=["blank-middle-keeps-tail", "all-blank-blocks-extraction"])
def test_empty_piece_result_is_compatible_with_persistent_worker(tmp_path, monkeypatch, all_blank):
    from app.database import create_database
    from app.models import ScreenshotImport, ScreenshotPiece, WorkItem
    from test_screenshot_tasks import FakeAI, add_import, queue_import, run_screenshot

    class RealOcrFakeTextAI(FakeAI):
        def freeze_ocr_request(self, session):
            return lambda image, mime: ocr_module().BailianOcrClient().recognize("fake-key", "qwen-vl-ocr", image, mime)

    factory = create_database(f"sqlite:///{(tmp_path / 'blank-piece-worker.db').as_posix()}")
    root = tmp_path / "uploads"
    root.mkdir()
    ai = RealOcrFakeTextAI()
    import_id = add_import(factory, root, pieces=3)
    with factory() as session:
        for index, piece in enumerate(session.query(ScreenshotPiece).order_by(ScreenshotPiece.id)):
            image = Image.new("RGB", (20, 20), "white")
            if not all_blank and index != 1:
                image.putpixel((0, 0), (0, 0, 0))
            image.save(root / piece.storage_key, "PNG")
    calls = []
    def post(*args, **kwargs):
        text = "图头：财务实习生" if not calls else "图尾：截止2099年12月31日；邮箱jobs@example.com"
        calls.append(text)
        return response(success_payload(choices=[{"finish_reason": "stop", "message": {"content": text}}]))
    monkeypatch.setattr(httpx, "post", post)
    queue_import(factory, import_id, ai)
    for _ in range(5):
        if not run_screenshot(factory, ai, root):
            break
    with factory() as session:
        record = session.get(ScreenshotImport, import_id)
        assert all(piece.status == "succeeded" for piece in session.query(ScreenshotPiece))
        assert session.query(ScreenshotPiece).order_by(ScreenshotPiece.id).all()[1].text == ""
        if all_blank:
            assert calls == [] and ai.text_calls == []
            assert record.status == "needs_attention"
            assert session.query(WorkItem).filter_by(kind="screenshot_extract_fields").one().error_code == "text_limit"
        else:
            assert len(calls) == 2 and len(ai.text_calls) == 1
            assert record.status == "ready"
            assert "图头" in record.ocr_text and "图尾" in record.ocr_text and "jobs@example.com" in record.ocr_text
