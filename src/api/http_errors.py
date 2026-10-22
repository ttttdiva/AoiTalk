"""Safe HTTP error boundary helpers for FastAPI applications."""

from __future__ import annotations

import logging
import os
import re
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config_errors import CharacterLookupErrorCategory, _safe_identifier
from src.llm.generation_error import GenerationErrorKind, user_message_for_generation_kind

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_DIAGNOSTIC_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_EXCEPTION_CLASS_LENGTH = 96
_MAX_SOURCE_PATH_LENGTH = 128
_MAX_SOURCE_FUNCTION_LENGTH = 64
_MAX_SOURCE_FRAME_LENGTH = 192
_MAX_SOURCE_LINE = 10_000_000


def _bounded_diagnostic_token(value: str, *, limit: int) -> str:
    """Keep diagnostic metadata printable and bounded without reading values."""

    token = _SAFE_DIAGNOSTIC_CHARS.sub("_", value)[:limit]
    return token or "unknown"


def _is_allowlisted_source_path(relative_path: Path) -> bool:
    return (
        (len(relative_path.parts) == 1 and relative_path.name == "main.py")
        or (
            len(relative_path.parts) >= 2
            and relative_path.parts[0] == "src"
            and relative_path.suffix == ".py"
        )
    )


def _safe_unhandled_exception_metadata(exc: BaseException) -> tuple[str, str]:
    """Return bounded exception class and allowlisted source-frame metadata.

    This helper intentionally reads only type/code-object metadata.  It never
    formats the exception, walks locals, or serializes a traceback.  A source
    frame is eligible only when its resolved path is ``main.py`` at the project
    root or a ``.py`` file below ``src/``; resolving before the relative-path
    check also excludes symlinks that point outside the repository.
    """

    try:
        exception_type = type(exc)
        class_parts = (
            getattr(exception_type, "__module__", None),
            getattr(exception_type, "__qualname__", None),
        )
        raw_class = ".".join(
            part for part in class_parts if isinstance(part, str) and part
        )
        exception_class = _bounded_diagnostic_token(
            raw_class,
            limit=_MAX_EXCEPTION_CLASS_LENGTH,
        )
        source_frame = "unknown"
        for frame, lineno in traceback.walk_tb(exc.__traceback__):
            try:
                filename = frame.f_code.co_filename
                if not isinstance(filename, str):
                    continue
                project_root = Path(os.path.abspath(str(_PROJECT_ROOT)))
                lexical_path = Path(os.path.abspath(filename))
                lexical_relative_path = lexical_path.relative_to(project_root)
                if not _is_allowlisted_source_path(lexical_relative_path):
                    continue

                # Resolve only after the lexical containment/allowlist check.
                # This avoids touching arbitrary outside or UNC paths while
                # still rejecting an allowlisted path that escapes through a
                # local symlink.
                resolved_path = lexical_path.resolve()
                relative_path = resolved_path.relative_to(project_root)
                if not _is_allowlisted_source_path(relative_path):
                    continue

                safe_path = "/".join(
                    _bounded_diagnostic_token(part, limit=64)
                    for part in relative_path.parts
                )[:_MAX_SOURCE_PATH_LENGTH]
                function_name = frame.f_code.co_name
                if not isinstance(function_name, str):
                    function_name = "unknown"
                safe_function = _bounded_diagnostic_token(
                    function_name,
                    limit=_MAX_SOURCE_FUNCTION_LENGTH,
                )
                try:
                    safe_lineno = max(0, min(int(lineno), _MAX_SOURCE_LINE))
                except (TypeError, ValueError, OverflowError):
                    safe_lineno = 0
                source_suffix = f":{safe_function}:{safe_lineno}"
                path_limit = max(
                    1,
                    _MAX_SOURCE_FRAME_LENGTH - len(source_suffix),
                )
                source_frame = f"{safe_path[:path_limit]}{source_suffix}"
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                continue
        return exception_class, source_frame
    except Exception:
        return "unknown", "unknown"


