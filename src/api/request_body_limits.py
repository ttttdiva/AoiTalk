"""ASGI request-body limits applied before FastAPI multipart parsing.

FastAPI resolves ``UploadFile`` parameters before endpoint dependencies.  A
route-level ``UploadFile.read(limit + 1)`` therefore cannot prevent an
oversized multipart body from first being copied into Starlette's spool file.
This middleware bounds the complete multipart request at the ASGI receive
boundary, before dependency resolution or multipart parsing can retain the
whole upload.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MIB = 1024 * 1024
DEFAULT_MULTIPART_BODY_MAX_BYTES = 512 * MIB
_SMALL_MULTIPART_OVERHEAD_BYTES = 1 * MIB
_APP_IMPORT_MULTIPART_OVERHEAD_BYTES = 8 * MIB
_DOCS_UPLOAD_DEFAULT_BYTES = 100 * MIB

_TASK_ATTACHMENT_PATH = re.compile(r"^/api/tasks/[^/]+/attachments$")
_APP_SOURCE_IMPORT_PATH = re.compile(
    r"^/api/apps/[^/]+/source-imports/preview$"
)
_X_COOKIE_PATH = "/api/users/me/x-cookie"
_X_COOKIE_BODY_MAX_BYTES = 2 * 1024 * 1024


class _MultipartBodyTooLarge(MultiPartException):
    """Internal signal that also makes Starlette close partial spool files."""

    def __init__(self) -> None:
        super().__init__("Multipart body exceeded the configured size limit")


class _MultipartBodyAborted(MultiPartException):
    """Make Starlette close partial multipart spool files on disconnect."""

    def __init__(self) -> None:
        super().__init__("Multipart body was interrupted")


def _bounded_docs_upload_bytes() -> int:
    try:
        configured = int(
            os.environ.get(
                "AOITALK_DOCS_CLIP_MAX_UPLOAD_BYTES",
                _DOCS_UPLOAD_DEFAULT_BYTES,
            )
        )
    except (TypeError, ValueError):
        configured = _DOCS_UPLOAD_DEFAULT_BYTES
    return max(1, configured)


def multipart_body_limit_for_path(path: str) -> int:
    """Return a finite complete-body limit for one multipart request path."""

    normalized = path.rstrip("/") or "/"
    if normalized == _X_COOKIE_PATH:
        return _X_COOKIE_BODY_MAX_BYTES
    if normalized == "/api/documents/upload" or _TASK_ATTACHMENT_PATH.fullmatch(
        normalized
    ):
        return 50 * MIB + _SMALL_MULTIPART_OVERHEAD_BYTES
    if normalized == "/api/users/import":
        return 2 * MIB + _SMALL_MULTIPART_OVERHEAD_BYTES
    if normalized == "/api/docs/ingest/uploads":
        return min(
            DEFAULT_MULTIPART_BODY_MAX_BYTES,
            _bounded_docs_upload_bytes() + _SMALL_MULTIPART_OVERHEAD_BYTES,
        )
    if _APP_SOURCE_IMPORT_PATH.fullmatch(normalized):
        return 250 * MIB + _APP_IMPORT_MULTIPART_OVERHEAD_BYTES
    if normalized == "/api/skill-recordings":
        return 500 * MIB + _SMALL_MULTIPART_OVERHEAD_BYTES
    # Legacy upload endpoints without their own bound remain compatible up to
    # this finite process-wide ceiling instead of accepting an unlimited spool.
    return DEFAULT_MULTIPART_BODY_MAX_BYTES


def raw_body_limit_for_path(path: str) -> int | None:
    """Return a finite limit for non-multipart credential bodies."""

    normalized = path.rstrip("/") or "/"
    if normalized == _X_COOKIE_PATH:
        return _X_COOKIE_BODY_MAX_BYTES
    return None


def _is_multipart_form_data(scope: Scope) -> bool:
    # Treat any duplicate Content-Type value selecting multipart as multipart.
    # Different proxy/server choices for duplicate headers must not let a
    # request bypass the outer size boundary.
    return any(
        value.split(b";", 1)[0].strip().lower() == b"multipart/form-data"
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-type"
    )


def _declared_content_length(scope: Scope) -> tuple[int | None, bool]:
    values = [
        value.strip()
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None, True
    # Duplicate, comma-joined, signed, or otherwise ambiguous lengths must not
    # select a weaker interpretation than the ASGI server/proxy uses.
    if len(values) != 1 or not values[0].isdigit():
        return None, False
    try:
        return int(values[0]), True
    except (ValueError, OverflowError):
        return None, False


async def _send_json_error(send: Send, status_code: int, detail: str) -> None:
    payload = json.dumps(
        {"detail": detail},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"private, no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class MultipartBodyLimitMiddleware:
    """Reject oversized multipart bodies at the ASGI receive boundary."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        route_limit_overrides: Mapping[str, int] | None = None,
        default_limit_bytes: int = DEFAULT_MULTIPART_BODY_MAX_BYTES,
    ) -> None:
        self.app = app
        self.route_limit_overrides = {
            str(path).rstrip("/") or "/": max(1, int(limit))
            for path, limit in (route_limit_overrides or {}).items()
        }
        self.default_limit_bytes = max(1, int(default_limit_bytes))

    def _limit_for_scope(self, scope: Scope) -> int:
        path = str(scope.get("path") or "")
        normalized = path.rstrip("/") or "/"
        override = self.route_limit_overrides.get(normalized)
        if override is not None:
            return override
        if self.default_limit_bytes != DEFAULT_MULTIPART_BODY_MAX_BYTES:
            return self.default_limit_bytes
        return multipart_body_limit_for_path(normalized)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Credential uploads are raw octet/text bodies, not multipart.  Buffer
        # only this small bounded route body before FastAPI sees it; this keeps
        # an unauthenticated oversized request from being retained by parser or
        # endpoint code while preserving the original ASGI messages below the
        # limit.
        raw_limit = raw_body_limit_for_path(str(scope.get("path") or ""))
        if raw_limit is not None and not _is_multipart_form_data(scope):
            declared_length, valid_length = _declared_content_length(scope)
            if not valid_length:
                await _send_json_error(send, 400, "Content-Length が不正です")
                return
            if declared_length is not None and declared_length > raw_limit:
                await _send_json_error(send, 413, "リクエスト本文が大きすぎます")
                return
            messages: list[Message] = []
            received = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    messages.append(message)
                    break
                body = message.get("body", b"")
                received += len(body)
                if received > raw_limit:
                    await _send_json_error(send, 413, "リクエスト本文が大きすぎます")
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            iterator = iter(messages)

            async def replay_receive() -> Message:
                try:
                    return next(iterator)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)
            return

        if not _is_multipart_form_data(scope):
            await self.app(scope, receive, send)
            return

        limit = self._limit_for_scope(scope)
        declared_length, valid_length = _declared_content_length(scope)
        if not valid_length:
            await _send_json_error(send, 400, "Content-Length が不正です")
            return
        if declared_length is not None and declared_length > limit:
            await _send_json_error(send, 413, "リクエスト本文が大きすぎます")
            return

        received = 0
        exceeded = False
        aborted = False

        async def limited_receive() -> Message:
            nonlocal aborted, exceeded, received
            message = await receive()
            if message["type"] == "http.disconnect":
                aborted = True
                # Starlette's multipart parser does not include
                # ClientDisconnect in its cleanup branch.  Translate it to a
                # private MultiPartException so every partial spool is closed.
                raise _MultipartBodyAborted
            if message["type"] == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > limit:
                    exceeded = True
                    # Do not expose the over-limit chunk to the multipart
                    # parser and do not drain the remaining attacker body.
                    raise _MultipartBodyTooLarge
            return message

        async def limited_send(message: Message) -> None:
            # FastAPI maps the private multipart marker to a generic 400.
            # Suppress that response and emit the correct 413 after the
            # downstream stack has closed its partial spool files.
            if not exceeded and not aborted:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _MultipartBodyAborted:
            pass
        except _MultipartBodyTooLarge:
            # Plain ASGI applications may not have FastAPI's body-error mapper.
            pass
        if aborted:
            # The peer has gone away.  Parser cleanup is complete, but sending
            # a synthetic response on the disconnected channel is invalid.
            return
        if exceeded:
            await _send_json_error(send, 413, "リクエスト本文が大きすぎます")


__all__ = [
    "DEFAULT_MULTIPART_BODY_MAX_BYTES",
    "MultipartBodyLimitMiddleware",
    "multipart_body_limit_for_path",
    "raw_body_limit_for_path",
]
