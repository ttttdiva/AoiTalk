"""JSON/text 応答だけを安全に圧縮する ASGI middleware。

Starlette の標準 GZipMiddleware は Range / 206 応答も圧縮対象にし得る。
部分配信のバイト位置と Content-Range を壊さないよう、Range・部分応答・
添付ファイル・バイナリ media は明示的に圧縮対象から除外する。
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_BINARY_CONTENT_TYPE_PREFIXES = (
    "application/octet-stream",
    "audio/",
    "font/",
    "image/",
    "model/",
    "multipart/byteranges",
    "video/",
)


def _response_must_remain_identity(message: Message) -> bool:
    headers = Headers(raw=message.get("headers", []))
    content_type = headers.get("content-type", "").lower()
    content_disposition = headers.get("content-disposition", "").lower()
    return (
        message.get("status") == 206
        or "content-range" in headers
        or content_disposition.startswith("attachment")
        or content_type.startswith(_BINARY_CONTENT_TYPE_PREFIXES)
    )


class SafeGZipResponder(GZipResponder):
    """標準 responder に Range / binary 除外判定を追加する。"""

    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await super().send_with_compression(message)
            if _response_must_remain_identity(message):
                self.content_type_is_excluded = True
            return
        await super().send_with_compression(message)


class SafeGZipMiddleware:
    """Accept-Encoding: gzip の安全な HTTP 応答だけを圧縮する。"""

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int = 500,
        compresslevel: int = 9,
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if "range" in headers:
            await self.app(scope, receive, send)
            return

        if "gzip" in headers.get("accept-encoding", ""):
            responder: ASGIApp = SafeGZipResponder(
                self.app,
                self.minimum_size,
                compresslevel=self.compresslevel,
            )
        else:
            responder = IdentityResponder(self.app, self.minimum_size)
        await responder(scope, receive, send)