INTERNAL_ERROR_CATEGORY = "internal_error"
INTERNAL_ERROR_CODE = "internal_error"
LLM_UNAVAILABLE_CATEGORY = "llm_unavailable"
STORAGE_UNAVAILABLE_CATEGORY = "storage_unavailable"
_SAFE_STORAGE_ERROR_MESSAGES = {
    "storage_unavailable": "素材の保存先を利用できません。接続状態と登録記録を確認してください。",
    "storage_busy": "素材の保存先で処理が続いています。少し待ってから登録記録を確認してください。",
}
_SAFE_LLM_ERROR_CODES = frozenset(
    {
        GenerationErrorKind.INSUFFICIENT_QUOTA,
        GenerationErrorKind.RATE_LIMIT,
        GenerationErrorKind.AUTHENTICATION,
        GenerationErrorKind.PERMISSION_DENIED,
        GenerationErrorKind.MODEL_NOT_FOUND,
        GenerationErrorKind.PRICE_LIMIT,
        GenerationErrorKind.INVALID_REQUEST,
        GenerationErrorKind.CONTEXT_LENGTH,
        GenerationErrorKind.CONNECTION,
        GenerationErrorKind.TIMEOUT,
        GenerationErrorKind.SERVER_ERROR,
        GenerationErrorKind.EMPTY_RESPONSE,
        GenerationErrorKind.LLM_NOT_CONFIGURED,
    }
)
_RETRYABLE_LLM_ERROR_CODES = frozenset(
    {
        GenerationErrorKind.RATE_LIMIT,
        GenerationErrorKind.CONNECTION,
        GenerationErrorKind.TIMEOUT,
        GenerationErrorKind.SERVER_ERROR,
        GenerationErrorKind.EMPTY_RESPONSE,
    }
)
_SAFE_ERROR_CATEGORIES = frozenset(
    {
        INTERNAL_ERROR_CATEGORY,
        "not_found",
        LLM_UNAVAILABLE_CATEGORY,
        STORAGE_UNAVAILABLE_CATEGORY,
        *(member.value for member in CharacterLookupErrorCategory),
    }
)
_SAFE_ERROR_CODES = _SAFE_ERROR_CATEGORIES | frozenset({"character_not_found"})
_SAFE_DETAIL_FIELDS = frozenset(
    {"category", "code", "trace_id", "request_id", "message"}
)


def _allowlisted_machine_value(
    value: object,
    *,
    allowlist: frozenset[str],
    fallback: str,
) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in allowlist:
        return candidate
    return fallback


