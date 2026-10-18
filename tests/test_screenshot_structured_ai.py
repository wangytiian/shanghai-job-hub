from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.services.ai_settings import (
    AiSettingsService,
    BailianClient,
    OpenAICompatibleClient,
    CredentialNotConfiguredError,
    TextProviderNotReadyError,
)


SCHEMA = {
    "name": "screenshot_prefill",
    "strict": True,
    "schema": {"type": "object", "properties": {"employer_name": {"type": "string"}},
               "required": ["employer_name"], "additionalProperties": False},
}
RESULT = '{"employer_name":"示例医院"}'


def capture_http(monkeypatch, replies=None):
    calls = []
    pending = list(replies or [(200, {"choices": [{"message": {"content": RESULT}}]},)])

    def post(url, **kwargs):
        calls.append((url, deepcopy(kwargs)))
        reply = pending.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.ai_settings.httpx.post", post)
    return calls


def screenshot_call(kind, model="other-model"):
    if kind == "bailian":
        return lambda: BailianClient().complete_screenshot("fake-secret", model, "extract", SCHEMA)
    return lambda: OpenAICompatibleClient().complete_screenshot(
        "fake-secret", "https://gateway.example.test/v1", model, "extract", SCHEMA, kind)


@pytest.mark.parametrize("kind", ["chat_completions", "bailian", "responses"])
def test_screenshot_sends_real_structured_schema_and_bounded_output(monkeypatch, kind):
    reply = {"output_text": RESULT} if kind == "responses" else {"choices": [{"message": {"content": RESULT}}]}
    calls = capture_http(monkeypatch, [(200, reply)])
    assert screenshot_call(kind)() == RESULT
    assert len(calls) == 1
    url, options = calls[0]
    payload = options["json"]
    if kind == "responses":
        assert url.endswith("/responses")
        assert payload["text"]["format"] == {"type": "json_schema", **SCHEMA}
        assert payload["max_output_tokens"] == 8192
        assert payload["input"] == "extract"
    else:
        assert url.endswith("/chat/completions")
        assert payload["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
        assert payload["max_tokens"] == 8192
    assert options["timeout"].read == 120
    assert options["timeout"].connect == options["timeout"].write == options["timeout"].pool == 45
    assert "reasoning" not in payload and "reasoning_effort" not in payload


@pytest.mark.parametrize("kind", ["chat_completions", "responses"])
def test_only_known_terra_model_gets_none_reasoning(monkeypatch, kind):
    reply = {"output_text": RESULT} if kind == "responses" else {"choices": [{"message": {"content": RESULT}}]}
    calls = capture_http(monkeypatch, [(200, reply)])
    assert screenshot_call(kind, "gpt-5.6-terra")() == RESULT
    payload = calls[0][1]["json"]
    if kind == "responses":
        assert payload["reasoning"] == {"effort": "none"}
        assert payload["max_output_tokens"] == 8192
    else:
        assert payload["reasoning_effort"] == "none"
        assert payload["max_completion_tokens"] == 8192
        assert "max_tokens" not in payload


@pytest.mark.parametrize("kind,param", [
    ("chat_completions", "response_format"), ("bailian", "response_format"),
    ("responses", "text.format"), ("chat_completions", "response_format.json_schema.strict"),
    ("responses", "text.format.strict"), ("chat_completions", "temperature"),
    ("chat_completions", "reasoning_effort"), ("responses", "reasoning.effort"),
    ("chat_completions", "max_completion_tokens"),
])
@pytest.mark.parametrize("status", [400, 422])
def test_explicit_unsupported_option_has_one_conservative_fallback(monkeypatch, kind, param, status):
    reply = {"output_text": RESULT} if kind == "responses" else {"choices": [{"message": {"content": RESULT}}]}
    calls = capture_http(monkeypatch, [(status, {"error": {"param": param, "message": f"Unsupported parameter: '{param}'."}}), (200, reply)])
    assert screenshot_call(kind, "gpt-5.6-terra")() == RESULT
    assert len(calls) == 2
    expected = deepcopy(calls[0][1]["json"])
    if param == "response_format":
        expected.pop("response_format")
    elif param == "text.format":
        expected.pop("text")
    elif param.endswith("strict"):
        (expected["text"]["format"] if kind == "responses" else expected["response_format"]["json_schema"]).pop("strict")
    elif param.startswith("reasoning"):
        expected.pop("reasoning" if kind == "responses" else "reasoning_effort")
    else:
        expected.pop(param)
    assert calls[1][1]["json"] == expected


@pytest.mark.parametrize("kind", ["chat_completions", "bailian", "responses"])
@pytest.mark.parametrize("status,body", [
    (400, {"error": {"message": "Bad request fake-secret"}}),
    (400, {"error": {"param": "response_format", "message": "Invalid schema fake-secret"}}),
    (400, {"error": {"param": "model", "message": "Unsupported model fake-secret"}}),
    (401, {"error": {"message": "Unsupported response_format fake-secret"}}),
    (403, {"error": {"message": "Unsupported response_format fake-secret"}}),
    (429, {"error": {"message": "Unsupported response_format fake-secret"}}),
    (500, {"error": {"message": "Unsupported response_format fake-secret"}}),
    (503, {"error": {"message": "Unsupported response_format fake-secret"}}),
])
def test_other_failures_never_retry_or_expose_raw_provider_body(monkeypatch, kind, status, body):
    calls = capture_http(monkeypatch, [(status, body)])
    with pytest.raises(httpx.HTTPStatusError) as captured:
        screenshot_call(kind)()
    assert len(calls) == 1
    assert captured.value.response.status_code == status
    assert "fake-secret" not in str(captured.value)
    assert "fake-secret" not in captured.value.response.text


@pytest.mark.parametrize("kind", ["chat_completions", "bailian", "responses"])
def test_timeout_never_retries_and_hides_raw_error(monkeypatch, kind):
    calls = capture_http(monkeypatch, [httpx.ReadTimeout("fake-secret response body")])
    with pytest.raises(httpx.ReadTimeout) as captured:
        screenshot_call(kind)()
    assert len(calls) == 1
    assert "fake-secret" not in str(captured.value)


def test_fallback_failure_does_not_trigger_third_request(monkeypatch):
    failure = (400, {"error": {"param": "response_format", "message": "Unsupported parameter response_format fake-secret"}})
    calls = capture_http(monkeypatch, [failure, failure])
    with pytest.raises(httpx.HTTPStatusError):
        screenshot_call("chat_completions")()
    assert len(calls) == 2


def test_null_error_param_uses_explicit_unsupported_parameter_message(monkeypatch):
    failure = (400, {"error": {"param": None, "message": "Unsupported parameter: response_format"}})
    success = (200, {"choices": [{"message": {"content": RESULT}}]})
    calls = capture_http(monkeypatch, [failure, success])
    assert screenshot_call("chat_completions")() == RESULT
    assert len(calls) == 2
    assert "response_format" not in calls[1][1]["json"]


def test_null_error_param_with_generic_invalid_request_does_not_retry(monkeypatch):
    calls = capture_http(monkeypatch, [(400, {"error": {"param": None, "message": "invalid request"}})])
    with pytest.raises(httpx.HTTPStatusError):
        screenshot_call("chat_completions")()
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["chat_completions", "responses", "bailian"])
def test_ordinary_text_path_keeps_original_payload(monkeypatch, kind):
    reply = {"output_text": RESULT} if kind == "responses" else {"choices": [{"message": {"content": RESULT}}]}
    calls = capture_http(monkeypatch, [(200, reply)])
    if kind == "bailian":
        BailianClient().complete("fake-secret", "gpt-5.6-terra", "ordinary")
    else:
        OpenAICompatibleClient().complete("fake-secret", "https://example.test", "gpt-5.6-terra", "ordinary", kind)
    payload = calls[0][1]["json"]
    assert set(payload) == ({"model", "instructions", "input"} if kind == "responses" else {"model", "messages", "temperature"})


