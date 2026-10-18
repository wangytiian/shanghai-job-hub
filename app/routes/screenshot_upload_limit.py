"""Limit multipart bytes before authentication middleware can buffer the body."""

from starlette.responses import JSONResponse
from app.services.screenshot_contracts import MAX_REQUEST_BYTES


class ScreenshotUploadLimit:
    def __init__(self, app, max_bytes=MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path", "").rstrip("/") != "/imports/screenshots":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except (TypeError, ValueError):
            length = 0
        if length > self.max_bytes:
            return await self.reject(scope, receive, send)
        messages, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                return await self.reject(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)
        async def limited_receive():
            try:
                return next(iterator)
            except StopIteration:
                return await receive()
        return await self.app(scope, limited_receive, send)

    async def reject(self, scope, receive, send):
        response = JSONResponse({"error":"上传内容过大，请保持图片总量不超过25 MiB", "code":"request_too_large"}, status_code=413)
        return await response(scope, receive, send)
