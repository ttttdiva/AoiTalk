from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    Security,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import FormData, Headers
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..memory.models import MeetingProcessingJob
from ..services.clip_ingest_storage import ClipUploadError
from ..services.meeting_processing_storage import (
    MEETING_AUDIO_MAX_DURATION_SECONDS,
    MEETING_AUDIO_SUFFIXES,
    MeetingAudioStorage,
)
from ..services.meeting_processing_retry import (
    RetryCasOutcome,
    mark_audio_integrity_failed_cas,
    requeue_failed_job_cas,
)
from .meeting_processing_contract import (
    CONTRACT_VERSION,
    CONTRACT_VERSION_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    MeetingAudioInfo,
    MeetingProcessingCreateRequest,
    MeetingProcessingError,
    MeetingProcessingErrorEnvelope,
    MeetingProcessingHealthResponse,
    MeetingProcessingJobResponse,
    MeetingProcessingLimits,
    MeetingProcessingReadinessChecks,
    MeetingProcessingReadinessResponse,
    MeetingProcessingResult,
)


_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MEETING_REQUEST_JSON_MAX_BYTES = 256 * 1024
_MEETING_MULTIPART_HEADER_MAX_BYTES = 16 * 1024
_MEETING_ALLOWED_PARTS = frozenset({"audio", "request"})

ReadinessProvider = Callable[
    [],
    Mapping[str, bool] | Awaitable[Mapping[str, bool]],
]
TokenResolver = Callable[..., Any]


def _meeting_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    stage: str = "queued",
    retryable: bool = False,
    details: Mapping[str, Any] | None = None,
) -> HTTPException:
    payload = MeetingProcessingError(
        code=code,
        message=message,
        stage=stage,
        retryable=retryable,
        details=dict(details or {}),
    )
    return HTTPException(
        status_code=status_code,
        detail=payload.model_dump(mode="json"),
    )