@pytest.mark.parametrize("kind", ["chat_completions", "responses", "bailian"])
def test_frozen_screenshot_request_copies_configuration_schema_and_secret(monkeypatch, kind):
    setting = SimpleNamespace(provider="bailian" if kind == "bailian" else "openai_compatible", text_enabled=True,
                              text_model="old-model", base_url="https://old.example.test", api_mode=kind)
    store = SimpleNamespace(get_secret=lambda: "old-fake-secret")
    service = AiSettingsService(credential_store=store)
    session = object()
    reads = []

    def active(actual_session, *, commit):
        assert actual_session is session and commit is False
        reads.append(1)
        return setting

    monkeypatch.setattr(service, "get_active_text_setting", active)
    schema = deepcopy(SCHEMA)
    frozen = service.freeze_screenshot_text_request(session, schema)
    setting.base_url = "https://new.example.test"
    setting.text_model = "new-model"
    setting.api_mode = "responses" if kind != "responses" else "chat_completions"
    store.get_secret = lambda: pytest.fail("frozen request read credentials again")
    schema["schema"]["properties"].clear()
    monkeypatch.setattr(service, "get_active_text_setting", lambda *args, **kwargs: pytest.fail("frozen request read session again"))
    reply = {"output_text": RESULT} if kind == "responses" else {"choices": [{"message": {"content": RESULT}}]}
    calls = capture_http(monkeypatch, [(200, reply)])
    assert frozen("extract") == RESULT
    assert reads == [1]
    url, options = calls[0]
    assert "new.example" not in url
    assert options["headers"]["Authorization"] == "Bearer old-fake-secret"
    assert options["json"]["model"] == "old-model"
    sent_schema = options["json"]["text"]["format"] if kind == "responses" else options["json"]["response_format"]["json_schema"]
    assert sent_schema["schema"] == SCHEMA["schema"]


@pytest.mark.parametrize("enabled,key,url,model,error", [
    (False, "fake", "https://example.test", "model", TextProviderNotReadyError),
    (True, None, "https://example.test", "model", CredentialNotConfiguredError),
    (True, "fake", "", "model", TextProviderNotReadyError),
    (True, "fake", "https://example.test", "", TextProviderNotReadyError),
])
def test_frozen_screenshot_request_checks_provider_readiness(monkeypatch, enabled, key, url, model, error):
    service = AiSettingsService(credential_store=SimpleNamespace(get_secret=lambda: key))
    setting = SimpleNamespace(provider="openai_compatible", text_enabled=enabled, base_url=url, text_model=model, api_mode="responses")
    monkeypatch.setattr(service, "get_active_text_setting", lambda *args, **kwargs: setting)
    with pytest.raises(error):
        service.freeze_screenshot_text_request(object(), SCHEMA)
