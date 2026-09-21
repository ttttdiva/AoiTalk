"""Authenticated HTTP boundary for the trusted Engagement Operations kernel.

The operations service owns authorization, optimistic version checks, hashes and
state transitions.  This module intentionally contains only request parsing,
session/actor plumbing and HTTP error translation; in particular it never
performs a provider call or makes a human decision on behalf of a caller.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
from collections.abc import Mapping
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError

from ..services import operations_service as service
from ..services.operations_command_center import OperationsCommandCenterService


# ---------------------------------------------------------------------------
# Pydantic wire models
# ---------------------------------------------------------------------------


class _OperationsModel(BaseModel):
    """Strict command model; undeclared input must never widen authority."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _OperationsResponseModel(BaseModel):
    """Permissive safe projection model for additive response metadata."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ConnectionCreateRequest(_OperationsModel):
    provider_key: str = Field(..., min_length=1, max_length=120)
    display_name: str = Field(..., min_length=1, max_length=255)
    remote_account_ref: str | None = Field(default=None, max_length=255)
    auth_status: str = Field(default="unknown", max_length=32)
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectionUpdateRequest(_OperationsModel):
    expected_version: int = Field(..., ge=1)
    provider_key: str | None = Field(default=None, min_length=1, max_length=120)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    remote_account_ref: str | None = Field(default=None, max_length=255)
    auth_status: str | None = Field(default=None, min_length=1, max_length=32)
    metadata: dict[str, Any] | None = None


class ConnectionCollectionUpdateRequest(ConnectionUpdateRequest):
    """Compatibility body for clients that PATCH the collection endpoint."""

    connection_id: str


class OpportunityCreateRequest(_OperationsModel):
    # ``title`` is optional at the UI edge; the route derives a stable title
    # from the source URL when omitted before calling the service.
    title: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=4000)
    source_text: str | None = Field(default=None, max_length=1_000_000)
    connection_id: str | None = None
    project_id: str | None = None
    status: str = Field(default="open", min_length=1, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationCreateRequest(_OperationsModel):
    estimated_effort_hours: float | None = None
    estimated_cost: float | None = None
    estimated_revenue: float | None = None
    fit: str | None = Field(default=None, max_length=32)
    risks: list[Any] = Field(default_factory=list, max_length=100)
    missing_requirements: list[Any] = Field(default_factory=list, max_length=100)
    summary: str | None = Field(default=None, max_length=32_000)
    evidence_refs: list[Any] = Field(default_factory=list, max_length=100)


class DraftCreateRequest(_OperationsModel):
    message: str = Field(..., min_length=1, max_length=64_000)
    offered_price: float | None = None
    currency: str | None = Field(default=None, max_length=16)
    delivery_estimate: str | None = Field(default=None, max_length=255)
    artifact_version_ids: list[str] = Field(default_factory=list, max_length=100)


class ArtifactCreateRequest(_OperationsModel):
    project_id: str | None = None
    opportunity_id: str | None = None
    filename: str | None = Field(default=None, max_length=512)
    # ``label`` is the multipart/UI alias for filename.
    label: str | None = Field(default=None, max_length=512)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    content_base64: str | None = Field(default=None, max_length=35_000_000)
    # Text content is accepted for JSON callers; binary callers should use
    # content_base64 or multipart ``file``.
    content: str | None = Field(default=None, max_length=25 * 1024 * 1024)
    provenance: dict[str, Any] = Field(default_factory=dict)


_ARTIFACT_REQUEST_BODY_OPENAPI = {
    "required": True,
    "content": {
        "application/json": {"schema": ArtifactCreateRequest.model_json_schema()},
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file"],
                "properties": {
                    "project_id": {"type": ["string", "null"]},
                    "opportunity_id": {"type": ["string", "null"]},
                    "filename": {"type": ["string", "null"], "maxLength": 512},
                    "label": {"type": ["string", "null"], "maxLength": 512},
                    "mime_type": {"type": ["string", "null"], "maxLength": 255},
                    "provenance": {"type": ["string", "null"]},
                    "file": {"type": "string", "format": "binary"},
                },
            }
        },
    },
}


class ActionCreateRequest(_OperationsModel):
    connection_id: str
    application_draft_id: str
    idempotency_key: str | None = Field(default=None, max_length=255)
    origin_agent_id: str | None = None
    origin_agent_run_id: str | None = None
    origin_work_item_id: str | None = None


class MediaActionCreateRequest(_OperationsModel):
    """Strict wire model for a typed MediaOps manual action proposal."""

    action_type: str | None = Field(default=None, max_length=64)
    # ``operation_key`` is the descriptive alias used by MediaOps clients;
    # the service persists the canonical ``action_type`` value.
    operation_key: str | None = Field(default=None, max_length=64)
    platform: Literal["x", "pixiv", "dlsite", "patreon", "youtube", "instagram"]
    content_item_id: str
    content_variant_id: str
    content_variant_revision_id: str
    persona_revision_id: str
    connection_id: str
    platform_account_id: str | None = None
    platform_account_revision_id: str | None = None
    project_id: str | None = None
    content_item_hash: str | None = Field(default=None, max_length=64)
    content_variant_hash: str | None = Field(default=None, max_length=64)
    content_variant_revision_hash: str | None = Field(default=None, max_length=64)
    variant_revision_hash: str | None = Field(default=None, max_length=64)
    content_variant_revision_version: int | None = Field(default=None, ge=1)
    variant_revision_version: int | None = Field(default=None, ge=1)
    persona_revision_hash: str | None = Field(default=None, max_length=64)
    platform_account_revision_hash: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any]
    payload_hash: str | None = Field(default=None, max_length=64)
    artifact_hashes: list[str] = Field(default_factory=list, max_length=100)
    qa: dict[str, Any] = Field(default_factory=dict)
    rights: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    adapter_target: dict[str, Any] = Field(default_factory=dict)
    execution_mode: Literal["manual", "provider"] = "manual"
    capability_snapshot_id: str | None = None
    capability_snapshot_hash: str | None = Field(default=None, max_length=64)
    adapter_key: str | None = Field(default=None, max_length=128)
    adapter_version: str | None = Field(default=None, max_length=32)
    credential_state_hash: str | None = Field(default=None, max_length=64)
    execution_key: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=255)
    origin_agent_id: str | None = None
    origin_agent_run_id: str | None = None
    origin_work_item_id: str | None = None

    @model_validator(mode="after")
    def require_action_type(self) -> "MediaActionCreateRequest":
        value = self.action_type or self.operation_key
        if not value:
            raise ValueError("action_type or operation_key is required")
        if self.action_type and self.operation_key and self.action_type != self.operation_key:
            raise ValueError("action_type and operation_key must match")
        self.action_type = value
        return self


class MediaActionRevisionRequest(_OperationsModel):
    """Exact new ContentVariantRevision binding for a media action."""

    expected_version: int = Field(..., ge=1)
    content_variant_revision_id: str
    persona_revision_id: str | None = None
    platform_account_revision_id: str | None = None
    content_item_hash: str | None = Field(default=None, max_length=64)
    content_variant_hash: str | None = Field(default=None, max_length=64)
    content_variant_revision_hash: str | None = Field(default=None, max_length=64)
    variant_revision_hash: str | None = Field(default=None, max_length=64)
    content_variant_revision_version: int | None = Field(default=None, ge=1)
    variant_revision_version: int | None = Field(default=None, ge=1)
    persona_revision_hash: str | None = Field(default=None, max_length=64)
    platform_account_revision_hash: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any]
    payload_hash: str | None = Field(default=None, max_length=64)
    artifact_hashes: list[str] | None = Field(default=None, max_length=100)
    qa: dict[str, Any] | None = None
    rights: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    adapter_target: dict[str, Any] | None = None
    execution_mode: Literal["manual", "provider"] | None = None
    capability_snapshot_id: str | None = None
    capability_snapshot_hash: str | None = Field(default=None, max_length=64)
    adapter_key: str | None = Field(default=None, max_length=128)
    adapter_version: str | None = Field(default=None, max_length=32)
    credential_state_hash: str | None = Field(default=None, max_length=64)
    execution_key: str | None = Field(default=None, max_length=255)


class ActionReviseRequest(_OperationsModel):
    application_draft_id: str
    expected_version: int = Field(..., ge=1)


class ActionDecisionRequest(_OperationsModel):
    expected_version: int = Field(..., ge=1)
    action_version: int | None = Field(default=None, ge=1)
    payload_hash: str | None = Field(default=None, max_length=64)
    artifact_hashes: list[str] | None = None
    reason: str | None = Field(default=None, max_length=4000)


class AttemptCreateRequest(_OperationsModel):
    expected_version: int = Field(..., ge=1)
    provider_attempt_ref: str | None = Field(default=None, max_length=255)
    executor_type: Literal["manual", "provider"] = "manual"
    execution_mode: Literal["manual", "provider"] | None = None
    execution_key: str | None = Field(default=None, max_length=255)


class AttemptCompleteRequest(_OperationsModel):
    expected_version: int = Field(..., ge=1)
    status: str | None = None
    # Friendly UI alias accepted in addition to the canonical service field.
    outcome: str | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    provider_receipt_ref: str | None = Field(default=None, max_length=255)
    remote_resource_id: str | None = Field(default=None, max_length=255)
    remote_url: str | None = Field(default=None, max_length=4000)
    remote_status: str | None = Field(default=None, max_length=64)
    provider_observed_at: str | None = None
    evidence_note: str | None = Field(default=None, max_length=8000)
    confirmation_level: str | None = Field(default=None, max_length=32)
    error_message: str | None = Field(default=None, max_length=4000)
    result_summary: str | None = Field(default=None, max_length=4000)


class ReconcileRequest(_OperationsModel):
    expected_version: int = Field(..., ge=1)
    outcome: str | None = None
    # ``resolution`` is the UI alias for outcome.
    resolution: str | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    provider_receipt_ref: str | None = Field(default=None, max_length=255)
    remote_resource_id: str | None = Field(default=None, max_length=255)
    remote_url: str | None = Field(default=None, max_length=4000)
    remote_status: str | None = Field(default=None, max_length=64)
    provider_observed_at: str | None = None
    evidence_note: str | None = Field(default=None, max_length=8000)
    confirmation_level: str | None = Field(default=None, max_length=32)
    result_summary: str | None = Field(default=None, max_length=8000)
    reason: str | None = Field(default=None, max_length=4000)


# Responses remain permissive so safe projections can grow without breaking
# clients.  The service's ``to_safe_dict`` methods are the source of truth for
# redaction (credentials, raw source text and absolute storage paths).
class ConnectionResponse(_OperationsResponseModel):
    id: str
    provider_key: str
    display_name: str
    remote_account_ref: str | None = None
    auth_status: str | None = None
    project_id: str | None = None
    version: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ArtifactResponse(_OperationsResponseModel):
    id: str
    owner_user_id: str | None = None
    project_id: str | None = None
    filename: str | None = None
    sha256: str
    size_bytes: int
    mime_type: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    created_at: str | None = None


class OpportunityResponse(_OperationsResponseModel):
    id: str
    owner_user_id: str | None = None
    project_id: str | None = None
    connection_id: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_text: str | None = None
    source_snapshot_hash: str | None = None
    source_snapshot: dict[str, Any] | None = None
    source_untrusted: bool | None = None
    status: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class OpportunityDetailResponse(OpportunityResponse):
    """Authorized detail projection, including immutable analysis versions."""

    evaluations: list[dict[str, Any]] = Field(default_factory=list)
    drafts: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationResponse(_OperationsResponseModel):
    id: str
    opportunity_id: str | None = None
    owner_user_id: str | None = None
    project_id: str | None = None
    version: int | None = None
    estimated_effort_hours: float | None = None
    estimated_cost: float | None = None
    estimated_revenue: float | None = None
    fit: str | None = None
    risks: list[Any] = Field(default_factory=list)
    missing_requirements: list[Any] = Field(default_factory=list)
    summary: str | None = None
    evidence_refs: list[Any] = Field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None


class DraftResponse(_OperationsResponseModel):
    id: str
    opportunity_id: str | None = None
    owner_user_id: str | None = None
    project_id: str | None = None
    version: int | None = None
    message: str | None = None
    offered_price: float | None = None
    currency: str | None = None
    delivery_estimate: str | None = None
    artifact_version_ids: list[str] = Field(default_factory=list)
    draft_hash: str | None = None
    created_by: str | None = None
    created_at: str | None = None


class AttemptResponse(_OperationsResponseModel):
    id: str
    action_id: str | None = None
    status: str | None = None
    action_version: int | None = None
    executor_type: str | None = None
    execution_mode: str | None = None
    provider_key: str | None = None
    provider_adapter_key: str | None = None
    provider_adapter_version: str | None = None
    capability_snapshot_id: str | None = None
    capability_snapshot_hash: str | None = None
    execution_key: str | None = None
    provider_attempt_ref: str | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    result_summary: str | None = None
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    ended_at: str | None = None
    created_by: str | None = None


class ActionResponse(_OperationsResponseModel):
    id: str
    owner_user_id: str | None = None
    project_id: str | None = None
    opportunity_id: str | None = None
    source_url: str | None = None
    source_snapshot_hash: str | None = None
    connection_id: str | None = None
    application_draft_id: str | None = None
    content_item_id: str | None = None
    content_variant_id: str | None = None
    content_variant_revision_id: str | None = None
    persona_revision_id: str | None = None
    platform_account_id: str | None = None
    platform_account_revision_id: str | None = None
    platform: str | None = None
    execution_mode: str | None = None
    capability_snapshot_id: str | None = None
    capability_snapshot_hash: str | None = None
    adapter_key: str | None = None
    adapter_version: str | None = None
    execution_key: str | None = None
    action_type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    action_version: int | None = None
    version: int | None = None
    status: str | None = None
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    history: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    origin_agent_id: str | None = None
    origin_agent_run_id: str | None = None
    origin_work_item_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ActionListItemResponse(_OperationsResponseModel):
    """Bounded collection projection; exact payload is detail-only."""

    id: str
    owner_user_id: str | None = None
    project_id: str | None = None
    opportunity_id: str | None = None
    connection_id: str | None = None
    application_draft_id: str | None = None
    content_item_id: str | None = None
    content_variant_id: str | None = None
    content_variant_revision_id: str | None = None
    persona_revision_id: str | None = None
    platform_account_id: str | None = None
    platform_account_revision_id: str | None = None
    platform: str | None = None
    execution_mode: str | None = None
    capability_snapshot_id: str | None = None
    capability_snapshot_hash: str | None = None
    adapter_key: str | None = None
    adapter_version: str | None = None
    execution_key: str | None = None
    action_type: str | None = None
    payload_hash: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    action_version: int | None = None
    version: int | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    origin_agent_id: str | None = None
    origin_agent_run_id: str | None = None
    origin_work_item_id: str | None = None


class MediaAdapterStatusResponse(_OperationsResponseModel):
    schema_version: str
    provider_calls: bool = False
    platforms: dict[str, dict[str, Any]] = Field(default_factory=dict)


class EventResponse(_OperationsResponseModel):
    id: str
    entity_type: str
    entity_id: str
    event_type: str
    actor_id: str | None = None
    actor_type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


# List response models are intentionally direct arrays on the wire.  The
# frontend normalizer also accepts wrapped forms for compatibility with older
# deployments.
ConnectionListResponse = list[ConnectionResponse]
OpportunityListResponse = list[OpportunityResponse]
ActionListResponse = list[ActionListItemResponse]
EventListResponse = list[EventResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024
_ARTIFACT_JSON_MAX_BYTES = 36 * 1024 * 1024


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _raise_http_error(exc: Exception) -> None:
    """Translate known service boundary errors without leaking internals."""

    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, service.OperationsError):
        raise HTTPException(
            status_code=int(getattr(exc, "status_code", 400) or 400),
            detail=str(exc),
        ) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(status_code=409, detail="operation conflicts with an existing record") from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc) or "operation access denied") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


async def _invoke(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await _maybe_await(method(*args, **kwargs))
    except Exception as exc:  # service errors are deliberately mapped here
        _raise_http_error(exc)
        raise AssertionError("unreachable")


async def _get_session(get_db_manager: Callable[[], Any]) -> Any:
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    manager = await _maybe_await(manager)
    if manager is None or not callable(getattr(manager, "get_session", None)):
        raise HTTPException(status_code=503, detail="database is unavailable")
    try:
        session = await _maybe_await(manager.get_session())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    if session is None:
        raise HTTPException(status_code=503, detail="database is unavailable")
    return session


async def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception:
            # Do not replace a successful response or the original service
            # error with a best-effort connection cleanup failure.
            return


async def _actor(get_user_from_request: Callable[..., Any], request: Request) -> Any:
    try:
        user = await _maybe_await(get_user_from_request(request))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Not authenticated") from exc
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _principal_field(principal: Any, name: str, default: Any = None) -> Any:
    if isinstance(principal, Mapping):
        return principal.get(name, default)
    return getattr(principal, name, default)


_PRINCIPAL_AUTHORITY_ABSENT = object()


def _authenticated_human_actor(user: Any) -> dict[str, Any]:
    """Stamp human authority only for a normal authenticated user with no marker."""

    raw_actor_type = _principal_field(user, "actor_type", _PRINCIPAL_AUTHORITY_ABSENT)
    is_agent = bool(_principal_field(user, "is_agent", False))
    if is_agent:
        actor_type: Any = "agent"
    elif raw_actor_type is _PRINCIPAL_AUTHORITY_ABSENT:
        actor_type = (
            "human"
            if _principal_field(user, "_authority_source") == "web_session"
            else "unknown"
        )
    else:
        # Preserve explicit classifications, including invalid/empty values.
        # OperationsService owns the fail-closed classification and rejection.
        actor_type = raw_actor_type

    return {
        "id": _principal_field(user, "id"),
        "user_id": _principal_field(user, "user_id"),
        "role": _principal_field(user, "role", ""),
        "actor_type": actor_type,
        "is_agent": is_agent,
    }


async def _with_session(
    get_db_manager: Callable[[], Any],
    callback: Callable[[Any], Any],
) -> Any:
    session = await _get_session(get_db_manager)
    try:
        return await _maybe_await(callback(session))
    finally:
        await _close_session(session)


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"{field} must be a JSON object") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise HTTPException(status_code=422, detail=f"{field} must be a JSON object")


def _artifact_content_from_json(payload: ArtifactCreateRequest) -> bytes | None:
    if payload.content_base64 is not None:
        try:
            return base64.b64decode(payload.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="content_base64 is invalid") from exc
    if payload.content is not None:
        return payload.content.encode("utf-8")
    return None


async def _bounded_json(request: Request, *, max_bytes: int) -> Any:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(status_code=413, detail="artifact payload is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="artifact payload is too large")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid JSON artifact payload") from exc


def _validate_model(model_type: type[BaseModel], value: Any) -> BaseModel:
    """Validate manually parsed multipart/JSON payloads like FastAPI bodies."""

    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


# ---------------------------------------------------------------------------
# Router factory and endpoints
# ---------------------------------------------------------------------------


def create_operations_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
    *,
    action_registry: Any = None,
) -> APIRouter:
    """Build the authenticated Operations API router."""

    router = APIRouter(prefix="/api/operations", tags=["operations"])
    operations = service.OperationsService()
    command_center = OperationsCommandCenterService(action_registry=action_registry)

    async def current_actor(request: Request) -> Any:
        user = await _actor(get_user_from_request, request)
        return _authenticated_human_actor(user)

    def protected(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        # Kept as documentation for callers that introspect this module; each
        # endpoint declares Depends explicitly so FastAPI exposes auth in the
        # generated contract.
        return endpoint

    @router.get(
        "/command-center",
        operation_id="operations_get_command_center",
    )
    @protected
    async def get_command_center(
        request: Request,
        space_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        domain: str | None = Query(default=None, max_length=64),
        state: str | None = Query(default=None, max_length=32),
        from_: str | None = Query(default=None, alias="from"),
        to_: str | None = Query(default=None, alias="to"),
        limit: int = Query(default=100, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        for value, label in (
            (space_id, "space_id"),
            (project_id, "project_id"),
            (agent_id, "agent_id"),
        ):
            if value:
                try:
                    UUID(str(value))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422, detail=f"{label} is invalid")
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                command_center.snapshot,
                session,
                actor,
                space_id=space_id,
                project_id=project_id,
                agent_id=agent_id,
                domain=domain,
                state=state,
                from_at=from_,
                to_at=to_,
                limit=limit,
            ),
        )

    async def media_call(method_name: str, actor: Any, *args: Any, **kwargs: Any) -> Any:
        """Invoke one MediaOps action method through the shared DB boundary."""

        method = getattr(operations, method_name, None)
        if not callable(method):
            raise HTTPException(status_code=503, detail="MediaOps action service is unavailable")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(method, session, actor, *args, **kwargs),
        )

    @router.get(
        "/media/adapter-status",
        response_model=MediaAdapterStatusResponse,
        operation_id="media_operations_adapter_status",
    )
    @protected
    async def media_adapter_status(
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call("get_media_adapter_status", actor)

    # Compatibility path for clients that group adapter readiness under
    # ``/adapters`` rather than the singular status resource.
    @router.get(
        "/media/adapters/status",
        response_model=MediaAdapterStatusResponse,
        operation_id="media_operations_adapters_status",
        include_in_schema=False,
    )
    @protected
    async def media_adapters_status(
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call("get_media_adapter_status", actor)

    @router.get(
        "/media/actions",
        response_model=ActionListResponse,
        operation_id="media_operations_list_actions",
    )
    @protected
    async def list_media_actions(
        request: Request,
        project_id: str | None = None,
        action_type: str | None = Query(default=None, max_length=64),
        platform: str | None = Query(default=None, max_length=16),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call(
            "list_media_actions",
            actor,
            project_id=project_id,
            action_type=action_type,
            platform=platform,
            status=status,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/media/actions",
        response_model=ActionResponse,
        operation_id="media_operations_propose_action",
    )
    @protected
    async def create_media_action(
        payload: MediaActionCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(exclude_unset=True)
        action_type = data.pop("action_type", None) or data.pop("operation_key", None)
        idempotency_key = data.pop("idempotency_key", None) or request.headers.get("idempotency-key")
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        return await media_call(
            "create_media_action",
            actor,
            action_type=action_type,
            idempotency_key=idempotency_key,
            **data,
        )

    # ``proposals`` is retained as a non-schema alias for early MediaOps UI
    # builds; both paths use the exact same idempotency/service contract.
    @router.post(
        "/media/proposals",
        response_model=ActionResponse,
        operation_id="media_operations_propose_action_alias",
        include_in_schema=False,
    )
    @protected
    async def create_media_action_alias(
        payload: MediaActionCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        return await create_media_action(payload, request, _)

    @router.get(
        "/media/actions/{action_id}",
        response_model=ActionResponse,
        operation_id="media_operations_get_action",
    )
    @protected
    async def get_media_action(
        action_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call("get_action", actor, action_id)

    @router.post(
        "/media/actions/{action_id}/revise",
        response_model=ActionResponse,
        operation_id="media_operations_revise_action",
    )
    @protected
    async def revise_media_action(
        action_id: str,
        payload: MediaActionRevisionRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(exclude_unset=True)
        # A revision is itself optimistic/versioned; accepting an idempotency
        # header is harmless for old clients but it is not used as authority.
        data.pop("idempotency_key", None)
        return await media_call("revise_media_action", actor, action_id, **data)

    @router.post(
        "/media/actions/{action_id}/approve",
        response_model=ActionResponse,
        operation_id="media_operations_approve_action",
    )
    @protected
    async def approve_media_action(
        action_id: str,
        payload: ActionDecisionRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call(
            "approve_action",
            actor,
            action_id,
            expected_version=payload.expected_version,
            action_version=payload.action_version,
            payload_hash=payload.payload_hash,
            artifact_hashes=payload.artifact_hashes,
            reason=payload.reason,
        )

    @router.post(
        "/media/actions/{action_id}/reject",
        response_model=ActionResponse,
        operation_id="media_operations_reject_action",
    )
    @protected
    async def reject_media_action(
        action_id: str,
        payload: ActionDecisionRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call(
            "reject_action",
            actor,
            action_id,
            expected_version=payload.expected_version,
            action_version=payload.action_version,
            payload_hash=payload.payload_hash,
            artifact_hashes=payload.artifact_hashes,
            reason=payload.reason,
        )

    @router.post(
        "/media/actions/{action_id}/attempts",
        response_model=AttemptResponse,
        operation_id="media_operations_execute_action",
    )
    @protected
    async def execute_media_action(
        action_id: str,
        payload: AttemptCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await media_call(
            "create_attempt",
            actor,
            action_id,
            expected_version=payload.expected_version,
            provider_attempt_ref=payload.provider_attempt_ref,
            executor_type=payload.executor_type,
            execution_mode=payload.execution_mode,
            execution_key=payload.execution_key,
        )

    @router.post(
        "/media/actions/{action_id}/execute",
        response_model=AttemptResponse,
        operation_id="media_operations_execute_action_alias",
        include_in_schema=False,
    )
    @protected
    async def execute_media_action_alias(
        action_id: str,
        payload: AttemptCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        return await execute_media_action(action_id, payload, request, _)

    @router.post(
        "/media/actions/{action_id}/attempts/{attempt_id}/complete",
        response_model=ActionResponse,
        operation_id="media_operations_complete_attempt",
    )
    @protected
    async def complete_media_attempt(
        action_id: str,
        attempt_id: str,
        payload: AttemptCompleteRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        status_value = payload.status or payload.outcome
        if not status_value:
            raise HTTPException(status_code=422, detail="status is required")
        return await media_call(
            "complete_attempt",
            actor,
            action_id,
            attempt_id,
            status=status_value,
            expected_version=payload.expected_version,
            evidence_artifact_ids=payload.evidence_artifact_ids,
            provider_receipt_ref=payload.provider_receipt_ref,
            result_summary=payload.result_summary,
            remote_resource_id=payload.remote_resource_id,
            remote_url=payload.remote_url,
            remote_status=payload.remote_status,
            provider_observed_at=payload.provider_observed_at,
            evidence_note=payload.evidence_note,
            confirmation_level=payload.confirmation_level,
            error_message=payload.error_message,
        )

    @router.post(
        "/media/actions/{action_id}/reconcile",
        response_model=ActionResponse,
        operation_id="media_operations_reconcile_action",
    )
    @protected
    async def reconcile_media_action(
        action_id: str,
        payload: ReconcileRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        outcome = payload.outcome or payload.resolution
        if not outcome:
            raise HTTPException(status_code=422, detail="outcome is required")
        return await media_call(
            "reconcile_action",
            actor,
            action_id,
            outcome=outcome,
            expected_version=payload.expected_version,
            evidence_artifact_ids=payload.evidence_artifact_ids,
            provider_receipt_ref=payload.provider_receipt_ref,
            result_summary=payload.result_summary,
            remote_resource_id=payload.remote_resource_id,
            remote_url=payload.remote_url,
            remote_status=payload.remote_status,
            provider_observed_at=payload.provider_observed_at,
            evidence_note=payload.evidence_note,
            confirmation_level=payload.confirmation_level,
            reason=payload.reason,
        )

    @router.get("/connections", response_model=ConnectionListResponse)
    @protected
    async def list_connections(
        request: Request,
        project_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.list_connections,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post("/connections", response_model=ConnectionResponse)
    @protected
    async def create_connection(
        payload: ConnectionCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_connection,
                session,
                actor,
                **payload.model_dump(exclude_unset=True),
            ),
        )

    @router.patch("/connections", response_model=ConnectionResponse)
    @protected
    async def update_connection_collection(
        payload: ConnectionCollectionUpdateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(exclude_unset=True)
        connection_id = data.pop("connection_id")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.update_connection,
                session,
                actor,
                connection_id,
                **data,
            ),
        )

    @router.patch("/connections/{connection_id}", response_model=ConnectionResponse)
    @protected
    async def update_connection(
        connection_id: str,
        payload: ConnectionUpdateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.update_connection,
                session,
                actor,
                connection_id,
                **payload.model_dump(exclude_unset=True),
            ),
        )

    @router.post(
        "/artifacts",
        response_model=ArtifactResponse,
        openapi_extra={"requestBody": _ARTIFACT_REQUEST_BODY_OPENAPI},
    )
    @protected
    async def create_artifact(
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        content_type = request.headers.get("content-type", "").lower()
        upload: Any = None
        if content_type.startswith("multipart/"):
            declared_length = request.headers.get("content-length")
            if declared_length is None:
                raise HTTPException(
                    status_code=411,
                    detail="multipart artifact upload requires content-length",
                )
            try:
                if int(declared_length) > _ARTIFACT_MAX_BYTES + 1024 * 1024:
                    raise HTTPException(status_code=413, detail="artifact payload is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content-length") from exc
            try:
                form = await request.form(max_part_size=_ARTIFACT_MAX_BYTES + 1)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=422, detail="invalid multipart artifact payload") from exc
            raw_values: dict[str, Any] = {str(key): value for key, value in form.items() if str(key) != "file"}
            upload = form.get("file")
            if isinstance(upload, UploadFile) or (
                upload is not None and callable(getattr(upload, "read", None))
            ):
                raw_values.setdefault("filename", getattr(upload, "filename", None))
                raw_values.setdefault("mime_type", getattr(upload, "content_type", None) or "application/octet-stream")
            if "provenance" in raw_values:
                raw_values["provenance"] = _json_object(raw_values["provenance"], field="provenance")
            payload = _validate_model(ArtifactCreateRequest, raw_values)
            if upload is not None and callable(getattr(upload, "read", None)):
                content = await _maybe_await(upload.read(_ARTIFACT_MAX_BYTES + 1))
                if len(content) > _ARTIFACT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="artifact content exceeds 25 MiB")
            else:
                content = _artifact_content_from_json(payload)
        else:
            try:
                raw_json = await _bounded_json(
                    request,
                    max_bytes=_ARTIFACT_JSON_MAX_BYTES,
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=422, detail="invalid JSON artifact payload") from exc
            payload = _validate_model(ArtifactCreateRequest, raw_json)
            content = _artifact_content_from_json(payload)

        project_id = payload.project_id
        # The browser upload form identifies the opportunity rather than a
        # project.  Resolve it through the service's authorization-aware read
        # path before registering the artifact in that project scope.
        if not project_id and payload.opportunity_id:
            detail = await _with_session(
                get_db_manager,
                lambda session: _invoke(
                    operations.get_opportunity,
                    session,
                    actor,
                    payload.opportunity_id,
                    include_versions=False,
                ),
            )
            if isinstance(detail, Mapping):
                project_id = detail.get("project_id")

        filename = payload.filename or payload.label
        # A multipart UploadFile's filename is captured into payload above;
        # preserve a safe fallback for JSON registrations without a filename.
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_artifact,
                session,
                actor,
                content=content,
                filename=filename,
                mime_type=payload.mime_type,
                project_id=project_id,
                provenance=payload.provenance,
            ),
        )

    @router.get("/opportunities", response_model=OpportunityListResponse)
    @protected
    async def list_opportunities(
        request: Request,
        project_id: str | None = None,
        connection_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.list_opportunities,
                session,
                actor,
                project_id=project_id,
                connection_id=connection_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post("/opportunities", response_model=OpportunityResponse)
    @protected
    async def create_opportunity(
        payload: OpportunityCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(exclude_unset=True)
        if not data.get("title") or not str(data.get("title")).strip():
            data["title"] = service.default_opportunity_title(data.get("source_url"))
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(operations.create_opportunity, session, actor, **data),
        )

    @router.get("/opportunities/{opportunity_id}", response_model=OpportunityDetailResponse)
    @protected
    async def get_opportunity(
        opportunity_id: str,
        request: Request,
        include_versions: bool = True,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.get_opportunity,
                session,
                actor,
                opportunity_id,
                include_versions=include_versions,
            ),
        )

    @router.post("/opportunities/{opportunity_id}/evaluations", response_model=EvaluationResponse)
    @protected
    async def create_evaluation(
        opportunity_id: str,
        payload: EvaluationCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_evaluation,
                session,
                actor,
                opportunity_id,
                **payload.model_dump(exclude_unset=True),
            ),
        )

    @router.post("/opportunities/{opportunity_id}/drafts", response_model=DraftResponse)
    @protected
    async def create_draft(
        opportunity_id: str,
        payload: DraftCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_draft,
                session,
                actor,
                opportunity_id,
                **payload.model_dump(exclude_unset=True),
            ),
        )

    @router.get("/actions", response_model=ActionListResponse)
    @protected
    async def list_actions(
        request: Request,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.list_actions,
                session,
                actor,
                project_id=project_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post("/actions", response_model=ActionResponse)
    @protected
    async def create_action(
        payload: ActionCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        key = payload.idempotency_key or request.headers.get("idempotency-key")
        if not key:
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_action,
                session,
                actor,
                connection_id=payload.connection_id,
                application_draft_id=payload.application_draft_id,
                idempotency_key=key,
                payload=None,
                origin_agent_id=payload.origin_agent_id,
                origin_agent_run_id=payload.origin_agent_run_id,
                origin_work_item_id=payload.origin_work_item_id,
            ),
        )

    @router.get("/actions/{action_id}/events", response_model=EventListResponse)
    @router.get("/actions/{action_id}/timeline", response_model=EventListResponse, include_in_schema=False)
    @protected
    async def list_action_events(
        action_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.list_events,
                session,
                actor,
                entity_type="action",
                entity_id=action_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.get("/actions/{action_id}", response_model=ActionResponse)
    @protected
    async def get_action(
        action_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(operations.get_action, session, actor, action_id),
        )

    @router.post("/actions/{action_id}/revise", response_model=ActionResponse)
    @protected
    async def revise_action(
        action_id: str,
        payload: ActionReviseRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.revise_action,
                session,
                actor,
                action_id,
                application_draft_id=payload.application_draft_id,
                expected_version=payload.expected_version,
            ),
        )

    @router.post("/actions/{action_id}/approve", response_model=ActionResponse)
    @protected
    async def approve_action(
        action_id: str,
        payload: ActionDecisionRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.approve_action,
                session,
                actor,
                action_id,
                expected_version=payload.expected_version,
                action_version=payload.action_version,
                payload_hash=payload.payload_hash,
                artifact_hashes=payload.artifact_hashes,
                reason=payload.reason,
            ),
        )

    @router.post("/actions/{action_id}/reject", response_model=ActionResponse)
    @protected
    async def reject_action(
        action_id: str,
        payload: ActionDecisionRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.reject_action,
                session,
                actor,
                action_id,
                expected_version=payload.expected_version,
                action_version=payload.action_version,
                payload_hash=payload.payload_hash,
                artifact_hashes=payload.artifact_hashes,
                reason=payload.reason,
            ),
        )

    @router.post("/actions/{action_id}/attempts", response_model=AttemptResponse)
    @protected
    async def create_attempt(
        action_id: str,
        payload: AttemptCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.create_attempt,
                session,
                actor,
                action_id,
                expected_version=payload.expected_version,
                provider_attempt_ref=payload.provider_attempt_ref,
                executor_type=payload.executor_type,
                execution_mode=payload.execution_mode,
                execution_key=payload.execution_key,
            ),
        )

    @router.post("/actions/{action_id}/attempts/{attempt_id}/complete", response_model=ActionResponse)
    @protected
    async def complete_attempt(
        action_id: str,
        attempt_id: str,
        payload: AttemptCompleteRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        status_value = payload.status or payload.outcome
        if not status_value:
            raise HTTPException(status_code=422, detail="status is required")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.complete_attempt,
                session,
                actor,
                action_id,
                attempt_id,
                status=status_value,
                expected_version=payload.expected_version,
                evidence_artifact_ids=payload.evidence_artifact_ids,
                provider_receipt_ref=payload.provider_receipt_ref,
                result_summary=payload.result_summary,
                remote_resource_id=payload.remote_resource_id,
                remote_url=payload.remote_url,
                remote_status=payload.remote_status,
                provider_observed_at=payload.provider_observed_at,
                evidence_note=payload.evidence_note,
                confirmation_level=payload.confirmation_level,
                error_message=payload.error_message,
            ),
        )

    @router.post("/actions/{action_id}/reconcile", response_model=ActionResponse)
    @protected
    async def reconcile_action(
        action_id: str,
        payload: ReconcileRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        outcome = payload.outcome or payload.resolution
        if not outcome:
            raise HTTPException(status_code=422, detail="outcome is required")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                operations.reconcile_action,
                session,
                actor,
                action_id,
                outcome=outcome,
                expected_version=payload.expected_version,
                evidence_artifact_ids=payload.evidence_artifact_ids,
                provider_receipt_ref=payload.provider_receipt_ref,
                result_summary=payload.result_summary,
                remote_resource_id=payload.remote_resource_id,
                remote_url=payload.remote_url,
                remote_status=payload.remote_status,
                provider_observed_at=payload.provider_observed_at,
                evidence_note=payload.evidence_note,
                confirmation_level=payload.confirmation_level,
                reason=payload.reason,
            ),
        )

    return router


# Alias used by a few route registries and tests.
build_operations_router = create_operations_router


__all__ = [
    "ActionCreateRequest",
    "ActionDecisionRequest",
    "ActionResponse",
    "ActionReviseRequest",
    "MediaActionCreateRequest",
    "MediaActionRevisionRequest",
    "MediaAdapterStatusResponse",
    "ArtifactCreateRequest",
    "ArtifactResponse",
    "AttemptCompleteRequest",
    "AttemptCreateRequest",
    "AttemptResponse",
    "ConnectionCreateRequest",
    "ConnectionCollectionUpdateRequest",
    "ConnectionListResponse",
    "ConnectionResponse",
    "ConnectionUpdateRequest",
    "DraftCreateRequest",
    "DraftResponse",
    "EvaluationCreateRequest",
    "EvaluationResponse",
    "EventListResponse",
    "EventResponse",
    "OpportunityCreateRequest",
    "OpportunityDetailResponse",
    "OpportunityListResponse",
    "OpportunityResponse",
    "ReconcileRequest",
    "build_operations_router",
    "create_operations_router",
]