def _http_error(
    status_code: int,
    *,
    code: str,
    message: str,
    stage: str,
    retryable: bool,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Raise a typed meeting-processing error for existing call sites."""

    raise _meeting_http_error(
        status_code,
        code,
        message,
        stage=stage,
        retryable=retryable,
        details=details,
    )


def _single_header(
    request: Request,
    name: str,
) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        return None
    if len(values) != 1 or "," in values[0]:
        _http_error(
            400,
            code="request.ambiguous_header",
            message=f"{name} header is ambiguous",
            stage="request",
            retryable=False,
        )
    return values[0].strip()


def _require_contract_version(request: Request) -> None:
    version = _single_header(request, CONTRACT_VERSION_HEADER)
    if version != CONTRACT_VERSION:
        _http_error(
            426,
            code="contract.version_unsupported",
            message="Unsupported meeting-processing contract version",
            stage="contract",
            retryable=False,
            details={
                "received": version,
                "supported_versions": [CONTRACT_VERSION],
            },
        )


def _require_idempotency_key(request: Request) -> str:
    value = _single_header(request, IDEMPOTENCY_KEY_HEADER)
    if not value:
        _http_error(
            400,
            code="request.idempotency_key_required",
            message="Idempotency-Key is required",
            stage="request",
            retryable=False,
        )
    assert value is not None
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        _http_error(
            400,
            code="request.idempotency_key_invalid",
            message="Idempotency-Key is invalid",
            stage="request",
            retryable=False,
        )
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_upload(upload: Any) -> None:
    close = getattr(upload, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception:
            pass


def _request_fingerprint(
    request_model: MeetingProcessingCreateRequest,
    audio_sha256: str,
) -> str:
    canonical_request = json.dumps(
        request_model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    digest = hashlib.sha256()
    digest.update(canonical_request)
    digest.update(b"\0")
    digest.update(audio_sha256.encode("ascii"))
    return digest.hexdigest()


class _MeetingMultipartError(MultiPartException):
    """A bounded, typed error raised by the strict meeting multipart parser."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)


class _StrictMeetingMultipartParser(MultiPartParser):
    """Parse exactly one bounded ``audio`` file and ``request`` text field.

    Starlette's ``max_part_size`` applies to text fields only.  This parser
    keeps an explicit per-part byte counter for both text and file parts and
    rejects duplicate/unknown fields as soon as their headers are complete.
    The outer body-limit middleware remains the complete-request guard.
    """

    def __init__(
        self,
        *,
        headers: Headers,
        stream: AsyncIterator[bytes],
        audio_max_bytes: int,
    ) -> None:
        kwargs: dict[str, Any] = {
            "headers": headers,
            "stream": stream,
            # Keep enough headroom for our own duplicate/unknown checks.  The
            # semantic two-part contract is enforced below.
            "max_files": 4,
            "max_fields": 4,
        }

        # Newer Starlette releases expose max_part_size.  It is a second
        # defense for the JSON text part; our on_part_data counter is the
        # authoritative limit across supported releases.
        if "max_part_size" in inspect.signature(
            MultiPartParser.__init__
        ).parameters:
            kwargs["max_part_size"] = _MEETING_REQUEST_JSON_MAX_BYTES

        super().__init__(**kwargs)
        self._audio_max_bytes = int(audio_max_bytes)
        self._seen_parts: set[str] = set()
        self._current_name: str | None = None
        self._current_part_bytes = 0
        self._current_header_bytes = 0

    def on_part_begin(self) -> None:
        self._current_name = None
        self._current_part_bytes = 0
        self._current_header_bytes = 0
        super().on_part_begin()

    def _count_header_bytes(self, data: bytes, start: int, end: int) -> None:
        self._current_header_bytes += max(0, end - start)
        if self._current_header_bytes > _MEETING_MULTIPART_HEADER_MAX_BYTES:
            raise _MeetingMultipartError(
                "multipart part headers are too large",
                status_code=status.HTTP_400_BAD_REQUEST,
                code="request.invalid_multipart",
            )

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header_bytes(data, start, end)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header_bytes(data, start, end)
        super().on_header_value(data, start, end)

    def on_headers_finished(self) -> None:
        # ``super`` parses Content-Disposition and, for file parts, allocates
        # a spool.  If our contract rejects the part, MultiPartParser's
        # exception cleanup closes that spool.
        super().on_headers_finished()

        name = str(self._current_part.field_name or "")
        if name not in _MEETING_ALLOWED_PARTS:
            raise _MeetingMultipartError(
                f"unknown multipart part: {name or '<empty>'}",
                status_code=status.HTTP_400_BAD_REQUEST,
                code="request.invalid_multipart",
            )

        if name in self._seen_parts:
            raise _MeetingMultipartError(
                f"duplicate multipart part: {name}",
                status_code=status.HTTP_400_BAD_REQUEST,
                code="request.invalid_multipart",
            )

        is_file = self._current_part.file is not None
        if name == "audio" and not is_file:
            raise _MeetingMultipartError(
                "audio must be a file part",
                status_code=status.HTTP_400_BAD_REQUEST,
                code="request.invalid_multipart",
            )
        if name == "request" and is_file:
            raise _MeetingMultipartError(
                "request must be a text field",
                status_code=status.HTTP_400_BAD_REQUEST,
                code="request.invalid_multipart",
            )

        self._seen_parts.add(name)
        self._current_name = name
        self._current_part_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self._current_part_bytes += max(0, end - start)

        if self._current_name == "request":
            if self._current_part_bytes > _MEETING_REQUEST_JSON_MAX_BYTES:
                raise _MeetingMultipartError(
                    "meeting request JSON is too large",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    code="request.multipart_part_too_large",
                )
        elif self._current_name == "audio":
            if self._current_part_bytes > self._audio_max_bytes:
                raise _MeetingMultipartError(
                    "meeting audio is too large",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    code="request.multipart_part_too_large",
                )

        super().on_part_data(data, start, end)


def _single_multipart_content_type(request: Request) -> Headers:
    """Return raw request headers after rejecting ambiguous Content-Type."""

    raw_headers = list(request.scope.get("headers", ()))
    values = [
        value.decode("latin-1")
        for name, value in raw_headers
        if name.lower() == b"content-type"
    ]

    if len(values) != 1:
        raise _meeting_http_error(
            status.HTTP_400_BAD_REQUEST,
            "request.invalid_multipart",
            "exactly one Content-Type header is required",
        )

    media_type = values[0].split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise _meeting_http_error(
            status.HTTP_400_BAD_REQUEST,
            "request.invalid_multipart",
            "Content-Type must be multipart/form-data",
        )

    return Headers(raw=raw_headers)


async def _close_parser_spools(parser: _StrictMeetingMultipartParser) -> None:
    """Best-effort close of parser-owned spools on non-standard failures."""

    for handle in tuple(
        getattr(parser, "_files_to_close_on_error", ())
    ):
        try:
            if bool(getattr(handle, "closed", False)):
                continue
            result = handle.close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass


@asynccontextmanager
async def _parse_meeting_multipart(
    request: Request,
    storage: MeetingAudioStorage,
):
    """Yield exactly one audio UploadFile and one bounded JSON string."""

    headers = _single_multipart_content_type(request)
    parser = _StrictMeetingMultipartParser(
        headers=headers,
        stream=request.stream(),
        audio_max_bytes=storage.max_upload_bytes,
    )
    form: FormData | None = None

    try:
        try:
            form = await parser.parse()
        except _MeetingMultipartError as exc:
            raise _meeting_http_error(
                exc.status_code,
                exc.code,
                str(exc),
            ) from exc
        except MultiPartException as exc:
            raise _meeting_http_error(
                status.HTTP_400_BAD_REQUEST,
                "request.invalid_multipart",
                "multipart body is malformed",
            ) from exc
        except Exception as exc:
            # Keep malformed/decoder failures on the same typed wire while
            # ensuring parser-owned spools are closed in the finally block.
            raise _meeting_http_error(
                status.HTTP_400_BAD_REQUEST,
                "request.invalid_multipart",
                "multipart body is malformed",
                details={"type": type(exc).__name__},
            ) from exc

        names = [key for key, _value in form.multi_items()]
        if sorted(names) != ["audio", "request"]:
            raise _meeting_http_error(
                status.HTTP_400_BAD_REQUEST,
                "request.invalid_multipart",
                "multipart body must contain exactly audio and request",
            )

        audio = form.get("audio")
        request_value = form.get("request")
        if not isinstance(audio, StarletteUploadFile):
            raise _meeting_http_error(
                status.HTTP_400_BAD_REQUEST,
                "request.invalid_multipart",
                "audio must be a file part",
            )
        if isinstance(request_value, StarletteUploadFile) or not isinstance(
            request_value,
            str,
        ):
            raise _meeting_http_error(
                status.HTTP_400_BAD_REQUEST,
                "request.invalid_multipart",
                "request must be a JSON text field",
            )

        yield audio, request_value
    finally:
        if form is not None:
            try:
                await form.close()
            except Exception:
                pass
        else:
            await _close_parser_spools(parser)


def _inline_local_json_schema_refs(
    model: type[BaseModel],
) -> dict[str, Any]:
    """Inline a Pydantic model's local definitions for multipart metadata."""

    raw = deepcopy(model.model_json_schema())
    definitions = dict(raw.pop("$defs", {}) or {})

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value

        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in stack:
                raise RuntimeError(
                    "recursive request schema is unsupported for multipart OpenAPI: "
                    f"{name}"
                )
            target = definitions.get(name)
            if target is None:
                raise RuntimeError(
                    f"unresolved local JSON schema ref: {reference}"
                )
            resolved = expand(deepcopy(target), stack + (name,))
            siblings = {
                key: item
                for key, item in value.items()
                if key != "$ref"
            }
            if siblings:
                if not isinstance(resolved, dict):
                    raise RuntimeError(f"invalid referenced schema: {reference}")
                resolved.update(expand(siblings, stack))
            return resolved

        return {
            key: expand(item, stack)
            for key, item in value.items()
            if key != "$defs"
        }

    return expand(raw)


_MEETING_CREATE_REQUEST_SCHEMA = _inline_local_json_schema_refs(
    MeetingProcessingCreateRequest
)

_MEETING_CREATE_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["audio", "request"],
                    "properties": {
                        "audio": {
                            "type": "string",
                            "format": "binary",
                            "description": "Meeting audio payload.",
                        },
                        "request": {
                            "type": "string",
                            "contentMediaType": "application/json",
                            "contentSchema": _MEETING_CREATE_REQUEST_SCHEMA,
                            "description": (
                                "JSON-encoded MeetingProcessingCreateRequest."
                            ),
                        },
                    },
                },
                "encoding": {
                    "request": {"contentType": "application/json"},
                },
            },
        },
    },
}