def request_correlation_ids(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    headers = request.headers
    return (
        headers.get("x-request-id") or headers.get("x-correlation-id"),
        headers.get("x-trace-id"),
    )


def build_internal_error_payload(
    request: Request | None,
    *,
    trace_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    header_request_id, header_trace_id = request_correlation_ids(request)
    resolved_trace_id = (
        _safe_identifier(trace_id)
        or _safe_identifier(header_trace_id)
        or uuid.uuid4().hex
    )
    resolved_request_id = _safe_identifier(request_id) or _safe_identifier(
        header_request_id
    )
    payload = {
        "category": INTERNAL_ERROR_CATEGORY,
        "code": INTERNAL_ERROR_CODE,
        "trace_id": resolved_trace_id,
        "message": (
            "An unexpected internal error occurred. "
            f"trace_id={resolved_trace_id}"
        ),
    }
    if resolved_request_id:
        payload["request_id"] = resolved_request_id
    return payload


def build_storage_unavailable_detail(code: str) -> dict[str, str] | None:
    """Canonical public detail only; never forward a storage exception string."""
    message = _SAFE_STORAGE_ERROR_MESSAGES.get(code)
    if message is None:
        return None
    return {"category": STORAGE_UNAVAILABLE_CATEGORY, "code": code, "message": message}


def whitelist_error_detail(
    detail: object,
    request: Request | None,
) -> dict[str, Any]:
    """Rebuild a 5xx payload from an allowlisted field set only."""

    base = build_internal_error_payload(request)
    if not isinstance(detail, dict):
        return base

    trace_id = _safe_identifier(detail.get("trace_id")) or base["trace_id"]
    request_id = _safe_identifier(detail.get("request_id")) or base.get("request_id")
    category = _allowlisted_machine_value(
        detail.get("category"),
        allowlist=_SAFE_ERROR_CATEGORIES,
        fallback=INTERNAL_ERROR_CATEGORY,
    )
    raw_code = detail.get("code")
    if raw_code is None or not str(raw_code).strip():
        code = category
    else:
        code_allowlist = _SAFE_ERROR_CODES
        if category == LLM_UNAVAILABLE_CATEGORY:
            code_allowlist |= _SAFE_LLM_ERROR_CODES
        elif category == STORAGE_UNAVAILABLE_CATEGORY:
            code_allowlist |= frozenset(_SAFE_STORAGE_ERROR_MESSAGES)
        code = _allowlisted_machine_value(
            raw_code,
            allowlist=code_allowlist,
            fallback=INTERNAL_ERROR_CODE,
        )

    message = f"An unexpected internal error occurred. trace_id={trace_id}"
    retryable: bool | None = None
    if category == LLM_UNAVAILABLE_CATEGORY:
        # A typed LLM error may expose only the exact static message generated
        # by ``classify_generation_error``.  Valid category/code pairs with a
        # caller-supplied message are treated as untrusted and downgraded to
        # the generic internal payload; this keeps arbitrary dicts from
        # becoming a message injection path.
        expected_message = user_message_for_generation_kind(code)
        if (
            code not in _SAFE_LLM_ERROR_CODES
            or expected_message is None
            or detail.get("message") != expected_message
        ):
            category = INTERNAL_ERROR_CATEGORY
            code = INTERNAL_ERROR_CODE
        else:
            message = expected_message
            # Retryability is derived from the canonical code.  Ignore any
            # caller-provided ``retryable`` field, which is untrusted input.
            retryable = code in _RETRYABLE_LLM_ERROR_CODES
    elif category == STORAGE_UNAVAILABLE_CATEGORY:
        expected_message = _SAFE_STORAGE_ERROR_MESSAGES.get(code)
        if expected_message is None or detail.get("message") != expected_message:
            category = INTERNAL_ERROR_CATEGORY
            code = INTERNAL_ERROR_CODE
        else:
            message = expected_message
    elif code == STORAGE_UNAVAILABLE_CATEGORY:
        # An untyped storage exception is still untrusted. Only the canonical
        # category/code/message tuple may identify a storage availability error.
        code = INTERNAL_ERROR_CODE

    payload: dict[str, Any] = {
        "category": category,
        "code": code,
        "trace_id": trace_id,
        "message": message,
    }
    if request_id:
        payload["request_id"] = request_id
    if retryable is not None:
        payload["retryable"] = retryable
    return payload


def _json_error_response(
    *,
    status_code: int,
    detail: Any,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )


def register_http_error_handlers(app: FastAPI) -> None:
    """Register secret-free handlers for unhandled API failures."""

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code < 500:
            return _json_error_response(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=dict(exc.headers or {}),
            )
        payload = whitelist_error_detail(exc.detail, request)
        logger.error(
            "HTTP %s without safe detail at %s %s trace_id=%s",
            exc.status_code,
            request.method,
            request.url.path,
            payload["trace_id"],
        )
        return _json_error_response(status_code=exc.status_code, detail=payload)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        if isinstance(exc, HTTPException):
            if exc.status_code < 500:
                return _json_error_response(
                    status_code=exc.status_code,
                    detail=exc.detail,
                    headers=dict(exc.headers or {}),
                )
            payload = whitelist_error_detail(exc.detail, request)
            logger.error(
                "HTTPException without safe detail at %s %s trace_id=%s",
                request.method,
                request.url.path,
                payload["trace_id"],
            )
            return _json_error_response(status_code=exc.status_code, detail=payload)

        payload = build_internal_error_payload(request)
        exception_class, source_frame = _safe_unhandled_exception_metadata(exc)
        logger.error(
            "Unhandled exception at %s %s trace_id=%s request_id=%s "
            "exception_class=%s source_frame=%s",
            request.method,
            request.url.path,
            payload["trace_id"],
            payload.get("request_id", "-"),
            exception_class,
            source_frame,
        )
        return _json_error_response(status_code=500, detail=payload)
