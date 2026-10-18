import asyncio
import importlib
import pytest


@pytest.mark.parametrize("request_path", ["/imports/screenshots", "/imports/screenshots/"])
def test_upload_limit_rejects_before_authentication_reads_body(request_path):
    try:
        module = importlib.import_module("app.routes.screenshot_upload_limit")
    except ImportError:
        assert False, "missing outer upload request limiter"
    calls = []
    async def inner(scope, receive, send):
        calls.append("authenticated")
    events = iter([{"type":"http.request", "body":b"12345", "more_body":True},
                   {"type":"http.request", "body":b"67890", "more_body":False}])
    sent = []
    async def receive(): return next(events)
    async def send(message): sent.append(message)
    limiter = module.ScreenshotUploadLimit(inner, max_bytes=8)
    asyncio.run(limiter({"type":"http", "method":"POST", "path":request_path, "headers":[]}, receive, send))
    assert not calls
    assert sent[0]["status"] == 413