_OPENAPI_AOITALK_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="AoiTalkLongLivedToken",
    bearerFormat="aoitpat_",
    description=(
        "AoiTalk long-lived API token. Runtime authorization additionally "
        "validates active user state, password-reset state and session_version."
    ),
)

ContractVersionSchemaHeader = Annotated[
    str | None,
    Header(
        alias=CONTRACT_VERSION_HEADER,
        description="Meeting-processing contract version. Must be 1.0.",
    ),
]

IdempotencySchemaHeader = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_KEY_HEADER,
        description="Actor-scoped idempotency key for POST /jobs.",
    ),
]

AoiTalkBearerSchema = Annotated[
    HTTPAuthorizationCredentials | None,
    Security(_OPENAPI_AOITALK_BEARER),
]


def _job_response(
    job: MeetingProcessingJob,
) -> MeetingProcessingJobResponse:
    """Serialize a job using the stable HTTP response model."""

    return _serialize_job(job)


def _retry_cas_loss_response(
    outcome: RetryCasOutcome,
    *,
    expected_generation: int,
) -> MeetingProcessingJobResponse:
    """Map a losing retry CAS to an idempotent response or typed conflict."""

    current = outcome.job
    if current is None:
        raise _meeting_http_error(
            status.HTTP_404_NOT_FOUND,
            "job.not_found",
            "meeting processing job was not found",
            stage="request",
            retryable=False,
        )

    # A concurrent retry or worker already moved the job forward.  Returning
    # that snapshot is idempotent and must never rewind a newer generation.
    if current.status in {"queued", "running"}:
        return _job_response(current)

    if current.status == "succeeded":
        raise _meeting_http_error(
            status.HTTP_409_CONFLICT,
            "retry.job_already_succeeded",
            "succeeded meeting processing jobs cannot be retried",
            stage=str(current.stage or "complete"),
            retryable=False,
        )

    if current.status == "failed" and not current.retryable:
        current_error = current.error_json or {}
        raise _meeting_http_error(
            status.HTTP_409_CONFLICT,
            str(current_error.get("code") or "retry.not_retryable"),
            str(
                current_error.get("message")
                or "meeting processing job is not retryable"
            ),
            stage=str(current.stage or "queued"),
            retryable=False,
            details=dict(current_error.get("details") or {}),
        )

    current_generation = int(current.retry_generation or 0)
    raise _meeting_http_error(
        status.HTTP_409_CONFLICT,
        "retry.concurrent_state_changed",
        "meeting processing job changed during retry",
        stage=str(current.stage or "queued"),
        retryable=bool(current.retryable),
        details={
            "expected_generation": expected_generation,
            "current_generation": current_generation,
            "current_status": current.status,
        },
    )


