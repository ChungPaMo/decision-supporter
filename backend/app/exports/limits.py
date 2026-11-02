"""/exports/* 요청 본문 제한 (ASGI 미들웨어).

Content-Length 는 믿지 않는다: 없거나 거짓일 수 있으므로 실제로 받은 바이트를 센다. 한도를 넘으면 앱을 호출하지 않고 413 을 돌려준다.
FastAPI 는 본문 수신 중의 예외를 400 으로 바꾸므로, 앱 안에서 막지 않고 끝까지 읽기 전에 여기서 끊는다. 메모리에는 한도까지만 쌓인다.
같은 이유로 지나치게 깊은 JSON 도 여기서 막는다: json 파서의 RecursionError 는 FastAPI 에서 공통 오류 형식이 아닌 400 이 된다.
"""

from __future__ import annotations

import json
import re

from app.errors import ApiError, ErrorCode

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_JSON_DEPTH = 16              # 계약의 실제 깊이는 5. 넉넉히 두되 파서가 재귀 한계에 닿기 훨씬 전에 끊는다
_JSON_STRING = re.compile(rb'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKET = re.compile(rb"[\[\]{}]")


def json_depth_exceeds(body: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
    """문자열 안의 괄호는 세지 않는다. 형식이 깨진 JSON 은 이후 파서가 422 로 처리한다."""
    depth = 0
    for m in _BRACKET.finditer(_JSON_STRING.sub(b"", body)):
        depth += 1 if m.group() in (b"[", b"{") else -1
        if depth > limit:
            return True
    return False


async def _respond(send, err: ApiError) -> None:
    payload = json.dumps(err.body(), ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": err.status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": payload})


class ExportBodyLimit:
    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/exports/"):
            await self.app(scope, receive, send)
            return
        too_large = ApiError(413, ErrorCode.EXPORT_TOO_LARGE, f"요청 본문이 너무 큽니다 (최대 {self.limit // 1048576}MB).")
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.limit:      # 선언만으로 넘으면 읽지 않고 거부
            await _respond(send, too_large)
            return
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":              # 연결 끊김: 응답할 상대가 없다
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.limit:
                await _respond(send, too_large)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if json_depth_exceeds(body):
            await _respond(send, ApiError(422, ErrorCode.REQUEST_INVALID, f"JSON 중첩이 너무 깊습니다 (최대 {MAX_JSON_DEPTH}단계)."))
            return
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