def _serialize_job(
    row: MeetingProcessingJob,
) -> MeetingProcessingJobResponse:
    result = None
    if row.status == "succeeded":
        result = MeetingProcessingResult.model_validate(
            row.result_json or {}
        )

    error = None
    if row.status == "failed" and row.error_json:
        error = MeetingProcessingError.model_validate(
            row.error_json
        )

    return MeetingProcessingJobResponse(
        job_id=str(row.id),
        status=row.status,
        stage=row.stage,
        idempotency_key=row.idempotency_key,
        retry_generation=int(row.retry_generation or 0),
        attempt_count=int(row.attempt_count or 0),
        retryable=bool(
            row.status == "failed" and row.retryable
        ),
        audio=MeetingAudioInfo(
            file_name=row.audio_file_name,
            mime_type=row.audio_mime_type,
            size_bytes=int(row.audio_size_bytes),
            sha256=row.audio_sha256,
        ),
        result=result,
        error=error,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


def create_meeting_processing_router(
    *,
    db_manager: Any,
    resolve_long_lived_token: TokenResolver | None,
    readiness_provider: ReadinessProvider | None = None,
    storage: MeetingAudioStorage | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/meeting-processing",
        tags=["meeting-processing"],
    )
    audio_storage = storage or MeetingAudioStorage()

    async def resolve_actor(request: Request) -> UUID:
        authorization = _single_header(
            request,
            "Authorization",
        )
        if not authorization:
            _http_error(
                401,
                code="auth.long_lived_token_required",
                message="Bearer aoitpat_ token is required",
                stage="auth",
                retryable=False,
            )

        assert authorization is not None
        scheme, separator, token = authorization.partition(" ")
        token = token.strip()

        if (
            scheme.lower() != "bearer"
            or not separator
            or not token.startswith("aoitpat_")
        ):
            _http_error(
                401,
                code="auth.long_lived_token_required",
                message="Bearer aoitpat_ token is required",
                stage="auth",
                retryable=False,
            )

        if resolve_long_lived_token is None:
            _http_error(
                503,
                code="dependency.authentication_unavailable",
                message="Authentication backend is unavailable",
                stage="auth",
                retryable=True,
            )

        try:
            principal = await _maybe_await(
                resolve_long_lived_token(
                    token,
                    raise_on_db_error=True,
                )
            )
        except Exception:
            _http_error(
                503,
                code="dependency.database_unavailable",
                message="Authentication database is unavailable",
                stage="auth",
                retryable=True,
            )

        if not principal:
            _http_error(
                401,
                code="auth.unauthorized",
                message="Token is invalid or inactive",
                stage="auth",
                retryable=False,
            )

        try:
            return UUID(str(principal["id"]))
        except (KeyError, TypeError, ValueError):
            _http_error(
                503,
                code="auth.principal_invalid",
                message="Authenticated principal is invalid",
                stage="auth",
                retryable=False,
            )
        raise AssertionError("unreachable")

    async def database_ready() -> bool:
        if db_manager is None:
            return False
        session = None
        try:
            session = await db_manager.get_session()
            await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            if session is not None:
                await session.close()

    async def readiness_snapshot(
        *,
        authenticate_request: Request | None = None,
    ) -> MeetingProcessingReadinessResponse:
        if authenticate_request is not None:
            _require_contract_version(authenticate_request)
            await resolve_actor(authenticate_request)

        checks: dict[str, bool] = {
            "database": await database_ready(),
            "worker": False,
            "whisper": False,
            "local_llm": False,
            "docs": False,
        }

        if readiness_provider is not None:
            try:
                supplied = await _maybe_await(
                    readiness_provider()
                )
                if isinstance(supplied, Mapping):
                    for key in (
                        "worker",
                        "whisper",
                        "local_llm",
                        "docs",
                    ):
                        checks[key] = bool(
                            supplied.get(key, False)
                        )
            except Exception:
                pass

        response = MeetingProcessingReadinessResponse(
            ready=all(checks.values()),
            checks=MeetingProcessingReadinessChecks(
                **checks,
            ),
            limits=MeetingProcessingLimits(
                max_audio_bytes=audio_storage.max_upload_bytes,
                max_duration_seconds=(
                    MEETING_AUDIO_MAX_DURATION_SECONDS
                ),
                allowed_suffixes=sorted(
                    MEETING_AUDIO_SUFFIXES
                ),
            ),
        )
        return response

    async def require_ready(request: Request) -> None:
        snapshot = await readiness_snapshot(
            authenticate_request=request,
        )
        if not snapshot.ready:
            _http_error(
                503,
                code="readiness.unavailable",
                message="Meeting processing is not ready",
                stage="readiness",
                retryable=True,
                details={
                    "checks": snapshot.checks.model_dump(),
                },
            )

    async def cleanup_upload(
        actor_id: UUID,
        upload_id: str | None,
    ) -> None:
        if not upload_id:
            return
        try:
            await audio_storage.cleanup_uploads(
                actor_id,
                [upload_id],
            )
        except Exception:
            pass

    @router.get(
        "/health",
        response_model=MeetingProcessingHealthResponse,
        operation_id="meetingProcessingHealth",
        tags=["meeting-processing"],
    )
    async def health() -> MeetingProcessingHealthResponse:
        return MeetingProcessingHealthResponse()

    @router.get(
        "/ready",
        response_model=MeetingProcessingReadinessResponse,
        operation_id="meetingProcessingReady",
        tags=["meeting-processing"],
        responses={
            401: {"model": MeetingProcessingErrorEnvelope},
            426: {"model": MeetingProcessingErrorEnvelope},
            503: {
                "model": MeetingProcessingReadinessResponse,
                "description": "Meeting processing is not ready.",
            },
        },
    )
    async def ready(
        request: Request,
        _openapi_contract_version: ContractVersionSchemaHeader = None,
        _openapi_security: AoiTalkBearerSchema = None,
    ):
        snapshot = await readiness_snapshot(
            authenticate_request=request,
        )
        return JSONResponse(
            status_code=200 if snapshot.ready else 503,
            content=snapshot.model_dump(mode="json"),
        )

    @router.post(
        "/jobs",
        response_model=MeetingProcessingJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="meetingProcessingCreateJob",
        tags=["meeting-processing"],
        openapi_extra=_MEETING_CREATE_OPENAPI_EXTRA,
        responses={
            400: {"model": MeetingProcessingErrorEnvelope},
            401: {"model": MeetingProcessingErrorEnvelope},
            409: {"model": MeetingProcessingErrorEnvelope},
            413: {"model": MeetingProcessingErrorEnvelope},
            415: {"model": MeetingProcessingErrorEnvelope},
            422: {"model": MeetingProcessingErrorEnvelope},
            426: {"model": MeetingProcessingErrorEnvelope},
            503: {"model": MeetingProcessingErrorEnvelope},
        },
    )
    async def create_job(
        request: Request,
        _openapi_contract_version: ContractVersionSchemaHeader = None,
        _openapi_idempotency_key: IdempotencySchemaHeader = None,
        _openapi_security: AoiTalkBearerSchema = None,
    ):
        _require_contract_version(request)
        actor_id = await resolve_actor(request)
        idempotency_key = _require_idempotency_key(
            request
        )

        # Crucially executed before request.form(): unavailable
        # servers do not spool the uploaded audio.
        await require_ready(request)

        staged = None
        async with _parse_meeting_multipart(
            request,
            audio_storage,
        ) as (audio, request_text):
            try:
                request_model = (
                    MeetingProcessingCreateRequest.model_validate_json(
                        request_text
                    )
                )
            except ValidationError as exc:
                raise _meeting_http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "request.invalid_request",
                    "meeting processing request JSON is invalid",
                    stage="request",
                    retryable=False,
                    details={
                        "errors": exc.errors(
                            include_url=False,
                            include_input=False,
                        ),
                    },
                ) from exc

            try:
                audio_storage.validate_file_name(
                    getattr(audio, "filename", None)
                )
            except (ClipUploadError, ValueError) as exc:
                raise _meeting_http_error(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "audio.unsupported_type",
                    str(exc),
                    stage="upload",
                    retryable=False,
                ) from exc

            try:
                staged = await audio_storage.stage_upload(
                    actor_id,
                    audio,
                )
            except ValueError as exc:
                raise _meeting_http_error(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    "audio.unsupported_type",
                    str(exc),
                    stage="upload",
                    retryable=False,
                ) from exc
            except ClipUploadError as exc:
                message = str(exc)
                too_large = (
                    "上限" in message
                    or "size" in message.lower()
                    or "too large" in message.lower()
                )
                raise _meeting_http_error(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                    if too_large
                    else status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "audio.staging_failed",
                    str(exc),
                    stage="upload",
                    retryable=False,
                ) from exc
            except OSError as exc:
                raise _meeting_http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "audio.staging_failed",
                    str(exc),
                    stage="upload",
                    retryable=False,
                ) from exc

        assert staged is not None

        if staged.size_bytes <= 0:
            await cleanup_upload(
                actor_id,
                staged.upload_id,
            )
            _http_error(
                422,
                code="audio.empty",
                message="Audio file is empty",
                stage="upload",
                retryable=False,
            )

        request_sha256 = _request_fingerprint(
            request_model,
            staged.sha256,
        )

        session = None
        committed = False
        try:
            if db_manager is None:
                raise RuntimeError(
                    "database manager unavailable"
                )
            session = await db_manager.get_session()

            existing = await session.scalar(
                select(MeetingProcessingJob).where(
                    MeetingProcessingJob.actor_user_id
                    == actor_id,
                    MeetingProcessingJob.idempotency_key
                    == idempotency_key,
                )
            )

            if existing is not None:
                await cleanup_upload(
                    actor_id,
                    staged.upload_id,
                )
                if (
                    existing.request_sha256
                    != request_sha256
                ):
                    _http_error(
                        409,
                        code="idempotency.conflict",
                        message=(
                            "Idempotency-Key was already "
                            "used with a different payload"
                        ),
                        stage="request",
                        retryable=False,
                        details={
                            "job_id": str(existing.id),
                        },
                    )

                response = _serialize_job(existing)
                return JSONResponse(
                    status_code=202,
                    content=response.model_dump(
                        mode="json"
                    ),
                )

            now = datetime.utcnow()
            job = MeetingProcessingJob(
                id=uuid4(),
                actor_user_id=actor_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                request_json=request_model.model_dump(
                    mode="json"
                ),
                audio_upload_id=UUID(
                    staged.upload_id
                ),
                audio_file_name=staged.file_name,
                audio_mime_type=staged.mime_type,
                audio_size_bytes=staged.size_bytes,
                audio_sha256=staged.sha256,
                status="queued",
                stage="queued",
                retry_generation=0,
                attempt_count=0,
                retryable=True,
                result_json={},
                error_json={},
                created_at=now,
                updated_at=now,
            )

            session.add(job)

            try:
                await session.commit()
                committed = True
                await session.refresh(job)
            except IntegrityError:
                await session.rollback()

                raced = await session.scalar(
                    select(
                        MeetingProcessingJob
                    ).where(
                        MeetingProcessingJob.actor_user_id
                        == actor_id,
                        MeetingProcessingJob.idempotency_key
                        == idempotency_key,
                    )
                )

                await cleanup_upload(
                    actor_id,
                    staged.upload_id,
                )

                if raced is None:
                    raise

                if (
                    raced.request_sha256
                    != request_sha256
                ):
                    _http_error(
                        409,
                        code="idempotency.conflict",
                        message=(
                            "Idempotency-Key was already "
                            "used with a different payload"
                        ),
                        stage="request",
                        retryable=False,
                        details={
                            "job_id": str(raced.id),
                        },
                    )

                response = _serialize_job(raced)
                return JSONResponse(
                    status_code=202,
                    content=response.model_dump(
                        mode="json"
                    ),
                )

            response = _serialize_job(job)
            return JSONResponse(
                status_code=202,
                content=response.model_dump(
                    mode="json"
                ),
            )

        except HTTPException:
            raise
        except Exception:
            if not committed:
                await cleanup_upload(
                    actor_id,
                    staged.upload_id,
                )
            if session is not None:
                try:
                    await session.rollback()
                except Exception:
                    pass
            _http_error(
                503,
                code="dependency.database_unavailable",
                message="Meeting job could not be persisted",
                stage="storage",
                retryable=True,
            )
        finally:
            if session is not None:
                await session.close()

    @router.get(
        "/jobs/{job_id}",
        response_model=MeetingProcessingJobResponse,
        operation_id="meetingProcessingGetJob",
        tags=["meeting-processing"],
        responses={
            401: {"model": MeetingProcessingErrorEnvelope},
            404: {"model": MeetingProcessingErrorEnvelope},
            426: {"model": MeetingProcessingErrorEnvelope},
        },
    )
    async def get_job(
        request: Request,
        job_id: str,
        _openapi_contract_version: ContractVersionSchemaHeader = None,
        _openapi_security: AoiTalkBearerSchema = None,
    ):
        _require_contract_version(request)
        actor_id = await resolve_actor(request)

        try:
            parsed_job_id = UUID(job_id)
        except ValueError:
            _http_error(
                404,
                code="job.not_found",
                message="Meeting job was not found",
                stage="request",
                retryable=False,
            )

        session = None
        try:
            session = await db_manager.get_session()
            row = await session.scalar(
                select(MeetingProcessingJob).where(
                    MeetingProcessingJob.id
                    == parsed_job_id,
                    MeetingProcessingJob.actor_user_id
                    == actor_id,
                )
            )

            if row is None:
                _http_error(
                    404,
                    code="job.not_found",
                    message="Meeting job was not found",
                    stage="request",
                    retryable=False,
                )

            response = _serialize_job(row)
            return JSONResponse(
                status_code=200,
                content=response.model_dump(
                    mode="json"
                ),
            )
        finally:
            if session is not None:
                await session.close()

    @router.post(
        "/jobs/{job_id}/retry",
        response_model=MeetingProcessingJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="meetingProcessingRetryJob",
        tags=["meeting-processing"],
        responses={
            401: {"model": MeetingProcessingErrorEnvelope},
            404: {"model": MeetingProcessingErrorEnvelope},
            409: {"model": MeetingProcessingErrorEnvelope},
            426: {"model": MeetingProcessingErrorEnvelope},
        },
    )
    async def retry_job(
        request: Request,
        job_id: str,
        _openapi_contract_version: ContractVersionSchemaHeader = None,
        _openapi_security: AoiTalkBearerSchema = None,
    ):
        _require_contract_version(request)
        actor_id = await resolve_actor(request)
        await require_ready(request)

        try:
            parsed_job_id = UUID(job_id)
        except ValueError:
            _http_error(
                404,
                code="job.not_found",
                message="Meeting job was not found",
                stage="request",
                retryable=False,
            )

        session = None
        try:
            session = await db_manager.get_session()
            row = await session.scalar(
                select(MeetingProcessingJob).where(
                    MeetingProcessingJob.id
                    == parsed_job_id,
                    MeetingProcessingJob.actor_user_id
                    == actor_id,
                )
            )

            if row is None:
                _http_error(
                    404,
                    code="job.not_found",
                    message="Meeting job was not found",
                    stage="request",
                    retryable=False,
                )

            if row.status in {"queued", "running"}:
                response = _job_response(row)
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=response.model_dump(mode="json"),
                )

            if row.status == "succeeded":
                _http_error(
                    status.HTTP_409_CONFLICT,
                    code="retry.job_already_succeeded",
                    message=(
                        "succeeded meeting processing jobs cannot be retried"
                    ),
                    stage=str(row.stage or "complete"),
                    retryable=False,
                )

            if row.status != "failed":
                _http_error(
                    status.HTTP_409_CONFLICT,
                    code="retry.invalid_state",
                    message=(
                        "meeting processing job is not in a retryable state"
                    ),
                    stage=str(row.stage or "queued"),
                    retryable=False,
                )

            if not row.retryable:
                current_error = row.error_json or {}
                _http_error(
                    status.HTTP_409_CONFLICT,
                    code=str(
                        current_error.get("code") or "retry.not_retryable"
                    ),
                    message=str(
                        current_error.get("message")
                        or "meeting processing job is not retryable"
                    ),
                    stage=str(row.stage or "queued"),
                    retryable=False,
                    details=dict(current_error.get("details") or {}),
                )

            expected_generation = int(row.retry_generation or 0)

            # Durable audio must still match the immutable job snapshot before
            # a failed generation can become queued again.
            try:
                resolved = audio_storage.resolve_upload(
                    actor_id,
                    row.audio_upload_id,
                )
                if (
                    str(resolved.upload_id) != str(row.audio_upload_id)
                    or resolved.file_name != row.audio_file_name
                    or int(resolved.size_bytes) != int(row.audio_size_bytes)
                    or resolved.sha256 != row.audio_sha256
                ):
                    raise ClipUploadError(
                        "staged meeting audio does not match durable job metadata"
                    )
            except (ClipUploadError, ValueError, OSError) as exc:
                outcome = await mark_audio_integrity_failed_cas(
                    session,
                    row,
                    message="Staged audio failed integrity validation",
                    details={"type": type(exc).__name__},
                )
                if not outcome.won:
                    loser_response = _retry_cas_loss_response(
                        outcome,
                        expected_generation=expected_generation,
                    )
                    return JSONResponse(
                        status_code=status.HTTP_202_ACCEPTED,
                        content=loser_response.model_dump(mode="json"),
                    )

                _http_error(
                    status.HTTP_409_CONFLICT,
                    code="audio.integrity_failed",
                    message="Staged audio failed integrity validation",
                    stage=str(
                        outcome.job.stage
                        if outcome.job is not None
                        else row.stage
                    ),
                    retryable=False,
                )

            outcome = await requeue_failed_job_cas(session, row)
            if outcome.won:
                assert outcome.job is not None
                response = _job_response(outcome.job)
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=response.model_dump(mode="json"),
                )

            loser_response = _retry_cas_loss_response(
                outcome,
                expected_generation=expected_generation,
            )
            # A CAS loser that observed queued/running is an idempotent 202;
            # conflict outcomes raise from _retry_cas_loss_response.
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=loser_response.model_dump(mode="json"),
            )
        finally:
            if session is not None:
                await session.close()

    return router
