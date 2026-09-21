"""Trusted Operations Kernel v1 service layer.

The service is the only mutation boundary for EngagementOps records.  It
performs project ACL checks, computes hashes, appends timeline events and
guards every state transition with an optimistic ``version`` check.  No agent
endpoint is exposed here; callers identify the principal and the explicit
``actor_type`` is checked for human-only commands.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from inspect import isawaitable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    ApplicationDraft,
    ArtifactVersion,
    ExternalAction,
    ExternalActionApproval,
    ExternalActionAttempt,
    ExternalActionReceipt,
    ExternalConnection,
    MediaPlatformCredential,
    media_credential_state_hash,
    EngagementOpportunity,
    OperationEvent,
    OpportunityEvaluation,
    Project,
    ProjectMember,
)
# MediaOps is an additive surface.  Import the optional rows directly rather
# than through ``memory.models`` so lightweight EngagementOps deployments that
# do not register the WS5 models can still import this service.
try:  # pragma: no cover - exercised by MediaOps integration tests
    from ..memory.models.media_operations_content import (
        CONTENT_VARIANT_PLATFORM_VALUES,
        ContentVariant,
        ContentVariantRevision,
        QAAssessment,
        RightsAssessment,
    )
    from ..memory.models.media_operations_research import ContentItem
    from ..memory.models.media_operations_setup import (
        PlatformAccount,
        PlatformAccountRevision,
    )
    from ..memory.models.media_operations import PersonaRevision
except ImportError:  # pragma: no cover - optional legacy installation
    CONTENT_VARIANT_PLATFORM_VALUES = (
        "x",
        "pixiv",
        "dlsite",
        "patreon",
        "youtube",
        "instagram",
    )
    ContentItem = ContentVariant = ContentVariantRevision = None  # type: ignore[assignment]
    QAAssessment = RightsAssessment = None  # type: ignore[assignment]
    PersonaRevision = PlatformAccount = PlatformAccountRevision = None  # type: ignore[assignment]

# Provider capability observations are an additive WS05 surface.  Keep this
# import optional so the legacy EngagementOps-only deployment can still load
# the service before the capability migration has been applied.
try:  # pragma: no cover - exercised by WS05 integration tests
    from ..memory.models.media_provider_capability import MediaProviderCapabilitySnapshot
except ImportError:  # pragma: no cover - optional legacy installation
    MediaProviderCapabilitySnapshot = None  # type: ignore[assignment]
from ..memory.models.operations import (
    _is_http_url_candidate,
    _is_sensitive_provenance_key,
    default_opportunity_title,
    safe_opportunity_title,
    sanitize_source_url,
)
from ..security.media_credential_crypto import (
    MediaCredentialCryptoError,
    canonical_payload,
    decrypt_media_credential,
    media_credential_ciphertext_key_id,
)
from .project_context import has_project_read_access
from .project_permissions import (
    has_effective_project_permission,
    normalize_project_member_permissions,
)

try:  # pragma: no cover - exercised by WS05 provider execution tests
    from .media_provider_capability_registry import (
        CapabilityStatus,
        effective_operation_status,
        get_provider_capability,
    )
except ImportError:  # pragma: no cover - optional legacy installation
    CapabilityStatus = None  # type: ignore[assignment]
    effective_operation_status = None  # type: ignore[assignment]
    get_provider_capability = None  # type: ignore[assignment]


class OperationsError(RuntimeError):
    """Base error raised by the operations service."""

    status_code = 400


class OperationsNotFoundError(OperationsError):
    status_code = 404


class OperationsAuthorizationError(OperationsError, PermissionError):
    status_code = 403


class OperationsConflictError(OperationsError):
    status_code = 409


class OperationsStaleVersionError(OperationsConflictError):
    """The optimistic version supplied by a caller is stale."""


class OperationsValidationError(OperationsError, ValueError):
    status_code = 422


class OperationsInvalidTransitionError(OperationsConflictError):
    """A state transition is not allowed by the kernel."""


class OperationsHumanRequiredError(OperationsAuthorizationError):
    """A command reserved for a human principal was invoked by an agent."""


_UNSET = object()
_ACTION_APPROVAL_HISTORY_LIMIT = 100
_ACTION_ATTEMPT_HISTORY_LIMIT = 100
_ACTION_TIMELINE_LIMIT = 200
MAX_ARTIFACT_PROVENANCE_BYTES = 64 * 1024

MEDIA_ACTION_TYPES = frozenset(
    {
        "media.publish_content",
        "media.update_content",
        "media.delete_content",
        "media.release_product",
    }
)
MEDIA_PLATFORM_VALUES = tuple(str(item) for item in CONTENT_VARIANT_PLATFORM_VALUES)

# There are intentionally no provider adapters in this slice.  Keeping the
# status explicit prevents a future caller from mistaking a recorded manual
# attempt for a network publication.  The shape is stable for UI/readiness
# consumers and covers every WS5 platform.
MEDIA_ADAPTER_STATUS: dict[str, dict[str, Any]] = {
    platform: {
        "platform": platform,
        "status": "manual",
        "mode": "manual",
        "provider_calls": False,
        "available": True,
    }
    for platform in MEDIA_PLATFORM_VALUES
}
_MEDIA_ACTION_STATUS = {"proposed", "approved", "rejected", "attempting", "succeeded", "failed", "uncertain"}


def _connection_snapshot(connection: ExternalConnection) -> dict[str, Any]:
    """Return the immutable, secret-free provider target identity."""

    return {
        "id": str(connection.id),
        "version": int(connection.version or 1),
        "provider_key": str(connection.provider_key),
        "remote_account_ref": connection.remote_account_ref,
    }


def _connection_target_matches(
    stored: Mapping[str, Any],
    connection: ExternalConnection,
) -> bool:
    """Compare execution-authority fields while allowing display-only edits."""

    current = _connection_snapshot(connection)
    return all(
        stored.get(key) == current.get(key)
        for key in ("id", "provider_key", "remote_account_ref")
    )


def _validated_external_url(value: Any, field: str) -> str | None:
    """Validate and normalize an untrusted external HTTP(S) URL."""

    rendered = _text(value, field, max_bytes=4_000)
    if rendered is None or not rendered.strip():
        return None
    normalized = sanitize_source_url(rendered)
    if normalized is None:
        raise OperationsValidationError(
            f"{field} must be an HTTP(S) URL without userinfo or sensitive query parameters"
        )
    return normalized


def _validated_source_url(value: Any) -> str | None:
    """Validate and normalize an untrusted opportunity source URL."""

    return _validated_external_url(value, "source_url")


def _bounded_page(limit: Any, offset: Any, *, maximum: int = 100) -> tuple[int, int]:
    """Normalize list pagination without allowing post-query filtering."""

    if isinstance(limit, bool) or isinstance(offset, bool):
        raise OperationsValidationError("limit and offset must be integers")
    try:
        limit_value = int(limit)
        offset_value = int(offset)
    except (TypeError, ValueError) as exc:
        raise OperationsValidationError("limit and offset must be integers") from exc
    if limit_value < 1 or limit_value > maximum:
        raise OperationsValidationError(f"limit must be between 1 and {maximum}")
    if offset_value < 0:
        raise OperationsValidationError("offset must be a non-negative integer")
    return limit_value, offset_value


def _json_safe(value: Any) -> Any:
    """Normalize JSON values into a deterministic, JSON-serializable shape."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OperationsValidationError("non-finite numbers are not valid payload values")
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(subvalue) for key, subvalue in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    # Pydantic models and similar DTOs commonly expose model_dump().
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    raise OperationsValidationError(f"value of type {type(value).__name__} is not JSON serializable")


def _validated_provenance(value: Any) -> Any:
    """Validate and canonicalize URL-like values before JSON normalization.

    Provenance is metadata, so ordinary prose and non-HTTP strings are kept
    byte-for-byte.  A complete absolute HTTP(S) scalar is treated as a URL and
    must pass the same public sanitizer used for source/receipt URLs.
    """

    if isinstance(value, Mapping):
        return {
            key: _validated_provenance(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_validated_provenance(item) for item in value]
    if _is_http_url_candidate(value):
        normalized = sanitize_source_url(value)
        if normalized is None:
            raise OperationsValidationError("artifact provenance contains unsafe URL")
        return normalized
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON for hashing and idempotency."""

    try:
        normalized = _json_safe(value)
        rendered = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        # ``ensure_ascii=False`` intentionally preserves Unicode, but an
        # unpaired surrogate would otherwise fail later at a less controlled
        # hashing/DB boundary.  Validate the exact bytes produced by callers
        # and map all serializer failures to the public validation error.
        rendered.encode("utf-8")
        return rendered
    except OperationsValidationError:
        raise
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError):
        raise OperationsValidationError("value cannot be canonicalized") from None


def canonicalize_payload(value: Any) -> str:
    """Compatibility alias used by API/tests."""

    return canonical_json(value)


canonicalize_json = canonical_json


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _media_revision_content_hash(revision: Any) -> str:
    """Recompute the immutable WS5 revision digest at the publication gate.

    A stored ``content_hash`` is evidence, not authority.  Recomputing the
    digest here prevents a tampered revision whose hash was left unchanged
    from satisfying the QA/Rights readiness check.
    """

    return sha256_json(
        {
            "content_item_id": str(revision.content_item_id),
            "content_item_hash": revision.content_item_hash,
            "persona_revision_id": str(revision.persona_revision_id),
            "persona_revision_hash": revision.persona_revision_hash,
            "platform_account_id": (
                str(revision.platform_account_id)
                if revision.platform_account_id
                else None
            ),
            "platform_account_revision_id": (
                str(revision.platform_account_revision_id)
                if revision.platform_account_revision_id
                else None
            ),
            "platform_account_revision_hash": revision.platform_account_revision_hash,
            "platform": revision.platform,
            "payload": revision.payload_json,
            "generation_output_refs": revision.generation_output_refs_json,
            "source_evidence": revision.source_evidence_json,
        }
    )


def _media_assessment_hash(assessment: Any, revision: Any) -> str:
    """Recompute an append-only QA/Rights digest at the publication gate."""

    policy_id = getattr(assessment, "policy_revision_id", None)
    return sha256_json(
        {
            "revision_id": str(revision.id),
            "revision_hash": revision.content_hash,
            "policy_revision_id": str(policy_id) if policy_id else None,
            "policy_revision_hash": assessment.policy_revision_hash,
            "result": assessment.result,
            "checks": list(getattr(assessment, "checks_json", None) or []),
            "findings": list(getattr(assessment, "findings_json", None) or []),
            "evidence": list(getattr(assessment, "evidence_json", None) or []),
        }
    )


def payload_hash(value: Any) -> str:
    return sha256_json(value)


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return value
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise OperationsValidationError("provider_observed_at must be an ISO datetime")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperationsValidationError("provider_observed_at must be an ISO datetime") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


_CREDENTIAL_REF_RE = re.compile(
    r"^(?:vault|secret|credential)://[A-Za-z0-9][A-Za-z0-9._/-]{0,479}$"
)
_PROTECTED_INPUT_MARKERS = (
    "api_key",
    "authorization",
    "browser_context",
    "browser_data",
    "browser_profile",
    "browser_state",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "secret",
    "token",
)


def _credential_reference(value: Any) -> str | None:
    rendered = _text(value, "credential_ref", max_bytes=512)
    if rendered is None:
        return None
    if _CREDENTIAL_REF_RE.fullmatch(rendered) is None:
        raise OperationsValidationError(
            "credential_ref must be an opaque vault/secret/credential reference"
        )
    return rendered


def _is_media_vault_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("credential://media-platform/")


def _reject_protected_input_keys(value: Any, *, field: str) -> None:
    """Reject secret-shaped keys before arbitrary JSON reaches persistence."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if _is_sensitive_provenance_key(key) or normalized in _PROTECTED_INPUT_MARKERS:
                raise OperationsValidationError(
                    f"{field} must not contain protected key {normalized!r}"
                )
            _reject_protected_input_keys(item, field=field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_protected_input_keys(item, field=field)


def _normalize_confirmation_level(value: str | None) -> str:
    normalized = str(value or "human_confirmed").strip().lower()
    aliases = {
        "human": "human_confirmed",
        "manual": "human_confirmed",
        "provider": "provider_confirmed",
        "confirmed": "provider_confirmed",
        "reconcile": "reconciled",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"human_confirmed", "provider_confirmed", "reconciled"}:
        raise OperationsValidationError("confirmation_level is invalid")
    return normalized


# ``complete_attempt`` is intentionally a human-facing service method.  A
# provider adapter is the only code path that may mark a provider attempt as
# succeeded directly; the opaque sentinel keeps that authority out of the
# HTTP/Pydantic surface (and cannot be forged by a JSON caller).
_PROVIDER_ADAPTER_CONFIRMATION_TOKEN = object()


def _has_provider_success_evidence(
    *,
    provider_receipt_ref: str | None,
    remote_resource_id: str | None,
    remote_status: str | None,
    provider_observed_at: datetime | None,
    evidence_artifact_ids: Sequence[Any] | None,
    evidence_note: str | None,
) -> bool:
    """Return whether a provider success has a bounded receipt/postcondition.

    A free-form result summary is deliberately not evidence: it is caller
    narration and can be fabricated without a provider observation.  A
    provider receipt reference is sufficient on its own.  Otherwise the
    remote resource identity must be paired with at least one bounded
    postcondition/evidence field that has already passed service validation.
    """

    if provider_receipt_ref is not None and str(provider_receipt_ref).strip():
        return True
    if remote_resource_id is None or not str(remote_resource_id).strip():
        return False
    return bool(
        (remote_status is not None and str(remote_status).strip())
        or provider_observed_at is not None
        or (evidence_note is not None and str(evidence_note).strip())
        or bool(evidence_artifact_ids)
    )


def _artifact_storage_root() -> Path:
    configured = os.environ.get("AOITALK_OPERATIONS_ARTIFACT_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        # Keep the default under the repository's existing runtime data area;
        # callers only receive the opaque relative storage_ref.
        root = Path(__file__).resolve().parents[2] / "data" / "operations_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _persist_artifact_bytes(data: bytes, digest: str) -> str:
    """Persist bytes in a content-addressed file, atomically and idempotently."""

    root = _artifact_storage_root()
    target = root / digest
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise OperationsError("artifact storage is unavailable") from exc
        if sha256_bytes(existing) != digest or len(existing) != len(data):
            raise OperationsConflictError("content-addressed artifact storage mismatch")
    else:
        temporary = root / f".{digest}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            try:
                os.replace(temporary, target)
            except FileExistsError:
                # Another request won the race; verify its content below.
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if not target.exists():
            raise OperationsError("artifact storage write failed")
    return f"operations_artifacts/{digest}"


def _as_uuid(value: UUID | str | None, label: str, *, required: bool = True) -> UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise OperationsValidationError(f"{label} is required")
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise OperationsValidationError(f"{label} is not a valid UUID") from exc


def _actor_field(actor: Any, name: str, default: Any = None) -> Any:
    if isinstance(actor, Mapping):
        return actor.get(name, default)
    return getattr(actor, name, default)


def _actor_id(actor: Any) -> UUID:
    raw = _actor_field(actor, "id") or _actor_field(actor, "user_id")
    value = _as_uuid(raw, "actor id")
    assert value is not None
    return value


def _actor_type(actor: Any) -> str:
    if _actor_field(actor, "is_agent", False):
        return "agent"
    raw = _actor_field(actor, "actor_type", None)
    if raw is None:
        return "unknown"
    value = str(raw).strip().lower()
    return value if value in {"human", "agent", "system"} else "unknown"


async def _validate_typed_origin(
    session: Any,
    *,
    origin_agent_id: UUID | str | None = None,
    origin_agent_run_id: UUID | str | None = None,
    origin_work_item_id: UUID | str | None = None,
) -> tuple[UUID | None, UUID | None, UUID | None]:
    """Validate trusted Agent-origin links without changing human ownership."""

    if origin_agent_id is None and origin_agent_run_id is None:
        if origin_work_item_id is None:
            return None, None, None
    agent_uuid = _as_uuid(origin_agent_id, "origin_agent_id", required=False)
    run_uuid = _as_uuid(origin_agent_run_id, "origin_agent_run_id", required=False)
    work_item_uuid = _as_uuid(origin_work_item_id, "origin_work_item_id", required=False)
    if origin_agent_id is not None and agent_uuid is None:
        raise OperationsValidationError("origin_agent_id is invalid")
    if origin_agent_run_id is not None and run_uuid is None:
        raise OperationsValidationError("origin_agent_run_id is invalid")
    if origin_work_item_id is not None and work_item_uuid is None:
        raise OperationsValidationError("origin_work_item_id is invalid")
    from ..memory.models import Agent, AgentRun
    from ..memory.models import AgentWorkItem

    if agent_uuid is None and run_uuid is not None:
        run = await session.get(AgentRun, run_uuid)
        agent_uuid = getattr(run, "agent_id", None) if run is not None else None
    if agent_uuid is None and work_item_uuid is not None:
        work_item = await session.get(AgentWorkItem, work_item_uuid)
        agent_uuid = getattr(work_item, "assigned_agent_id", None) if work_item is not None else None
    agent = await session.get(Agent, agent_uuid) if agent_uuid is not None else None
    if agent is None or str(getattr(agent, "state", "")) != "active":
        raise OperationsAuthorizationError("origin Agent is not active")
    if run_uuid is not None:
        run = await session.get(AgentRun, run_uuid)
        if run is None or run.agent_id != agent_uuid:
            raise OperationsConflictError("origin AgentRun is not bound to origin Agent")
    if work_item_uuid is not None:
        work_item = await session.get(AgentWorkItem, work_item_uuid)
        if work_item is None:
            raise OperationsNotFoundError("origin WorkItem was not found")
        # A typed origin must identify the exact assigned Agent. An
        # unassigned WorkItem is not a provenance grant for an arbitrary
        # active Agent.
        if agent_uuid is None or work_item.assigned_agent_id != agent_uuid:
            raise OperationsConflictError("origin WorkItem is not assigned to origin Agent")
        if run_uuid is not None:
            run_for_work = await session.get(AgentRun, run_uuid)
            if (
                run_for_work is None
                or getattr(run_for_work, "work_item_id", None) != work_item.id
                or getattr(run_for_work, "agent_revision_id", None)
                != getattr(work_item, "agent_revision_id", None)
            ):
                raise OperationsConflictError(
                    "origin AgentRun is not bound to origin WorkItem"
                )
        if run_uuid is not None and getattr(work_item, "active_agent_run_id", None) != run_uuid:
            raise OperationsConflictError("origin WorkItem is not bound to origin AgentRun")
    if run_uuid is not None:
        run = await session.get(AgentRun, run_uuid)
        if run is None or run.agent_id != agent_uuid:
            raise OperationsConflictError("origin AgentRun is not bound to origin Agent")
        if str(getattr(run, "status", "") or "") in {"succeeded", "failed", "cancelled"}:
            raise OperationsConflictError("origin AgentRun is no longer active")
    return agent_uuid, run_uuid, work_item_uuid


def _text(value: Any, label: str, *, required: bool = False, max_bytes: int = 1_000_000) -> str | None:
    if value is None:
        if required:
            raise OperationsValidationError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise OperationsValidationError(f"{label} must be a string")
    if required and not value.strip():
        raise OperationsValidationError(f"{label} must not be empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise OperationsValidationError(f"{label} exceeds maximum size")
    return value


def _hash_list(values: Sequence[Any] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise OperationsValidationError("artifact hashes must be a list")
    result: list[str] = []
    for raw in values:
        value = str(raw).strip().lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise OperationsValidationError("artifact hashes must be SHA-256 values")
        if value not in result:
            result.append(value)
    return result


def _id_list(values: Sequence[Any] | None) -> list[UUID]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise OperationsValidationError("artifact_version_ids must be a list")
    if len(values) > 100:
        raise OperationsValidationError("artifact_version_ids exceeds 100 items")
    result: list[UUID] = []
    for raw in values:
        value = _as_uuid(raw, "artifact_version_id")
        assert value is not None
        if value not in result:
            result.append(value)
    return result


def _normalize_media_action_type(value: Any) -> str:
    """Return one of the four explicitly supported MediaOps action keys."""

    normalized = str(getattr(value, "value", value) or "").strip().lower()
    aliases = {
        "publish": "media.publish_content",
        "update": "media.update_content",
        "delete": "media.delete_content",
        "release": "media.release_product",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in MEDIA_ACTION_TYPES:
        raise OperationsValidationError(
            "action_type must be one of media.publish_content, media.update_content, "
            "media.delete_content, or media.release_product"
        )
    return normalized


def _normalized_media_mapping(
    value: Any,
    field: str,
    *,
    default: Mapping[str, Any] | None = None,
    max_bytes: int = 32_000,
) -> dict[str, Any]:
    """Normalize a bounded semantic mapping used by a MediaOps proposal."""

    if value is None:
        value = default or {}
    if not isinstance(value, Mapping):
        raise OperationsValidationError(f"{field} must be an object")
    _reject_protected_input_keys(value, field=field)
    normalized = _json_safe(dict(value))
    if not isinstance(normalized, dict):  # pragma: no cover - _json_safe contract
        raise OperationsValidationError(f"{field} must be an object")
    if len(canonical_json(normalized).encode("utf-8")) > max_bytes:
        raise OperationsValidationError(f"{field} exceeds {max_bytes} bytes")
    return normalized


def _normalize_media_hash(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise OperationsValidationError(f"{field} is required")
        return None
    rendered = str(value).strip().lower()
    if len(rendered) != 64 or any(char not in "0123456789abcdef" for char in rendered):
        raise OperationsValidationError(f"{field} must be a SHA-256 value")
    return rendered


def _normalize_media_platform(value: Any) -> str:
    rendered = str(getattr(value, "value", value) or "").strip().lower()
    if rendered not in MEDIA_PLATFORM_VALUES:
        raise OperationsValidationError(
            "platform must be one of x, pixiv, dlsite, patreon, youtube, instagram"
        )
    return rendered


_MEDIA_EXECUTION_MODES = frozenset({"manual", "provider"})


def _normalize_media_execution_mode(value: Any, *, default: str = "manual") -> str:
    rendered = str(value or default).strip().lower()
    if rendered not in _MEDIA_EXECUTION_MODES:
        raise OperationsValidationError("execution_mode must be manual or provider")
    return rendered


def _normalize_provider_adapter_value(
    value: Any,
    field: str,
    *,
    required: bool = False,
    max_bytes: int = 128,
) -> str | None:
    rendered = _text(value, field, required=required, max_bytes=max_bytes)
    if rendered is None:
        return None
    rendered = rendered.strip()
    if required and not rendered:
        raise OperationsValidationError(f"{field} is required")
    if not rendered:
        return None
    # Adapter identifiers are opaque server-owned keys, not URLs, paths or
    # arbitrary JSON.  Keep the grammar deliberately small so they are safe
    # to include in timeline/audit metadata.
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", rendered) is None
        or re.match(r"^[A-Za-z]:[/\\]", rendered)
        or "/../" in f"/{rendered}/"
        or rendered.endswith("/..")
    ):
        raise OperationsValidationError(f"{field} contains invalid characters")
    return rendered


def _media_provider_operation(action_type: Any, payload: Mapping[str, Any] | None) -> str:
    """Map one typed MediaOps action to the closed registry operation set."""

    action_key = str(action_type or "").strip().lower()
    if action_key == "media.release_product":
        return "revenue"
    if action_key == "media.delete_content":
        return "delete"
    if action_key == "media.update_content":
        return "edit"
    raw_type = payload.get("type") if isinstance(payload, Mapping) else None
    rendered = str(raw_type or "").strip().lower()
    if "video" in rendered or "reel" in rendered:
        return "video"
    if "image" in rendered or "carousel" in rendered or "work" in rendered:
        return "image"
    # Text-only posts/threads and unknown typed payloads fail closed through
    # the registry's status lookup rather than guessing a provider operation.
    return "text"


class OperationsService:
    """Application service for trusted external operations."""

    def __init__(self, session: Any | None = None, provider_adapter: Any | None = None):
        # A bound session is convenient for focused tests; production routes
        # pass the session explicitly to each method.
        self.session = session
        # Provider adapters are intentionally dependency-injected and absent
        # from the production composition root until a provider-specific
        # private/draft contract is verified.  This keeps the default path
        # fail-closed while allowing deterministic contract tests to exercise
        # timeout/receipt/reconcile behavior without network calls.
        self.provider_adapter = provider_adapter

    def _resolve_session(self, session: Any | None) -> Any:
        resolved = session or self.session
        if resolved is None:
            raise OperationsValidationError("database session is required")
        return resolved

    async def _execute(self, session: Any, statement: Any) -> Any:
        result = session.execute(statement)
        if isawaitable(result):
            result = await result
        return result

    async def _scalar(self, session: Any, statement: Any) -> Any:
        result = await self._execute(session, statement)
        scalar = getattr(result, "scalar", None)
        if callable(scalar):
            return scalar()
        scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
        if callable(scalar_one_or_none):
            return scalar_one_or_none()
        return result

    async def _scalars(self, session: Any, statement: Any) -> list[Any]:
        result = await self._execute(session, statement)
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            rows = scalars().all()
            return list(rows)
        return list(result or [])

    async def _flush_commit(self, session: Any) -> None:
        flush = getattr(session, "flush", None)
        if callable(flush):
            result = flush()
            if isawaitable(result):
                await result
        await self._commit(session)

    async def _flush_only(self, session: Any) -> None:
        flush = getattr(session, "flush", None)
        if callable(flush):
            result = flush()
            if isawaitable(result):
                await result

    async def _commit(self, session: Any) -> None:
        commit = getattr(session, "commit", None)
        if callable(commit):
            result = commit()
            if isawaitable(result):
                await result

    async def _rollback(self, session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            result = rollback()
            if isawaitable(result):
                await result

    async def _get_project(self, session: Any, project_id: UUID | None) -> Project | None:
        if project_id is None:
            return None
        return await self._scalar(session, select(Project).where(Project.id == project_id).limit(1))

    async def _assert_access(
        self,
        session: Any,
        actor: Any,
        *,
        project_id: UUID | None,
        owner_user_id: UUID | None = None,
        permission: str = "read",
    ) -> UUID:
        actor_id = _actor_id(actor)
        role = str(_actor_field(actor, "role", "") or "").lower()
        if project_id is None:
            if owner_user_id is not None and actor_id == owner_user_id:
                return actor_id
            if role == "admin":
                return actor_id
            raise OperationsAuthorizationError("operation access denied")
        project = await self._get_project(session, project_id)
        if project is None or getattr(project, "deleted_at", None) is not None:
            raise OperationsNotFoundError("project not found")
        if role == "admin":
            return actor_id
        # Keep read behavior aligned with the canonical project context helper.
        if permission == "read":
            allowed = await has_project_read_access(
                session,
                project,
                user_id=str(actor_id),
                user_role=role or None,
            )
        else:
            member = await self._scalar(
                session,
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id, ProjectMember.user_id == actor_id)
                .limit(1),
            )
            allowed = has_effective_project_permission(
                user_id=actor_id,
                user_role=role,
                project_owner_id=getattr(project, "owner_id", None),
                member_permissions=getattr(member, "permissions", None),
                permission=permission,
            )
        if not allowed:
            raise OperationsAuthorizationError("operation access denied")
        return actor_id

    async def _authorized_project_ids(self, session: Any, actor: Any) -> list[UUID]:
        """Resolve readable Projects through the canonical Project ACL helper.

        Scope-less collection reads must apply ACLs *before* LIMIT/OFFSET.  A
        caller-owned row in another user's Project is not a personal grant;
        only the canonical ``has_project_read_access`` decision can authorize
        that Project.  The resulting IDs are then used in one SQL predicate so
        pagination cannot leak gaps or shift across unauthorized rows.
        """

        actor_id = _actor_id(actor)
        role = str(_actor_field(actor, "role", "") or "").strip().lower() or None
        if role == "admin":
            return await self._scalars(
                session,
                select(Project.id)
                .where(Project.deleted_at.is_(None))
                .order_by(Project.created_at.asc(), Project.id.asc()),
            )
        result = await self._execute(
            session,
            select(Project.id, Project.owner_id, ProjectMember.permissions)
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == actor_id,
                ),
            )
            .where(
                Project.deleted_at.is_(None),
                or_(Project.owner_id == actor_id, ProjectMember.user_id == actor_id),
            )
            .order_by(Project.created_at.asc(), Project.id.asc()),
        )
        rows = result.all() if callable(getattr(result, "all", None)) else list(result or [])
        return [
            project_id
            for project_id, owner_id, permissions in rows
            if has_effective_project_permission(
                user_id=actor_id,
                user_role=role,
                project_owner_id=owner_id,
                member_permissions=permissions,
                permission="read",
            )
        ]

    async def _scope_less_condition(
        self,
        session: Any,
        actor: Any,
        model: Any,
    ) -> Any:
        """Build an ACL-filtered SQL condition for a scope-less collection."""

        actor_id = _actor_id(actor)
        project_ids = await self._authorized_project_ids(session, actor)
        personal = and_(model.project_id.is_(None), model.owner_user_id == actor_id)
        if not project_ids:
            return personal
        return or_(personal, model.project_id.in_(project_ids))

    async def _assert_human(self, actor: Any) -> UUID:
        actor_id = _actor_id(actor)
        actor_type = _actor_type(actor)
        # Admins are trusted human operators even when a repository adapter
        # omits the explicit ``actor_type`` marker.  An agent marker always
        # wins, so a mislabeled admin token cannot execute provider actions.
        role = str(_actor_field(actor, "role", "") or "").strip().lower()
        is_admin_human = role == "admin" and not bool(_actor_field(actor, "is_agent", False)) and actor_type == "unknown"
        if actor_type != "human" and not is_admin_human:
            raise OperationsHumanRequiredError("this command requires a human principal")
        return actor_id

    async def _assert_entity_access(
        self,
        session: Any,
        actor: Any,
        entity: Any,
        *,
        permission: str = "read",
    ) -> UUID:
        return await self._assert_access(
            session,
            actor,
            project_id=getattr(entity, "project_id", None),
            owner_user_id=getattr(entity, "owner_user_id", None),
            permission=permission,
        )

    async def _assert_create_scope(self, session: Any, actor: Any, project_id: UUID | None) -> UUID:
        """Authorize a new row before its owner_user_id exists."""

        actor_id = _actor_id(actor)
        if project_id is None:
            return actor_id
        await self._assert_access(session, actor, project_id=project_id, owner_user_id=None, permission="write")
        return actor_id

    async def _event(
        self,
        session: Any,
        *,
        actor: Any,
        entity_type: str,
        entity_id: UUID,
        event_type: str,
        owner_user_id: UUID,
        project_id: UUID | None,
        payload: Mapping[str, Any] | None = None,
    ) -> OperationEvent:
        event = OperationEvent(
            owner_user_id=owner_user_id,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_id=_actor_id(actor),
            actor_type=_actor_type(actor),
            payload_json=_json_safe(dict(payload or {})),
        )
        session.add(event)
        return event

    async def _get_or_404(
        self,
        session: Any,
        model: Any,
        entity_id: UUID | str,
        label: str,
        *,
        for_update: bool = False,
    ) -> Any:
        parsed = _as_uuid(entity_id, label)
        assert parsed is not None
        statement = select(model).where(model.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()
        value = await self._scalar(session, statement)
        if value is None:
            raise OperationsNotFoundError(f"{label} not found")
        return value

    async def _check_expected_version(self, entity: Any, expected_version: int | None) -> None:
        if expected_version is None:
            raise OperationsValidationError("expected_version is required")
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise OperationsValidationError("expected_version must be an integer") from exc
        actual = int(getattr(entity, "version", 0) or 0)
        if expected != actual:
            raise OperationsStaleVersionError(
                f"stale version: expected {expected}, current {actual}"
            )

    async def _artifact_rows(
        self,
        session: Any,
        actor: Any,
        artifact_ids: Sequence[Any],
        *,
        project_id: UUID | None,
    ) -> list[ArtifactVersion]:
        ids = _id_list(artifact_ids)
        if not ids:
            return []
        rows = await self._scalars(session, select(ArtifactVersion).where(ArtifactVersion.id.in_(ids)))
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(ids):
            raise OperationsNotFoundError("artifact version not found")
        for row in rows:
            if row.project_id != project_id and row.project_id is not None:
                raise OperationsAuthorizationError("artifact belongs to another project")
            await self._assert_entity_access(session, actor, row, permission="read")
        return [by_id[item] for item in ids]

    # ------------------------------------------------------------------
    # External connections
    # ------------------------------------------------------------------

    async def create_connection(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        provider_key: str,
        display_name: str,
        remote_account_ref: str | None = None,
        auth_status: str = "unknown",
        project_id: UUID | str | None = None,
        credential_ref: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        actor_id = _actor_id(actor)
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        await self._assert_create_scope(session, actor, project_uuid)
        provider = _text(provider_key, "provider_key", required=True, max_bytes=120)
        name = _text(display_name, "display_name", required=True, max_bytes=255)
        status = _text(auth_status, "auth_status", required=True, max_bytes=32)
        # credential_ref is opaque by contract.  It is intentionally never
        # copied into events or returned DTOs, and only strict reference URIs
        # are accepted even on trusted/internal service calls.
        opaque_ref = _credential_reference(credential_ref)
        if _is_media_vault_reference(opaque_ref):
            raise OperationsValidationError(
                "media credential references are owned by the credential vault"
            )
        _reject_protected_input_keys(metadata or {}, field="connection metadata")
        connection = ExternalConnection(
            owner_user_id=actor_id,
            project_id=project_uuid,
            provider_key=provider,
            display_name=name,
            remote_account_ref=_text(remote_account_ref, "remote_account_ref", max_bytes=255),
            credential_ref=opaque_ref,
            auth_status=status,
            metadata_json=_json_safe(dict(metadata or {})),
        )
        session.add(connection)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="connection",
            entity_id=connection.id,
            event_type="connection.created",
            owner_user_id=actor_id,
            project_id=project_uuid,
            payload={"provider_key": provider, "auth_status": status},
        )
        await self._flush_commit(session)
        return connection.to_safe_dict()

    async def list_connections(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, page_offset = _bounded_page(limit, offset)
        if project_uuid is not None:
            await self._assert_access(session, actor, project_id=project_uuid, permission="read")
            scope_condition = ExternalConnection.project_id == project_uuid
        else:
            scope_condition = await self._scope_less_condition(
                session,
                actor,
                ExternalConnection,
            )
        rows = await self._scalars(
            session,
            select(ExternalConnection)
            .where(scope_condition)
            .order_by(ExternalConnection.created_at.desc(), ExternalConnection.id.desc())
            .limit(page_limit)
            .offset(page_offset),
        )
        return [row.to_safe_dict() for row in rows]

    async def get_connection(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        connection_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or connection_id is None:
            raise OperationsValidationError("actor and connection_id are required")
        connection = await self._get_or_404(session, ExternalConnection, connection_id, "connection_id")
        await self._assert_entity_access(session, actor, connection, permission="read")
        return connection.to_safe_dict()

    async def update_connection(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        connection_id: UUID | str | None = None,
        *,
        expected_version: int | None,
        provider_key: str | None = None,
        display_name: str | None = None,
        remote_account_ref: str | None | object = _UNSET,
        auth_status: str | None = None,
        credential_ref: str | None | object = _UNSET,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or connection_id is None:
            raise OperationsValidationError("actor and connection_id are required")
        connection = await self._get_or_404(session, ExternalConnection, connection_id, "connection_id", for_update=True)
        await self._assert_entity_access(session, actor, connection, permission="write")
        if _is_media_vault_reference(connection.credential_ref):
            raise OperationsAuthorizationError(
                "vault-owned credential connections must be changed through the credential vault"
            )
        await self._check_expected_version(connection, expected_version)
        if provider_key is not None:
            connection.provider_key = _text(provider_key, "provider_key", required=True, max_bytes=120)
        if display_name is not None:
            connection.display_name = _text(display_name, "display_name", required=True, max_bytes=255)
        if remote_account_ref is not _UNSET:
            connection.remote_account_ref = _text(remote_account_ref, "remote_account_ref", max_bytes=255)
        if auth_status is not None:
            connection.auth_status = _text(auth_status, "auth_status", required=True, max_bytes=32)
        if credential_ref is not _UNSET:
            next_credential_ref = _credential_reference(credential_ref)
            if _is_media_vault_reference(next_credential_ref):
                raise OperationsAuthorizationError(
                    "media credential references are owned by the credential vault"
                )
            connection.credential_ref = next_credential_ref
        if metadata is not None:
            _reject_protected_input_keys(metadata, field="connection metadata")
            connection.metadata_json = _json_safe(dict(metadata))
        connection.version = int(connection.version or 1) + 1
        await self._flush_only(session)
        actor_id = _actor_id(actor)
        await self._event(
            session,
            actor=actor,
            entity_type="connection",
            entity_id=connection.id,
            event_type="connection.updated",
            owner_user_id=connection.owner_user_id,
            project_id=connection.project_id,
            payload={"version": connection.version},
        )
        await self._flush_commit(session)
        return connection.to_safe_dict()

    # ------------------------------------------------------------------
    # Artifacts and opportunities
    # ------------------------------------------------------------------

    async def create_artifact(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        content: bytes | bytearray | None = None,
        filename: str | None = None,
        mime_type: str = "application/octet-stream",
        project_id: UUID | str | None = None,
        provenance: Mapping[str, Any] | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        storage_ref: str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        actor_id = _actor_id(actor)
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        await self._assert_create_scope(session, actor, project_uuid)
        data: bytes | None = None
        if content is not None:
            if isinstance(content, bytearray):
                data = bytes(content)
            elif isinstance(content, bytes):
                data = content
            else:
                raise OperationsValidationError("content must be bytes")
        if data is None:
            raise OperationsValidationError(
                "artifact content is required; unverified references cannot be registered"
            )
        if len(data) > 25 * 1024 * 1024:
            raise OperationsValidationError("artifact content exceeds 25 MiB")
        computed_hash = sha256_bytes(data)
        computed_size = len(data)
        if sha256 is not None and str(sha256).strip().lower() != computed_hash:
            raise OperationsValidationError("sha256 does not match content")
        if size_bytes is not None and int(size_bytes) != computed_size:
            raise OperationsValidationError("size_bytes does not match content")
        sha_value, size_value = computed_hash, computed_size
        if storage_ref is not None:
            raise OperationsValidationError("client supplied storage_ref is not supported")
        if provenance is None:
            provenance_input: Mapping[str, Any] = {}
        elif isinstance(provenance, Mapping):
            provenance_input = dict(provenance)
        else:
            raise OperationsValidationError("provenance must be an object")
        _reject_protected_input_keys(provenance_input, field="artifact provenance")
        # URL-like provenance is validated before JSON conversion, size
        # accounting, filesystem writes, or database mutation.
        try:
            provenance_value = _json_safe(_validated_provenance(provenance_input))
            provenance_bytes = len(canonical_json(provenance_value).encode("utf-8"))
        except OperationsValidationError:
            raise
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError):
            raise OperationsValidationError("artifact provenance is invalid") from None
        if provenance_bytes > MAX_ARTIFACT_PROVENANCE_BYTES:
            raise OperationsValidationError("artifact provenance exceeds 64 KiB")
        mime = _text(mime_type, "mime_type", required=True, max_bytes=255)
        name = _text(filename, "filename", max_bytes=512)
        # Content-addressed registration is idempotent for the same owner,
        # scope, hash, size and MIME.  It never mutates the existing version.
        identity_conditions = [
            ArtifactVersion.sha256 == sha_value,
            ArtifactVersion.size_bytes == size_value,
            ArtifactVersion.mime_type == mime,
        ]
        if project_uuid is None:
            identity_conditions.extend(
                [
                    ArtifactVersion.owner_user_id == actor_id,
                    ArtifactVersion.project_id.is_(None),
                ]
            )
        else:
            identity_conditions.extend(
                [
                    ArtifactVersion.owner_user_id == actor_id,
                    ArtifactVersion.project_id == project_uuid,
                ]
            )
        existing = await self._scalar(
            session,
            select(ArtifactVersion).where(*identity_conditions).limit(1),
        )
        if existing is not None:
            return existing.to_safe_dict()
        generated_storage_ref = _persist_artifact_bytes(data, sha_value)
        artifact = ArtifactVersion(
            owner_user_id=actor_id,
            project_id=project_uuid,
            filename=name,
            sha256=sha_value,
            size_bytes=size_value,
            mime_type=mime,
            storage_ref=generated_storage_ref,
            provenance_json=provenance_value,
            created_by=actor_id,
        )
        session.add(artifact)
        try:
            await self._flush_only(session)
        except IntegrityError:
            # The fast SELECT above is intentionally retained for the common
            # path, while the unique partial index closes the concurrent race.
            # Roll back only the losing transaction, then resolve the exact
            # identity in the same session.  The content-addressed file is
            # shared and must never be deleted by the losing request.
            await self._rollback(session)
            existing = await self._scalar(
                session,
                select(ArtifactVersion).where(*identity_conditions).limit(1),
            )
            if existing is None:
                raise
            return existing.to_safe_dict()
        await self._event(
            session,
            actor=actor,
            entity_type="artifact",
            entity_id=artifact.id,
            event_type="artifact.created",
            owner_user_id=actor_id,
            project_id=project_uuid,
            payload={"sha256": sha_value, "size_bytes": size_value, "mime_type": mime},
        )
        await self._flush_commit(session)
        return artifact.to_safe_dict()

    # Common names used by integrations/tests.
    upload_artifact = create_artifact
    register_artifact = create_artifact

    async def create_opportunity(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        title: str | None = None,
        source_url: str | None = None,
        source_text: str | None = None,
        source_snapshot: Mapping[str, Any] | Sequence[Any] | str | None = None,
        source_snapshot_hash: str | None = None,
        connection_id: UUID | str | None = None,
        project_id: UUID | str | None = None,
        status: str = "open",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        actor_id = _actor_id(actor)
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        await self._assert_create_scope(session, actor, project_uuid)
        title_value = _text(title, "title", max_bytes=500)
        if not title_value or not title_value.strip():
            title_value = default_opportunity_title(source_url)
        connection_uuid = _as_uuid(connection_id, "connection_id", required=False)
        if connection_uuid is not None:
            connection = await self._get_or_404(session, ExternalConnection, connection_uuid, "connection_id")
            await self._assert_entity_access(session, actor, connection, permission="read")
            if connection.project_id != project_uuid:
                raise OperationsValidationError("connection and opportunity project_id must match")
        source_value = _text(source_text, "source_text", max_bytes=1_000_000)
        snapshot_value: Any = {"sha256": None}
        if source_snapshot is not None:
            snapshot_value = _json_safe(source_snapshot)
            computed_snapshot_hash = sha256_json(snapshot_value)
        elif source_value is not None:
            computed_snapshot_hash = sha256_text(source_value)
            snapshot_value = {"sha256": computed_snapshot_hash}
        else:
            computed_snapshot_hash = None
            snapshot_value = {}
        snapshot_hash = str(source_snapshot_hash or computed_snapshot_hash or "").strip().lower() or None
        if source_snapshot_hash is not None:
            if len(snapshot_hash or "") != 64 or any(ch not in "0123456789abcdef" for ch in snapshot_hash or ""):
                raise OperationsValidationError("source_snapshot_hash must be a SHA-256 value")
            if computed_snapshot_hash is not None and snapshot_hash != computed_snapshot_hash:
                raise OperationsValidationError("source_snapshot_hash does not match source snapshot")
        _reject_protected_input_keys(metadata or {}, field="opportunity metadata")
        source_url_value = _validated_source_url(source_url)
        opportunity = EngagementOpportunity(
            owner_user_id=actor_id,
            project_id=project_uuid,
            connection_id=connection_uuid,
            title=title_value,
            source_url=source_url_value,
            source_text=source_value,
            source_snapshot_hash=snapshot_hash,
            source_snapshot_json=snapshot_value,
            status=_text(status, "status", required=True, max_bytes=32),
            metadata_json=_json_safe(dict(metadata or {})),
            created_by=actor_id,
        )
        session.add(opportunity)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="opportunity",
            entity_id=opportunity.id,
            event_type="opportunity.created",
            owner_user_id=actor_id,
            project_id=project_uuid,
            payload={"title": title_value, "source_snapshot_hash": snapshot_hash},
        )
        await self._flush_commit(session)
        return opportunity.to_safe_dict()

    async def list_opportunities(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        connection_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, page_offset = _bounded_page(limit, offset)
        if project_uuid is not None:
            await self._assert_access(session, actor, project_id=project_uuid, permission="read")
            scope_condition = EngagementOpportunity.project_id == project_uuid
        else:
            scope_condition = await self._scope_less_condition(
                session,
                actor,
                EngagementOpportunity,
            )
        conditions: list[Any] = [scope_condition]
        if connection_id is not None:
            conditions.append(EngagementOpportunity.connection_id == _as_uuid(connection_id, "connection_id"))
        if status is not None:
            conditions.append(EngagementOpportunity.status == str(status))
        rows = await self._scalars(
            session,
            select(EngagementOpportunity)
            .where(*conditions)
            .order_by(EngagementOpportunity.created_at.desc(), EngagementOpportunity.id.desc())
            .limit(page_limit)
            .offset(page_offset),
        )
        return [row.to_safe_dict() for row in rows]

    async def get_opportunity(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        opportunity_id: UUID | str | None = None,
        *,
        include_versions: bool = True,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or opportunity_id is None:
            raise OperationsValidationError("actor and opportunity_id are required")
        opportunity = await self._get_or_404(session, EngagementOpportunity, opportunity_id, "opportunity_id")
        await self._assert_entity_access(session, actor, opportunity, permission="read")
        payload = opportunity.to_safe_dict()
        # The browser detail view is an authorized human/project read path;
        # unlike the safe list/projection above it may inspect the captured
        # source text.  Agent/tool projections must use to_safe_dict().
        payload["source_text"] = opportunity.source_text
        payload["source_snapshot"] = {
            "sha256": opportunity.source_snapshot_hash,
            "untrusted": True,
        }
        if include_versions:
            evaluations = await self._scalars(
                session,
                select(OpportunityEvaluation)
                .where(OpportunityEvaluation.opportunity_id == opportunity.id)
                .order_by(OpportunityEvaluation.version.desc())
                .limit(25),
            )
            drafts = await self._scalars(
                session,
                select(ApplicationDraft)
                .where(ApplicationDraft.opportunity_id == opportunity.id)
                .order_by(ApplicationDraft.version.desc())
                .limit(25),
            )
            payload["evaluations"] = [item.to_safe_dict() for item in evaluations]
            payload["drafts"] = [item.to_safe_dict() for item in drafts]
        return payload

    async def create_evaluation(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        opportunity_id: UUID | str | None = None,
        *,
        estimated_effort_hours: float | None = None,
        estimated_cost: float | None = None,
        estimated_revenue: float | None = None,
        fit: str | None = None,
        risks: Sequence[Any] | None = None,
        missing_requirements: Sequence[Any] | None = None,
        summary: str | None = None,
        evidence_refs: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or opportunity_id is None:
            raise OperationsValidationError("actor and opportunity_id are required")
        opportunity = await self._get_or_404(session, EngagementOpportunity, opportunity_id, "opportunity_id", for_update=True)
        await self._assert_entity_access(session, actor, opportunity, permission="write")
        for value, label in (
            (estimated_effort_hours, "estimated_effort_hours"),
            (estimated_cost, "estimated_cost"),
            (estimated_revenue, "estimated_revenue"),
        ):
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value))):
                raise OperationsValidationError(f"{label} must be a finite number")
        latest = await self._scalar(
            session,
            select(func.max(OpportunityEvaluation.version)).where(
                OpportunityEvaluation.opportunity_id == opportunity.id
            ),
        )
        version = int(latest or 0) + 1
        for value, field_name in (
            (risks or [], "evaluation risks"),
            (missing_requirements or [], "evaluation missing_requirements"),
            (evidence_refs or [], "evaluation evidence_refs"),
        ):
            _reject_protected_input_keys(value, field=field_name)
        for value, field_name in (
            (risks or [], "evaluation risks"),
            (missing_requirements or [], "evaluation missing_requirements"),
            (evidence_refs or [], "evaluation evidence_refs"),
        ):
            if len(canonical_json(value).encode("utf-8")) > 16_000:
                raise OperationsValidationError(f"{field_name} exceeds 16000 bytes")
        evaluation = OpportunityEvaluation(
            opportunity_id=opportunity.id,
            owner_user_id=opportunity.owner_user_id,
            project_id=opportunity.project_id,
            version=version,
            estimated_effort_hours=float(estimated_effort_hours) if estimated_effort_hours is not None else None,
            estimated_cost=float(estimated_cost) if estimated_cost is not None else None,
            estimated_revenue=float(estimated_revenue) if estimated_revenue is not None else None,
            fit=_text(fit, "fit", max_bytes=32),
            risks=_json_safe(list(risks or [])),
            missing_requirements=_json_safe(list(missing_requirements or [])),
            summary=_text(summary, "summary", max_bytes=32_000),
            evidence_refs=_json_safe(list(evidence_refs or [])),
            created_by=_actor_id(actor),
        )
        session.add(evaluation)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="evaluation",
            entity_id=evaluation.id,
            event_type="evaluation.created",
            owner_user_id=evaluation.owner_user_id,
            project_id=evaluation.project_id,
            payload={"opportunity_id": str(opportunity.id), "version": version},
        )
        await self._flush_commit(session)
        return evaluation.to_safe_dict()

    # ``add_evaluation`` is kept as a concise service alias.
    add_evaluation = create_evaluation

    async def create_draft(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        opportunity_id: UUID | str | None = None,
        *,
        message: str,
        offered_price: float | None = None,
        currency: str | None = None,
        delivery_estimate: str | None = None,
        artifact_version_ids: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or opportunity_id is None:
            raise OperationsValidationError("actor and opportunity_id are required")
        opportunity = await self._get_or_404(session, EngagementOpportunity, opportunity_id, "opportunity_id", for_update=True)
        await self._assert_entity_access(session, actor, opportunity, permission="write")
        message_value = _text(message, "message", required=True, max_bytes=64_000)
        if offered_price is not None and (
            not isinstance(offered_price, (int, float)) or not math.isfinite(float(offered_price))
        ):
            raise OperationsValidationError("offered_price must be a finite number")
        artifact_ids = _id_list(artifact_version_ids)
        artifacts = await self._artifact_rows(
            session,
            actor,
            artifact_ids,
            project_id=opportunity.project_id,
        )
        latest = await self._scalar(
            session,
            select(func.max(ApplicationDraft.version)).where(ApplicationDraft.opportunity_id == opportunity.id),
        )
        version = int(latest or 0) + 1
        canonical_payload = {
            "message": message_value,
            "offered_price": float(offered_price) if offered_price is not None else None,
            "currency": _text(currency, "currency", max_bytes=16),
            "delivery_estimate": _text(delivery_estimate, "delivery_estimate", max_bytes=255),
            "artifact_version_ids": [str(item.id) for item in artifacts],
        }
        draft = ApplicationDraft(
            opportunity_id=opportunity.id,
            owner_user_id=opportunity.owner_user_id,
            project_id=opportunity.project_id,
            version=version,
            message=message_value,
            offered_price=canonical_payload["offered_price"],
            currency=canonical_payload["currency"],
            delivery_estimate=canonical_payload["delivery_estimate"],
            artifact_version_ids=canonical_payload["artifact_version_ids"],
            draft_hash=sha256_json(canonical_payload),
            created_by=_actor_id(actor),
        )
        session.add(draft)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="draft",
            entity_id=draft.id,
            event_type="draft.created",
            owner_user_id=draft.owner_user_id,
            project_id=draft.project_id,
            payload={"opportunity_id": str(opportunity.id), "version": version, "draft_hash": draft.draft_hash},
        )
        await self._flush_commit(session)
        return draft.to_safe_dict()

    create_application_draft = create_draft

    # ------------------------------------------------------------------
    # External action proposal and exact human decisions
    # ------------------------------------------------------------------

    @staticmethod
    def _require_media_models() -> None:
        if any(
            model is None
            for model in (
                ContentItem,
                ContentVariant,
                ContentVariantRevision,
                QAAssessment,
                RightsAssessment,
                PersonaRevision,
                PlatformAccount,
                PlatformAccountRevision,
            )
        ):
            raise OperationsValidationError("MediaOps content models are unavailable")

    @staticmethod
    def _require_provider_models() -> None:
        if MediaProviderCapabilitySnapshot is None:
            raise OperationsInvalidTransitionError(
                "MediaOps provider capability snapshots are unavailable"
            )

    @staticmethod
    def _media_account_revision_content_hash(revision: Any) -> str:
        """Recompute the immutable PlatformAccountRevision digest."""

        return sha256_json(
            {
                "display_name": revision.display_name,
                "publish_capability": revision.publish_capability,
                "media_capability": revision.media_capability,
                "analytics_capability": revision.analytics_capability,
                "credential_status": revision.credential_status,
                "remote_url": revision.remote_url,
                "locale": revision.locale,
                "timezone": revision.timezone,
                "supported_content_modes": list(
                    getattr(revision, "supported_content_modes_json", None) or []
                ),
                "disclosure_defaults": dict(
                    getattr(revision, "disclosure_defaults_json", None) or {}
                ),
                "rating_defaults": dict(
                    getattr(revision, "rating_defaults_json", None) or {}
                ),
                "adapter_ref": revision.adapter_ref,
            }
        )

    @staticmethod
    def _provider_observation(binding: Mapping[str, Any]) -> dict[str, Any]:
        """Build only the redacted observation accepted by the registry."""

        credential = binding.get("credential")
        account_revision = binding.get("account_revision")
        caps = getattr(credential, "capabilities", None)
        if not isinstance(caps, Mapping):
            caps = {}
        return {
            "status": str(getattr(credential, "status", "unknown") or "unknown"),
            "identity": str(caps.get("identity", "unknown") or "unknown"),
            "publish": str(caps.get("publish", "unknown") or "unknown"),
            "media": str(
                caps.get("media", getattr(account_revision, "media_capability", "unknown"))
                or "unknown"
            ),
            "analytics": str(
                caps.get(
                    "analytics",
                    getattr(account_revision, "analytics_capability", "unknown"),
                )
                or "unknown"
            ),
        }

    async def _assert_media_provider_readiness(
        self,
        session: Any,
        actor: Any,
        binding: Mapping[str, Any],
        *,
        action_type: str,
        payload: Mapping[str, Any],
        capability_snapshot_id: UUID | str | None,
        capability_snapshot_hash: str | None,
        adapter_key: str | None,
        adapter_version: str | None,
        credential_state_hash: str | None,
        execution_key: str | None,
    ) -> dict[str, Any]:
        """Validate the full provider execution authority tuple.

        This check is intentionally called both while proposing a provider
        action and immediately before starting its attempt.  It accepts only
        the redacted vault observation and immutable snapshot row; no provider
        request/response body is ever passed through this boundary.
        """

        await self._assert_human(actor)
        self._require_provider_models()
        account = binding.get("account")
        account_revision = binding.get("account_revision")
        credential = binding.get("credential")
        if account is None or account_revision is None or credential is None:
            raise OperationsInvalidTransitionError(
                "provider execution requires a pinned account, account revision, and verified credential"
            )

        snapshot_uuid = _as_uuid(capability_snapshot_id, "capability_snapshot_id")
        assert snapshot_uuid is not None
        snapshot_hash = _normalize_media_hash(
            capability_snapshot_hash,
            "capability_snapshot_hash",
            required=True,
        )
        adapter_key_value = _normalize_provider_adapter_value(
            adapter_key,
            "adapter_key",
            required=True,
            max_bytes=128,
        )
        adapter_version_value = _normalize_provider_adapter_value(
            adapter_version,
            "adapter_version",
            required=True,
            max_bytes=32,
        )
        credential_hash = _normalize_media_hash(
            credential_state_hash,
            "credential_state_hash",
            required=True,
        )
        execution_key_value = _text(
            execution_key,
            "execution_key",
            required=True,
            max_bytes=255,
        )
        assert execution_key_value is not None
        execution_key_value = execution_key_value.strip()
        if not execution_key_value:
            raise OperationsValidationError("execution_key must not be empty")

        snapshot = await self._get_or_404(
            session,
            MediaProviderCapabilitySnapshot,
            snapshot_uuid,
            "capability_snapshot_id",
        )
        await self._assert_entity_access(session, actor, snapshot, permission="read")
        expected_platform = str(binding["platform"])
        operation = _media_provider_operation(action_type, payload)
        if (
            snapshot.provider != expected_platform
            or snapshot.operation != operation
            or snapshot.platform_account_id != account.id
            or snapshot.account_revision_id != account_revision.id
            or snapshot.credential_id != credential.id
            or str(snapshot.account_type or "") != str(account.account_type or "")
            or snapshot.project_id != binding.get("project_id")
            or snapshot.owner_user_id != binding.get("owner_user_id")
        ):
            raise OperationsConflictError(
                "capability snapshot does not match the immutable MediaOps binding"
            )
        if snapshot_hash != str(snapshot.snapshot_hash or "").strip().lower():
            raise OperationsConflictError(
                "capability_snapshot_hash does not match the immutable snapshot"
            )
        if adapter_key_value != str(snapshot.adapter_key or ""):
            raise OperationsConflictError("adapter_key does not match capability snapshot")
        if adapter_version_value != str(snapshot.adapter_version or ""):
            raise OperationsConflictError("adapter_version does not match capability snapshot")

        current_credential_hash = str(binding.get("credential_state_hash") or "").lower()
        if not current_credential_hash or not hmac.compare_digest(
            credential_hash,
            current_credential_hash,
        ):
            raise OperationsConflictError(
                "credential state changed since the capability snapshot was observed"
            )
        if not hmac.compare_digest(
            str(snapshot.credential_state_hash or "").lower(),
            current_credential_hash,
        ):
            raise OperationsConflictError(
                "capability snapshot credential state is stale"
            )
        if int(snapshot.credential_revision or 0) != int(credential.revision or 0):
            raise OperationsConflictError("credential revision changed since the capability snapshot")
        if int(snapshot.account_revision or 0) != int(account_revision.version or 0):
            raise OperationsConflictError("account revision changed since the capability snapshot")
        current_account_revision_hash = self._media_account_revision_content_hash(account_revision)
        if str(account_revision.content_hash or "").lower() != current_account_revision_hash:
            raise OperationsConflictError("PlatformAccountRevision hash is invalid")
        # ContentVariantRevision pins the account-revision digest as part of
        # its immutable publication binding.  Verify that relationship again
        # at the provider boundary; an attacker must not be able to keep the
        # same revision id/version while swapping the account capability row.
        revision_account_hash = getattr(
            binding.get("revision"),
            "platform_account_revision_hash",
            None,
        )
        if revision_account_hash is None or not hmac.compare_digest(
            str(revision_account_hash).lower(),
            current_account_revision_hash,
        ):
            raise OperationsConflictError(
                "ContentVariantRevision account binding hash is stale"
            )

        policy = get_provider_capability(expected_platform) if callable(get_provider_capability) else None
        if policy is None or not callable(effective_operation_status):
            raise OperationsInvalidTransitionError(
                "provider capability registry is unavailable"
            )
        effective = effective_operation_status(
            expected_platform,
            operation,
            observation=self._provider_observation(binding),
        )
        effective_value = getattr(effective, "value", str(effective)).strip().lower()
        if effective_value != "automatable":
            raise OperationsInvalidTransitionError(
                f"provider operation is not automatable ({effective_value})"
            )
        if str(snapshot.status or "").strip().lower() != "automatable":
            raise OperationsInvalidTransitionError(
                "capability snapshot is not automatable"
            )
        if str(snapshot.account_eligibility or "").strip().lower() != "eligible":
            raise OperationsInvalidTransitionError(
                "provider account eligibility is not verified"
            )
        if str(snapshot.registry_version or "") != str(getattr(policy, "registry_version", "")):
            raise OperationsConflictError("provider capability registry version changed")
        current_adapter_key = getattr(policy, "adapter_key", None) or getattr(
            policy,
            "adapter_ref",
            None,
        )
        if str(current_adapter_key or "") != adapter_key_value:
            raise OperationsConflictError("provider adapter key is not current")
        # The registry release and adapter implementation are separate pins.
        # A policy may omit an implementation version while no adapter is
        # verified; when present, the implementation pin is exact and is
        # never conflated with ``registry_version``.
        current_adapter_version = getattr(policy, "adapter_version", None)
        if current_adapter_version is not None and str(current_adapter_version) != adapter_version_value:
            raise OperationsConflictError("provider adapter version is not current")

        return {
            "snapshot": snapshot,
            "operation": operation,
            "capability_snapshot_id": snapshot_uuid,
            "capability_snapshot_hash": snapshot_hash,
            "adapter_key": adapter_key_value,
            "adapter_version": adapter_version_value,
            "credential_state_hash": credential_hash,
            "execution_key": execution_key_value,
        }

    async def _load_media_binding(
        self,
        session: Any,
        actor: Any,
        *,
        content_item_id: UUID | str,
        content_variant_id: UUID | str,
        content_variant_revision_id: UUID | str,
        platform: Any,
        persona_revision_id: UUID | str,
        platform_account_id: UUID | str | None,
        platform_account_revision_id: UUID | str | None,
        connection_id: UUID | str,
        project_id: UUID | str | None = None,
        content_item_hash: str | None = None,
        content_variant_hash: str | None = None,
        content_variant_revision_hash: str | None = None,
        persona_revision_hash: str | None = None,
        platform_account_revision_hash: str | None = None,
        content_variant_revision_version: int | None = None,
    ) -> dict[str, Any]:
        """Load and cross-check every immutable MediaOps proposal binding."""

        self._require_media_models()
        platform_value = _normalize_media_platform(platform)
        item = await self._get_or_404(session, ContentItem, content_item_id, "content_item_id")
        variant = await self._get_or_404(session, ContentVariant, content_variant_id, "content_variant_id")
        revision = await self._get_or_404(
            session,
            ContentVariantRevision,
            content_variant_revision_id,
            "content_variant_revision_id",
        )
        persona_revision = await self._get_or_404(
            session,
            PersonaRevision,
            persona_revision_id,
            "persona_revision_id",
        )
        connection = await self._get_or_404(session, ExternalConnection, connection_id, "connection_id")

        for row, label, permission in (
            (item, "content item", "write"),
            (variant, "content variant", "write"),
            (revision, "content variant revision", "read"),
            (persona_revision, "persona revision", "read"),
            (connection, "connection", "write"),
        ):
            try:
                await self._assert_entity_access(session, actor, row, permission=permission)
            except OperationsAuthorizationError:
                raise
            except Exception as exc:
                raise OperationsAuthorizationError(f"{label} access denied") from exc

        project_uuid = _as_uuid(project_id, "project_id", required=False)
        if project_uuid is not None and project_uuid != getattr(item, "project_id", None):
            raise OperationsValidationError("project_id does not match ContentItem")
        project_uuid = getattr(item, "project_id", None)
        owner_id = getattr(item, "owner_user_id", None)

        def _same_scope(row: Any, label: str) -> None:
            if getattr(row, "owner_user_id", None) != owner_id or getattr(row, "project_id", None) != project_uuid:
                raise OperationsValidationError(f"{label} owner/project scope does not match ContentItem")

        _same_scope(variant, "ContentVariant")
        _same_scope(revision, "ContentVariantRevision")
        _same_scope(persona_revision, "PersonaRevision")
        if getattr(connection, "project_id", None) != project_uuid:
            raise OperationsValidationError("connection and ContentItem project_id must match")
        if project_uuid is None and getattr(connection, "owner_user_id", None) != owner_id:
            raise OperationsAuthorizationError("connection owner mismatch")
        if str(getattr(connection, "provider_key", "") or "").strip().lower() != platform_value:
            raise OperationsValidationError("connection provider does not match proposal platform")

        if getattr(variant, "content_item_id", None) != getattr(item, "id", None):
            raise OperationsValidationError("ContentVariant does not belong to ContentItem")
        if getattr(variant, "platform", None) != platform_value:
            raise OperationsValidationError("ContentVariant platform does not match proposal")
        if getattr(revision, "content_variant_id", None) != getattr(variant, "id", None):
            raise OperationsValidationError("ContentVariantRevision does not belong to ContentVariant")
        if getattr(revision, "content_item_id", None) != getattr(item, "id", None):
            raise OperationsValidationError("ContentVariantRevision content item does not match proposal")
        if getattr(revision, "platform", None) != platform_value:
            raise OperationsValidationError("ContentVariantRevision platform does not match proposal")
        if content_variant_revision_version is not None:
            try:
                expected_revision_version = int(content_variant_revision_version)
            except (TypeError, ValueError) as exc:
                raise OperationsValidationError(
                    "content_variant_revision_version must be an integer"
                ) from exc
            if expected_revision_version != int(getattr(revision, "version", 0) or 0):
                raise OperationsConflictError(
                    "content_variant_revision_version does not match immutable revision"
                )
        if getattr(revision, "persona_revision_id", None) != getattr(persona_revision, "id", None):
            raise OperationsValidationError("PersonaRevision does not match ContentVariantRevision")

        account = None
        account_revision = None
        credential = None
        credential_state_hash = None
        if platform_account_id is not None:
            account = await self._get_or_404(session, PlatformAccount, platform_account_id, "platform_account_id")
            await self._assert_entity_access(session, actor, account, permission="read")
            _same_scope(account, "PlatformAccount")
            if getattr(account, "platform", None) != platform_value:
                raise OperationsValidationError("PlatformAccount platform does not match proposal")
            # A publication target must use the exact connection bound to the
            # stable PlatformAccount.  Accepting any same-project connection
            # would let a proposal silently cross-post to another account.
            if getattr(account, "connection_id", None) is None:
                raise OperationsValidationError(
                    "PlatformAccount connection binding is required for publication"
                )
            if getattr(account, "connection_id", None) != getattr(connection, "id", None):
                raise OperationsValidationError(
                    "PlatformAccount connection binding does not match proposal"
                )
            if str(getattr(account, "status", "active") or "").strip().lower() != "active":
                raise OperationsInvalidTransitionError(
                    "paused PlatformAccount cannot be published"
                )
            # The remote provider identity is execution authority even for a
            # legacy opaque/non-vault connection.  Do not let publication
            # silently target a different account than the stable identity.
            if getattr(connection, "remote_account_ref", None) != getattr(account, "account_ref", None):
                raise OperationsInvalidTransitionError(
                    "PlatformAccount connection remote account does not match"
                )
            if getattr(revision, "platform_account_id", None) != getattr(account, "id", None):
                raise OperationsValidationError("PlatformAccount does not match ContentVariantRevision")
            if _is_media_vault_reference(getattr(connection, "credential_ref", None)):
                credential = await self._scalar(
                    session,
                    select(MediaPlatformCredential)
                    .where(MediaPlatformCredential.platform_account_id == account.id)
                    .limit(1),
                )
                expected_state_hash = media_credential_state_hash(
                    revision=int(getattr(credential, "revision", 0) or 0),
                    status=getattr(credential, "status", None),
                    connection_type=getattr(credential, "connection_type", None),
                    payload_digest=getattr(credential, "payload_digest", None),
                    capabilities=(
                        getattr(credential, "capabilities", {})
                        if isinstance(getattr(credential, "capabilities", {}), dict)
                        else {}
                    ),
                    verification_code=getattr(credential, "verification_code", None),
                    encryption_key_id=getattr(credential, "encryption_key_id", None),
                ) if credential is not None else None
                try:
                    embedded_key_id = media_credential_ciphertext_key_id(
                        getattr(credential, "encrypted_payload", None)
                    ) if credential is not None else None
                except MediaCredentialCryptoError:
                    embedded_key_id = None
                payload_integrity_ok = False
                if credential is not None:
                    try:
                        payload = decrypt_media_credential(
                            credential.encrypted_payload,
                            credential_id=credential.id,
                            platform_account_id=account.id,
                        )
                        if isinstance(payload, dict):
                            payload_digest = hashlib.sha256(canonical_payload(payload)).hexdigest()
                            payload_integrity_ok = hmac.compare_digest(
                                payload_digest,
                                str(getattr(credential, "payload_digest", "") or ""),
                            )
                    except MediaCredentialCryptoError:
                        payload_integrity_ok = False
                credential_state_hash = expected_state_hash
                if (
                    credential is None
                    or credential.connection_id != connection.id
                    or credential.owner_user_id != account.owner_user_id
                    or credential.project_id != account.project_id
                    or connection.owner_user_id != account.owner_user_id
                    or connection.project_id != account.project_id
                    or str(connection.provider_key or "").strip().lower() != platform_value
                    or str(connection.remote_account_ref or "") != str(account.account_ref or "")
                    or connection.credential_ref != f"credential://media-platform/{credential.id}"
                    or not credential.state_hash
                    or not hmac.compare_digest(str(credential.state_hash), str(expected_state_hash or ""))
                    or embedded_key_id is None
                    or not hmac.compare_digest(
                        str(embedded_key_id),
                        str(getattr(credential, "encryption_key_id", None) or ""),
                    )
                    or not payload_integrity_ok
                    or str(credential.status).strip().lower() != "verified"
                    or not isinstance(getattr(credential, "capabilities", None), dict)
                    or str(credential.capabilities.get("identity", "unknown")).strip().lower() != "available"
                    or str(getattr(credential, "verification_code", "") or "").strip().lower() != "identity_match"
                ):
                    raise OperationsInvalidTransitionError(
                        "Media credential must be provider-verified before publication"
                    )
        elif getattr(revision, "platform_account_id", None) is not None:
            raise OperationsValidationError("platform_account_id is required by ContentVariantRevision")

        if platform_account_revision_id is not None:
            if account is None:
                raise OperationsValidationError("platform_account_id is required with platform_account_revision_id")
            account_revision = await self._get_or_404(
                session,
                PlatformAccountRevision,
                platform_account_revision_id,
                "platform_account_revision_id",
            )
            await self._assert_entity_access(session, actor, account_revision, permission="read")
            _same_scope(account_revision, "PlatformAccountRevision")
            if getattr(account_revision, "platform_account_id", None) != getattr(account, "id", None):
                raise OperationsValidationError("PlatformAccountRevision does not belong to PlatformAccount")
            if getattr(revision, "platform_account_revision_id", None) != getattr(account_revision, "id", None):
                raise OperationsValidationError("PlatformAccountRevision does not match ContentVariantRevision")
        elif getattr(revision, "platform_account_revision_id", None) is not None:
            raise OperationsValidationError("platform_account_revision_id is required by ContentVariantRevision")

        expected_hashes = {
            "content_item_hash": (content_item_hash, getattr(item, "content_hash", None)),
            "content_variant_hash": (content_variant_hash, getattr(variant, "create_hash", None)),
            "content_variant_revision_hash": (
                content_variant_revision_hash,
                getattr(revision, "content_hash", None),
            ),
            "persona_revision_hash": (
                persona_revision_hash,
                getattr(revision, "persona_revision_hash", None) or getattr(persona_revision, "content_hash", None),
            ),
            "platform_account_revision_hash": (
                platform_account_revision_hash,
                getattr(revision, "platform_account_revision_hash", None)
                or (getattr(account_revision, "content_hash", None) if account_revision is not None else None),
            ),
        }
        for label, (supplied, actual) in expected_hashes.items():
            if supplied is None:
                continue
            supplied_value = _normalize_media_hash(supplied, label, required=True)
            actual_value = _normalize_media_hash(actual, label, required=True)
            if supplied_value != actual_value:
                raise OperationsConflictError(f"{label} does not match immutable MediaOps row")

        return {
            "item": item,
            "variant": variant,
            "revision": revision,
            "persona_revision": persona_revision,
            "account": account,
            "account_revision": account_revision,
            "credential": credential,
            "credential_state_hash": credential_state_hash,
            "connection": connection,
            "platform": platform_value,
            "project_id": project_uuid,
            "owner_user_id": owner_id,
        }

    async def _assert_media_publication_readiness(
        self,
        session: Any,
        actor: Any,
        binding: Mapping[str, Any],
    ) -> None:
        """Require the exact current revision to have human QA/Rights clearance.

        Media publication proposals are still proposal-only, but they must not
        be created from an old or unreviewed revision.  Readiness is evaluated
        from the append-only assessment ledgers rather than trusting caller
        supplied ``qa``/``rights`` metadata.  A changed/tampered revision or
        stale assessment therefore fails closed before an ExternalAction row
        is written.
        """

        revision = binding["revision"]
        variant = binding["variant"]
        if binding.get("account") is None or binding.get("account_revision") is None:
            raise OperationsInvalidTransitionError(
                "MediaOps publication requires a pinned PlatformAccountRevision"
            )
        # A proposal must target the variant's current (highest) immutable
        # revision.  Revisions are append-only and assessments never transfer.
        latest_version = await self._scalar(
            session,
            select(func.max(ContentVariantRevision.version)).where(
                ContentVariantRevision.content_variant_id == variant.id
            ),
        )
        if latest_version is None or int(revision.version or 0) != int(latest_version):
            raise OperationsInvalidTransitionError(
                "MediaOps publication requires the current ContentVariantRevision"
            )
        if getattr(revision, "content_hash", None) is None:
            raise OperationsConflictError("MediaOps revision hash is missing")
        if _media_revision_content_hash(revision) != str(revision.content_hash).lower():
            raise OperationsConflictError("MediaOps revision hash is invalid")

        async def latest(model: Any) -> Any | None:
            return await self._scalar(
                session,
                select(model)
                .where(model.content_variant_revision_id == revision.id)
                .order_by(model.created_at.desc(), model.id.desc())
                .limit(1),
            )

        qa = await latest(QAAssessment)
        rights = await latest(RightsAssessment)
        blockers: list[str] = []
        if qa is None:
            blockers.append("qa_missing")
        elif getattr(qa, "content_variant_id", None) != getattr(variant, "id", None):
            blockers.append("qa_variant_mismatch")
        elif qa.revision_hash != revision.content_hash:
            blockers.append("qa_revision_mismatch")
        elif getattr(qa, "assessment_hash", None) != _media_assessment_hash(qa, revision):
            blockers.append("qa_hash_invalid")
        elif str(qa.result).strip().lower() != "passed":
            blockers.append(f"qa_{qa.result}")
        if rights is None:
            blockers.append("rights_missing")
        elif getattr(rights, "content_variant_id", None) != getattr(variant, "id", None):
            blockers.append("rights_variant_mismatch")
        elif rights.revision_hash != revision.content_hash:
            blockers.append("rights_revision_mismatch")
        elif getattr(rights, "assessment_hash", None) != _media_assessment_hash(rights, revision):
            blockers.append("rights_hash_invalid")
        elif str(rights.result).strip().lower() != "cleared":
            blockers.append(f"rights_{rights.result}")
        if blockers:
            raise OperationsInvalidTransitionError(
                "MediaOps publication readiness is blocked: " + ", ".join(blockers)
            )

    async def _normalize_media_typed_payload(
        self,
        payload: Any,
        platform: str,
    ) -> dict[str, Any]:
        """Use the WS5 closed typed payload normalizer when available."""

        if not isinstance(payload, Mapping):
            raise OperationsValidationError("payload must be a typed object")
        try:
            from .media_operations_content_service import _normalize_payload

            normalized, _refs = _normalize_payload(payload, platform)
            return normalized
        except ImportError:  # pragma: no cover - optional legacy installation
            data = {str(key): value for key, value in payload.items()}
            _reject_protected_input_keys(data, field="payload")
            if str(data.get("platform") or platform).strip().lower() != platform:
                raise OperationsValidationError("payload platform does not match proposal")
            if not data.get("type"):
                raise OperationsValidationError("payload.type is required")
            return _json_safe(data)

    def _media_action_payload(
        self,
        binding: Mapping[str, Any],
        *,
        action_type: str,
        payload: Mapping[str, Any],
        artifact_hashes: Sequence[str],
        qa: Mapping[str, Any],
        rights: Mapping[str, Any],
        schedule: Mapping[str, Any],
        adapter_target: Mapping[str, Any],
        execution_mode: str = "manual",
        capability_snapshot_id: UUID | str | None = None,
        capability_snapshot_hash: str | None = None,
        adapter_key: str | None = None,
        adapter_version: str | None = None,
        credential_state_hash: str | None = None,
        execution_key: str | None = None,
    ) -> dict[str, Any]:
        item = binding["item"]
        variant = binding["variant"]
        revision = binding["revision"]
        persona_revision = binding["persona_revision"]
        account = binding.get("account")
        account_revision = binding.get("account_revision")
        connection = binding["connection"]
        return {
            "operation_key": action_type,
            "action_type": action_type,
            "platform": binding["platform"],
            "connection_id": str(connection.id),
            "connection": _connection_snapshot(connection),
            "content_item_id": str(item.id),
            "content_item_hash": str(item.content_hash).lower(),
            "content_variant_id": str(variant.id),
            "content_variant_hash": str(variant.create_hash).lower(),
            "content_variant_revision_id": str(revision.id),
            "content_variant_revision_version": int(revision.version),
            "content_variant_revision_hash": str(revision.content_hash).lower(),
            "persona_revision_id": str(persona_revision.id),
            "persona_revision_hash": str(
                getattr(revision, "persona_revision_hash", None)
                or getattr(persona_revision, "content_hash", None)
            ).lower(),
            "platform_account_id": str(account.id) if account is not None else None,
            "platform_account_revision_id": str(account_revision.id) if account_revision is not None else None,
            "platform_account_revision_hash": (
                str(getattr(revision, "platform_account_revision_hash", None) or account_revision.content_hash).lower()
                if account_revision is not None
                else None
            ),
            "payload": dict(payload),
            "artifact_hashes": list(artifact_hashes),
            "qa": dict(qa),
            "rights": dict(rights),
            "schedule": dict(schedule),
            "adapter_target": dict(adapter_target),
            "execution_mode": execution_mode,
            "capability_snapshot_id": (
                str(capability_snapshot_id)
                if capability_snapshot_id is not None
                else None
            ),
            "capability_snapshot_hash": capability_snapshot_hash,
            "adapter_key": adapter_key,
            "adapter_version": adapter_version,
            "credential_state_hash": credential_state_hash,
            "execution_key": execution_key,
        }

    async def create_media_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        action_type: str | None = None,
        operation_key: str | None = None,
        content_item_id: UUID | str,
        content_variant_id: UUID | str,
        content_variant_revision_id: UUID | str,
        persona_revision_id: UUID | str,
        connection_id: UUID | str,
        idempotency_key: str,
        platform: Any,
        payload: Mapping[str, Any],
        artifact_hashes: Sequence[Any] | None = None,
        platform_account_id: UUID | str | None = None,
        platform_account_revision_id: UUID | str | None = None,
        project_id: UUID | str | None = None,
        content_item_hash: str | None = None,
        content_variant_hash: str | None = None,
        content_variant_revision_hash: str | None = None,
        variant_revision_hash: str | None = None,
        persona_revision_hash: str | None = None,
        platform_account_revision_hash: str | None = None,
        content_variant_revision_version: int | None = None,
        variant_revision_version: int | None = None,
        payload_hash: str | None = None,
        qa: Mapping[str, Any] | None = None,
        rights: Mapping[str, Any] | None = None,
        schedule: Mapping[str, Any] | None = None,
        adapter_target: Mapping[str, Any] | None = None,
        execution_mode: str = "manual",
        capability_snapshot_id: UUID | str | None = None,
        capability_snapshot_hash: str | None = None,
        adapter_key: str | None = None,
        adapter_version: str | None = None,
        credential_state_hash: str | None = None,
        execution_key: str | None = None,
        origin_agent_id: UUID | str | None = None,
        origin_agent_run_id: UUID | str | None = None,
        origin_work_item_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Create an idempotent, typed MediaOps action proposal.

        This method records intent only.  It never invokes a provider; the
        existing human approval/attempt/reconcile methods own later authority.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        origin_agent_uuid, origin_run_uuid, origin_work_item_uuid = await _validate_typed_origin(
            session,
            origin_agent_id=origin_agent_id,
            origin_agent_run_id=origin_agent_run_id,
            origin_work_item_id=origin_work_item_id,
        )
        normalized_action_type = _normalize_media_action_type(action_type or operation_key)
        key = _text(idempotency_key, "idempotency_key", required=True, max_bytes=255)
        platform_value = _normalize_media_platform(platform)
        artifact_values = _hash_list(artifact_hashes)
        binding = await self._load_media_binding(
            session,
            actor,
            content_item_id=content_item_id,
            content_variant_id=content_variant_id,
            content_variant_revision_id=content_variant_revision_id,
            platform=platform_value,
            persona_revision_id=persona_revision_id,
            platform_account_id=platform_account_id,
            platform_account_revision_id=platform_account_revision_id,
            connection_id=connection_id,
            project_id=project_id,
            content_item_hash=content_item_hash,
            content_variant_hash=content_variant_hash,
            content_variant_revision_hash=content_variant_revision_hash or variant_revision_hash,
            persona_revision_hash=persona_revision_hash,
            platform_account_revision_hash=platform_account_revision_hash,
            content_variant_revision_version=(
                content_variant_revision_version or variant_revision_version
            ),
        )
        await self._assert_media_publication_readiness(session, actor, binding)
        normalized_payload = await self._normalize_media_typed_payload(payload, platform_value)
        execution_mode_value = _normalize_media_execution_mode(execution_mode)
        if execution_mode_value == "provider":
            provider_authority = await self._assert_media_provider_readiness(
                session,
                actor,
                binding,
                action_type=normalized_action_type,
                payload=normalized_payload,
                capability_snapshot_id=capability_snapshot_id,
                capability_snapshot_hash=capability_snapshot_hash,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                credential_state_hash=credential_state_hash,
                execution_key=execution_key or key,
            )
            capability_snapshot_id = provider_authority["capability_snapshot_id"]
            capability_snapshot_hash = provider_authority["capability_snapshot_hash"]
            adapter_key = provider_authority["adapter_key"]
            adapter_version = provider_authority["adapter_version"]
            credential_state_hash = provider_authority["credential_state_hash"]
            execution_key = provider_authority["execution_key"]
        else:
            # Provider-only evidence must never be accepted on a manual action
            # and later activated by mutating one JSON field after approval.
            if any(
                value is not None
                for value in (
                    capability_snapshot_id,
                    capability_snapshot_hash,
                    adapter_key,
                    adapter_version,
                    credential_state_hash,
                )
            ):
                raise OperationsValidationError(
                    "provider capability evidence is only valid for provider execution"
                )
            execution_key = _text(execution_key, "execution_key", max_bytes=255)
        qa_value = _normalized_media_mapping(qa, "qa", default={"status": "unknown"})
        rights_value = _normalized_media_mapping(rights, "rights", default={"status": "unknown"})
        schedule_value = _normalized_media_mapping(schedule, "schedule")
        adapter_value = _normalized_media_mapping(
            adapter_target if adapter_target else None,
            "adapter_target",
            default=(
                {
                    "platform": platform_value,
                    "status": "provider",
                    "mode": "provider",
                    "provider_calls": True,
                }
                if execution_mode_value == "provider"
                else MEDIA_ADAPTER_STATUS[platform_value]
            ),
        )
        adapter_platform = _normalize_media_platform(adapter_value.get("platform", platform_value))
        if adapter_platform != platform_value:
            raise OperationsValidationError("adapter_target platform does not match proposal")
        adapter_status = str(adapter_value.get("status") or "").strip().lower()
        if execution_mode_value == "manual":
            if adapter_status != "manual":
                raise OperationsValidationError("only the manual MediaOps adapter is available")
            if adapter_value.get("provider_calls", False) is not False:
                raise OperationsValidationError("MediaOps adapter provider calls are disabled")
            adapter_value = {
                **MEDIA_ADAPTER_STATUS[platform_value],
                **adapter_value,
                "platform": platform_value,
                "status": "manual",
                "mode": "manual",
                "provider_calls": False,
            }
        else:
            if adapter_status not in {"provider", "automatable", "verified"}:
                raise OperationsValidationError("provider adapter target is not executable")
            if adapter_value.get("provider_calls", False) is not True:
                raise OperationsValidationError("provider adapter target must explicitly enable provider calls")
            adapter_value = {
                "platform": platform_value,
                "status": "provider",
                "mode": "provider",
                "provider_calls": True,
                **adapter_value,
                "platform": platform_value,
                "status": "provider",
                "mode": "provider",
                "provider_calls": True,
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
            }
        action_payload = self._media_action_payload(
            binding,
            action_type=normalized_action_type,
            payload=normalized_payload,
            artifact_hashes=artifact_values,
            qa=qa_value,
            rights=rights_value,
            schedule=schedule_value,
            adapter_target=adapter_value,
            execution_mode=execution_mode_value,
            capability_snapshot_id=capability_snapshot_id,
            capability_snapshot_hash=capability_snapshot_hash,
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            credential_state_hash=credential_state_hash,
            execution_key=execution_key or (key if execution_mode_value == "provider" else None),
        )
        computed_hash = sha256_json(action_payload)
        supplied_hash = _normalize_media_hash(payload_hash, "payload_hash")
        if supplied_hash is not None and supplied_hash != computed_hash:
            raise OperationsConflictError("payload_hash does not match canonical MediaOps proposal")

        scope_conditions = [ExternalAction.idempotency_key == key]
        if binding["project_id"] is None:
            scope_conditions.extend(
                [
                    ExternalAction.project_id.is_(None),
                    ExternalAction.owner_user_id == _actor_id(actor),
                ]
            )
        else:
            scope_conditions.append(ExternalAction.project_id == binding["project_id"])
        existing = await self._scalar(session, select(ExternalAction).where(*scope_conditions).limit(1))
        if existing is not None:
            if (
                existing.action_type != normalized_action_type
                or existing.payload_hash != computed_hash
                or list(existing.artifact_hashes or []) != artifact_values
                or existing.origin_agent_id != origin_agent_uuid
                or existing.origin_agent_run_id != origin_run_uuid
                or existing.origin_work_item_id != origin_work_item_uuid
            ):
                raise OperationsConflictError("idempotency key is already bound to a different MediaOps proposal")
            return await self.get_action(session, actor, existing.id)

        action = ExternalAction(
            owner_user_id=binding["owner_user_id"],
            project_id=binding["project_id"],
            opportunity_id=None,
            source_url=None,
            source_snapshot_hash=None,
            connection_id=binding["connection"].id,
            application_draft_id=None,
            application_draft_version=1,
            action_type=normalized_action_type,
            idempotency_key=key,
            payload_json=action_payload,
            payload_hash=computed_hash,
            artifact_hashes=artifact_values,
            action_version=1,
            version=1,
            status="proposed",
            created_by=_actor_id(actor),
            content_item_id=binding["item"].id,
            content_variant_id=binding["variant"].id,
            content_variant_revision_id=binding["revision"].id,
            persona_revision_id=binding["persona_revision"].id,
            platform_account_id=binding["account"].id if binding["account"] is not None else None,
            platform_account_revision_id=binding["account_revision"].id if binding["account_revision"] is not None else None,
            platform=platform_value,
            execution_mode=execution_mode_value,
            capability_snapshot_id=(
                provider_authority["capability_snapshot_id"]
                if execution_mode_value == "provider"
                else None
            ),
            capability_snapshot_hash=(
                provider_authority["capability_snapshot_hash"]
                if execution_mode_value == "provider"
                else None
            ),
            adapter_key=(provider_authority["adapter_key"] if execution_mode_value == "provider" else None),
            adapter_version=(
                provider_authority["adapter_version"]
                if execution_mode_value == "provider"
                else None
            ),
            credential_state_hash=(
                provider_authority["credential_state_hash"]
                if execution_mode_value == "provider"
                else None
            ),
            execution_key=(
                provider_authority["execution_key"]
                if execution_mode_value == "provider"
                else execution_key
            ),
            origin_agent_id=origin_agent_uuid,
            origin_agent_run_id=origin_run_uuid,
            origin_work_item_id=origin_work_item_uuid,
        )
        session.add(action)
        try:
            await self._flush_only(session)
        except IntegrityError:
            await self._rollback(session)
            existing = await self._scalar(session, select(ExternalAction).where(*scope_conditions).limit(1))
            if existing is None:
                raise
            if (
                existing.action_type != normalized_action_type
                or existing.payload_hash != computed_hash
                or list(existing.artifact_hashes or []) != artifact_values
                or existing.origin_agent_id != origin_agent_uuid
                or existing.origin_agent_run_id != origin_run_uuid
                or existing.origin_work_item_id != origin_work_item_uuid
            ):
                raise OperationsConflictError("idempotency key is already bound to a different MediaOps proposal")
            return await self.get_action(session, actor, existing.id)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.proposed",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "action_type": normalized_action_type,
                "platform": platform_value,
                "action_version": 1,
                "version": 1,
                "payload_hash": computed_hash,
                "artifact_hashes": artifact_values,
            },
        )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    # Compatibility names used by API/repository adapters.
    propose_media_action = create_media_action
    create_media_proposal = create_media_action
    propose_media_publication = create_media_action
    create_media_publication = create_media_action
    create_media_operation = create_media_action

    async def _assert_media_action_integrity(
        self,
        session: Any,
        actor: Any,
        action: ExternalAction,
        *,
        require_readiness: bool = True,
    ) -> None:
        """Fail closed if an immutable MediaOps binding or payload was changed."""

        if action.action_type not in MEDIA_ACTION_TYPES:
            return
        raw = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        if not raw:
            raise OperationsConflictError("MediaOps action payload is missing")
        binding = await self._load_media_binding(
            session,
            actor,
            content_item_id=action.content_item_id,
            content_variant_id=action.content_variant_id,
            content_variant_revision_id=action.content_variant_revision_id,
            platform=action.platform or raw.get("platform"),
            persona_revision_id=action.persona_revision_id or raw.get("persona_revision_id"),
            platform_account_id=action.platform_account_id or raw.get("platform_account_id"),
            platform_account_revision_id=action.platform_account_revision_id or raw.get("platform_account_revision_id"),
            connection_id=action.connection_id,
            project_id=action.project_id,
            content_item_hash=raw.get("content_item_hash"),
            content_variant_hash=raw.get("content_variant_hash"),
            content_variant_revision_hash=raw.get("content_variant_revision_hash"),
            persona_revision_hash=raw.get("persona_revision_hash"),
            platform_account_revision_hash=raw.get("platform_account_revision_hash"),
            content_variant_revision_version=raw.get("content_variant_revision_version"),
        )
        execution_mode_value = _normalize_media_execution_mode(
            getattr(action, "execution_mode", None) or raw.get("execution_mode", "manual")
        )
        capability_snapshot_id_value = (
            getattr(action, "capability_snapshot_id", None)
            or raw.get("capability_snapshot_id")
        )
        capability_snapshot_hash_value = (
            getattr(action, "capability_snapshot_hash", None)
            or raw.get("capability_snapshot_hash")
        )
        adapter_key_value = getattr(action, "adapter_key", None) or raw.get("adapter_key")
        adapter_version_value = (
            getattr(action, "adapter_version", None) or raw.get("adapter_version")
        )
        credential_state_hash_value = (
            getattr(action, "credential_state_hash", None)
            or raw.get("credential_state_hash")
        )
        execution_key_value = getattr(action, "execution_key", None) or raw.get("execution_key")
        # A normal read/approve/attempt path must only operate on a currently
        # publishable revision.  During ``revise_media_action`` the old action
        # intentionally points at the previous revision, so its immutable
        # payload still needs validation but readiness is evaluated against
        # the replacement binding below.
        if require_readiness:
            await self._assert_media_publication_readiness(session, actor, binding)
        artifact_values = _hash_list(action.artifact_hashes)
        if artifact_values != list(action.artifact_hashes or []):
            raise OperationsConflictError("MediaOps artifact hashes are invalid")
        normalized_payload = await self._normalize_media_typed_payload(
            raw.get("payload"),
            binding["platform"],
        )
        qa_value = _normalized_media_mapping(raw.get("qa"), "qa", default={"status": "unknown"})
        rights_value = _normalized_media_mapping(raw.get("rights"), "rights", default={"status": "unknown"})
        schedule_value = _normalized_media_mapping(raw.get("schedule"), "schedule")
        adapter_value = _normalized_media_mapping(
            raw.get("adapter_target"),
            "adapter_target",
            default=(
                {
                    "platform": binding["platform"],
                    "status": "provider",
                    "mode": "provider",
                    "provider_calls": True,
                }
                if execution_mode_value == "provider"
                else MEDIA_ADAPTER_STATUS[binding["platform"]]
            ),
        )
        adapter_status = str(adapter_value.get("status") or "").strip().lower()
        if execution_mode_value == "manual":
            if (
                str(adapter_value.get("platform") or "").strip().lower() != binding["platform"]
                or adapter_status != "manual"
                or adapter_value.get("provider_calls", False) is not False
            ):
                raise OperationsConflictError("MediaOps adapter target is not the manual adapter")
        elif (
            str(adapter_value.get("platform") or "").strip().lower() != binding["platform"]
            or adapter_status not in {"provider", "automatable", "verified"}
            or adapter_value.get("provider_calls", False) is not True
        ):
            raise OperationsConflictError("MediaOps adapter target is not the provider adapter")
        canonical = self._media_action_payload(
            binding,
            action_type=action.action_type,
            payload=normalized_payload,
            artifact_hashes=artifact_values,
            qa=qa_value,
            rights=rights_value,
            schedule=schedule_value,
            adapter_target=(
                {
                    **MEDIA_ADAPTER_STATUS[binding["platform"]],
                    **adapter_value,
                    "platform": binding["platform"],
                    "status": "manual",
                    "mode": "manual",
                    "provider_calls": False,
                }
                if execution_mode_value == "manual"
                else {
                    **adapter_value,
                    "platform": binding["platform"],
                    "status": "provider",
                    "mode": "provider",
                    "provider_calls": True,
                    "adapter_key": adapter_key_value,
                    "adapter_version": adapter_version_value,
                }
            ),
            execution_mode=execution_mode_value,
            capability_snapshot_id=capability_snapshot_id_value,
            capability_snapshot_hash=(
                _normalize_media_hash(capability_snapshot_hash_value, "capability_snapshot_hash")
                if capability_snapshot_hash_value is not None
                else None
            ),
            adapter_key=adapter_key_value,
            adapter_version=adapter_version_value,
            credential_state_hash=(
                _normalize_media_hash(credential_state_hash_value, "credential_state_hash")
                if credential_state_hash_value is not None
                else None
            ),
            execution_key=execution_key_value,
        )
        expected_raw = canonical
        # Rows created before WS05-C do not carry the additive authority keys;
        # preserve their historical payload hash while normalizing the action
        # columns to the manual default.
        if "execution_mode" not in raw:
            for key in (
                "execution_mode",
                "capability_snapshot_id",
                "capability_snapshot_hash",
                "adapter_key",
                "adapter_version",
                "credential_state_hash",
                "execution_key",
            ):
                expected_raw.pop(key, None)
        if raw != expected_raw or action.payload_hash != sha256_json(expected_raw):
            raise OperationsConflictError("MediaOps payload or immutable binding hash changed")
        if execution_mode_value == "provider" and require_readiness:
            await self._assert_media_provider_readiness(
                session,
                actor,
                binding,
                action_type=action.action_type,
                payload=normalized_payload,
                capability_snapshot_id=capability_snapshot_id_value,
                capability_snapshot_hash=capability_snapshot_hash_value,
                adapter_key=adapter_key_value,
                adapter_version=adapter_version_value,
                credential_state_hash=credential_state_hash_value,
                execution_key=execution_key_value,
            )

    async def revise_media_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        expected_version: int,
        content_variant_revision_id: UUID | str,
        payload: Mapping[str, Any],
        artifact_hashes: Sequence[Any] | None = None,
        persona_revision_id: UUID | str | None = None,
        platform_account_revision_id: UUID | str | None = None,
        content_item_hash: str | None = None,
        content_variant_hash: str | None = None,
        content_variant_revision_hash: str | None = None,
        variant_revision_hash: str | None = None,
        persona_revision_hash: str | None = None,
        platform_account_revision_hash: str | None = None,
        content_variant_revision_version: int | None = None,
        variant_revision_version: int | None = None,
        payload_hash: str | None = None,
        qa: Mapping[str, Any] | None = None,
        rights: Mapping[str, Any] | None = None,
        schedule: Mapping[str, Any] | None = None,
        adapter_target: Mapping[str, Any] | None = None,
        execution_mode: str | None = None,
        capability_snapshot_id: UUID | str | None = None,
        capability_snapshot_hash: str | None = None,
        adapter_key: str | None = None,
        adapter_version: str | None = None,
        credential_state_hash: str | None = None,
        execution_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a new exact MediaOps revision and invalidate prior approval."""

        session = self._resolve_session(session)
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        if action.action_type not in MEDIA_ACTION_TYPES:
            raise OperationsValidationError("action is not a MediaOps action")
        await self._assert_entity_access(session, actor, action, permission="write")
        await self._check_expected_version(action, expected_version)
        await self._assert_media_action_integrity(
            session,
            actor,
            action,
            require_readiness=False,
        )
        raw = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        prior_execution_mode = _normalize_media_execution_mode(
            getattr(action, "execution_mode", None) or raw.get("execution_mode", "manual")
        )
        if execution_mode is not None and _normalize_media_execution_mode(execution_mode) != prior_execution_mode:
            raise OperationsConflictError("execution_mode cannot change across an action revision")
        if prior_execution_mode == "provider":
            capability_snapshot_id = capability_snapshot_id or getattr(action, "capability_snapshot_id", None) or raw.get("capability_snapshot_id")
            capability_snapshot_hash = capability_snapshot_hash or getattr(action, "capability_snapshot_hash", None) or raw.get("capability_snapshot_hash")
            adapter_key = adapter_key or getattr(action, "adapter_key", None) or raw.get("adapter_key")
            adapter_version = adapter_version or getattr(action, "adapter_version", None) or raw.get("adapter_version")
            credential_state_hash = credential_state_hash or getattr(action, "credential_state_hash", None) or raw.get("credential_state_hash")
            execution_key = execution_key or getattr(action, "execution_key", None) or raw.get("execution_key")
        elif any(value is not None for value in (capability_snapshot_id, capability_snapshot_hash, adapter_key, adapter_version, credential_state_hash)):
            raise OperationsValidationError("provider capability evidence is only valid for provider execution")
        binding = await self._load_media_binding(
            session,
            actor,
            content_item_id=action.content_item_id,
            content_variant_id=action.content_variant_id,
            content_variant_revision_id=content_variant_revision_id,
            platform=action.platform or raw.get("platform"),
            persona_revision_id=persona_revision_id or action.persona_revision_id or raw.get("persona_revision_id"),
            platform_account_id=action.platform_account_id or raw.get("platform_account_id"),
            platform_account_revision_id=platform_account_revision_id or action.platform_account_revision_id or raw.get("platform_account_revision_id"),
            connection_id=action.connection_id,
            project_id=action.project_id,
            content_item_hash=content_item_hash or raw.get("content_item_hash"),
            content_variant_hash=content_variant_hash or raw.get("content_variant_hash"),
            content_variant_revision_hash=content_variant_revision_hash or variant_revision_hash,
            persona_revision_hash=persona_revision_hash,
            platform_account_revision_hash=platform_account_revision_hash,
            content_variant_revision_version=(
                content_variant_revision_version or variant_revision_version
            ),
        )
        await self._assert_media_publication_readiness(session, actor, binding)
        artifact_values = _hash_list(artifact_hashes if artifact_hashes is not None else action.artifact_hashes)
        normalized_payload = await self._normalize_media_typed_payload(payload, binding["platform"])
        provider_authority = None
        if prior_execution_mode == "provider":
            provider_authority = await self._assert_media_provider_readiness(
                session,
                actor,
                binding,
                action_type=action.action_type,
                payload=normalized_payload,
                capability_snapshot_id=capability_snapshot_id,
                capability_snapshot_hash=capability_snapshot_hash,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                credential_state_hash=credential_state_hash,
                execution_key=execution_key,
            )
        qa_value = _normalized_media_mapping(qa if qa is not None else raw.get("qa"), "qa", default={"status": "unknown"})
        rights_value = _normalized_media_mapping(rights if rights is not None else raw.get("rights"), "rights", default={"status": "unknown"})
        schedule_value = _normalized_media_mapping(schedule if schedule is not None else raw.get("schedule"), "schedule")
        adapter_input = adapter_target if adapter_target else raw.get("adapter_target")
        adapter_value = _normalized_media_mapping(
            adapter_input if adapter_input else None,
            "adapter_target",
            default=(
                {
                    "platform": binding["platform"],
                    "status": "provider",
                    "mode": "provider",
                    "provider_calls": True,
                }
                if prior_execution_mode == "provider"
                else MEDIA_ADAPTER_STATUS[binding["platform"]]
            ),
        )
        adapter_status = str(adapter_value.get("status") or "").strip().lower()
        if prior_execution_mode == "manual":
            if adapter_status != "manual" or adapter_value.get("provider_calls", False) is not False:
                raise OperationsValidationError("only the manual MediaOps adapter is available")
            adapter_value = {
                **MEDIA_ADAPTER_STATUS[binding["platform"]],
                **adapter_value,
                "platform": binding["platform"],
                "status": "manual",
                "mode": "manual",
                "provider_calls": False,
            }
        else:
            if adapter_status not in {"provider", "automatable", "verified"} or adapter_value.get("provider_calls", False) is not True:
                raise OperationsValidationError("provider adapter target is not executable")
            adapter_value = {
                **adapter_value,
                "platform": binding["platform"],
                "status": "provider",
                "mode": "provider",
                "provider_calls": True,
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
            }
        action_payload = self._media_action_payload(
            binding,
            action_type=action.action_type,
            payload=normalized_payload,
            artifact_hashes=artifact_values,
            qa=qa_value,
            rights=rights_value,
            schedule=schedule_value,
            adapter_target=adapter_value,
            execution_mode=prior_execution_mode,
            capability_snapshot_id=(
                provider_authority["capability_snapshot_id"]
                if provider_authority is not None
                else None
            ),
            capability_snapshot_hash=(
                provider_authority["capability_snapshot_hash"]
                if provider_authority is not None
                else None
            ),
            adapter_key=(provider_authority["adapter_key"] if provider_authority is not None else None),
            adapter_version=(
                provider_authority["adapter_version"]
                if provider_authority is not None
                else None
            ),
            credential_state_hash=(
                provider_authority["credential_state_hash"]
                if provider_authority is not None
                else None
            ),
            execution_key=(
                provider_authority["execution_key"]
                if provider_authority is not None
                else None
            ),
        )
        computed_hash = sha256_json(action_payload)
        supplied_hash = _normalize_media_hash(payload_hash, "payload_hash")
        if supplied_hash is not None and supplied_hash != computed_hash:
            raise OperationsConflictError("payload_hash does not match canonical MediaOps proposal")
        previous = {
            "action_version": int(action.action_version or 1),
            "payload_hash": action.payload_hash,
            "content_variant_revision_id": str(action.content_variant_revision_id),
        }
        if int(binding["revision"].version) <= int(raw.get("content_variant_revision_version") or 0):
            raise OperationsConflictError("revision must reference a newer ContentVariantRevision version")
        prior_approvals = await self._scalars(
            session,
            select(ExternalActionApproval).where(
                ExternalActionApproval.action_id == action.id,
                ExternalActionApproval.action_version == previous["action_version"],
                ExternalActionApproval.decision == "approved",
            ),
        )
        for approval in prior_approvals:
            session.add(
                ExternalActionApproval(
                    action_id=action.id,
                    owner_user_id=action.owner_user_id,
                    action_version=approval.action_version,
                    payload_hash=approval.payload_hash,
                    artifact_hashes=list(approval.artifact_hashes or []),
                    decision="invalidated",
                    reason="MediaOps proposal revised",
                    decided_by=_actor_id(actor),
                )
            )
        action.content_variant_revision_id = binding["revision"].id
        action.persona_revision_id = binding["persona_revision"].id
        action.platform_account_id = binding["account"].id if binding["account"] is not None else None
        action.platform_account_revision_id = binding["account_revision"].id if binding["account_revision"] is not None else None
        action.execution_mode = prior_execution_mode
        action.capability_snapshot_id = (
            provider_authority["capability_snapshot_id"]
            if provider_authority is not None
            else None
        )
        action.capability_snapshot_hash = (
            provider_authority["capability_snapshot_hash"]
            if provider_authority is not None
            else None
        )
        action.adapter_key = provider_authority["adapter_key"] if provider_authority is not None else None
        action.adapter_version = (
            provider_authority["adapter_version"]
            if provider_authority is not None
            else None
        )
        action.credential_state_hash = (
            provider_authority["credential_state_hash"]
            if provider_authority is not None
            else None
        )
        action.execution_key = (
            provider_authority["execution_key"]
            if provider_authority is not None
            else None
        )
        action.payload_json = action_payload
        action.payload_hash = computed_hash
        action.artifact_hashes = artifact_values
        action.action_version = int(action.action_version or 1) + 1
        action.version = int(action.version or 1) + 1
        action.status = "proposed"
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.revised",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "action_type": action.action_type,
                "previous": previous,
                "action_version": action.action_version,
                "version": action.version,
                "payload_hash": computed_hash,
                "invalidated_approval_count": len(prior_approvals),
            },
        )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    revise_media_proposal = revise_media_action

    async def list_media_actions(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        action_type: str | None = None,
        platform: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        action_type_value = _normalize_media_action_type(action_type) if action_type else None
        platform_value = _normalize_media_platform(platform) if platform else None
        page_limit, page_offset = _bounded_page(limit, offset)
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        if project_uuid is not None:
            await self._assert_access(session, actor, project_id=project_uuid, permission="read")
            scope_condition = ExternalAction.project_id == project_uuid
        else:
            scope_condition = await self._scope_less_condition(session, actor, ExternalAction)
        conditions: list[Any] = [scope_condition, ExternalAction.action_type.in_(tuple(MEDIA_ACTION_TYPES))]
        if action_type_value:
            conditions.append(ExternalAction.action_type == action_type_value)
        if platform_value:
            conditions.append(ExternalAction.platform == platform_value)
        if status:
            conditions.append(ExternalAction.status == str(status))
        rows = await self._scalars(
            session,
            select(ExternalAction)
            .where(*conditions)
            .order_by(ExternalAction.created_at.desc(), ExternalAction.id.desc())
            .limit(page_limit)
            .offset(page_offset),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = row.to_safe_dict()
            item.pop("payload", None)
            item.pop("source_url", None)
            result.append(item)
        return result

    async def get_media_adapter_status(
        self,
        session: Any | None = None,
        actor: Any | None = None,
    ) -> dict[str, Any]:
        """Return deterministic manual-only status for all six platforms."""

        if actor is None:
            raise OperationsValidationError("actor is required")
        platforms: dict[str, dict[str, Any]] = {}
        for key, value in MEDIA_ADAPTER_STATUS.items():
            item = dict(value)
            policy = get_provider_capability(key) if callable(get_provider_capability) else None
            if policy is not None:
                # Keep provider network execution disabled until a concrete
                # adapter contract/private target is verified.  The registry
                # projection is metadata-only and never exposes credentials.
                item["registry_version"] = getattr(policy, "registry_version", None)
                item["adapter_key"] = getattr(policy, "adapter_key", None) or getattr(
                    policy,
                    "adapter_ref",
                    None,
                )
                item["operations"] = {
                    operation: getattr(policy.status_for(operation), "value", str(policy.status_for(operation)))
                    for operation in ("text", "image", "video", "schedule", "edit", "delete", "revenue")
                }
                item["provider_execution"] = "unverified"
            platforms[key] = item
        return {
            "schema_version": "media-operations-adapter-status-v1",
            "provider_calls": False,
            "platforms": platforms,
        }

    media_adapter_status = get_media_adapter_status
    list_media_adapter_status = get_media_adapter_status

    async def _load_connection_and_draft(
        self,
        session: Any,
        actor: Any,
        *,
        connection_id: UUID | str,
        draft_id: UUID | str,
    ) -> tuple[ExternalConnection, ApplicationDraft, EngagementOpportunity]:
        connection = await self._get_or_404(session, ExternalConnection, connection_id, "connection_id")
        draft = await self._get_or_404(session, ApplicationDraft, draft_id, "application_draft_id")
        opportunity = await self._get_or_404(session, EngagementOpportunity, draft.opportunity_id, "opportunity_id")
        await self._assert_entity_access(session, actor, connection, permission="write")
        await self._assert_entity_access(session, actor, opportunity, permission="write")
        if connection.project_id != draft.project_id or opportunity.project_id != draft.project_id:
            raise OperationsValidationError("connection, opportunity and draft project_id must match")
        if connection.owner_user_id != draft.owner_user_id:
            # A project member can use a project connection, but personal
            # connections are never shareable across owners.
            if connection.project_id is None or draft.project_id is None:
                raise OperationsAuthorizationError("connection owner mismatch")
        return connection, draft, opportunity

    async def create_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        connection_id: UUID | str,
        application_draft_id: UUID | str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        origin_agent_id: UUID | str | None = None,
        origin_agent_run_id: UUID | str | None = None,
        origin_work_item_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        origin_agent_uuid, origin_run_uuid, origin_work_item_uuid = await _validate_typed_origin(
            session,
            origin_agent_id=origin_agent_id,
            origin_agent_run_id=origin_agent_run_id,
            origin_work_item_id=origin_work_item_id,
        )
        key = _text(idempotency_key, "idempotency_key", required=True, max_bytes=255)
        connection, draft, opportunity = await self._load_connection_and_draft(
            session,
            actor,
            connection_id=connection_id,
            draft_id=application_draft_id,
        )
        draft_payload = {
            "operation_key": "engagement.submit_application",
            "connection_id": str(connection.id),
            "connection": _connection_snapshot(connection),
            "opportunity_id": str(opportunity.id),
            "message": draft.message,
            "offered_price": draft.offered_price,
            "currency": draft.currency,
            "delivery_estimate": draft.delivery_estimate,
            "application_draft_id": str(draft.id),
            "draft_version": int(draft.version),
        }
        # v1 has one fixed operation contract.  Arbitrary provider payloads
        # would allow callers to persist secrets or override draft-bound
        # values, so only the canonical exact draft payload is accepted.
        if payload:
            raise OperationsValidationError("custom action payload fields are not supported")
        action_payload = dict(draft_payload)
        artifact_rows = await self._artifact_rows(
            session,
            actor,
            draft.artifact_version_ids or [],
            project_id=draft.project_id,
        )
        artifact_hashes = [str(item.sha256).lower() for item in artifact_rows]
        action_payload_hash = sha256_json(action_payload)
        scope_conditions = [ExternalAction.idempotency_key == key]
        if draft.project_id is None:
            scope_conditions.extend(
                [
                    ExternalAction.project_id.is_(None),
                    ExternalAction.owner_user_id == _actor_id(actor),
                ]
            )
        else:
            scope_conditions.append(ExternalAction.project_id == draft.project_id)
        existing = await self._scalar(
            session,
            select(ExternalAction)
            .where(*scope_conditions)
            .limit(1),
        )
        if existing is not None:
            if (
                existing.payload_hash != action_payload_hash
                or list(existing.artifact_hashes or []) != artifact_hashes
                or existing.origin_agent_id != origin_agent_uuid
                or existing.origin_agent_run_id != origin_run_uuid
                or existing.origin_work_item_id != origin_work_item_uuid
            ):
                raise OperationsConflictError("idempotency key is already bound to a different action hash")
            return await self.get_action(session, actor, existing.id)
        action = ExternalAction(
            owner_user_id=_actor_id(actor),
            project_id=draft.project_id,
            opportunity_id=opportunity.id,
            source_url=sanitize_source_url(opportunity.source_url),
            source_snapshot_hash=opportunity.source_snapshot_hash,
            connection_id=connection.id,
            application_draft_id=draft.id,
            application_draft_version=int(draft.version),
            action_type="engagement.submit_application",
            idempotency_key=key,
            payload_json=action_payload,
            payload_hash=action_payload_hash,
            artifact_hashes=artifact_hashes,
            action_version=1,
            version=1,
            status="proposed",
            created_by=_actor_id(actor),
            origin_agent_id=origin_agent_uuid,
            origin_agent_run_id=origin_run_uuid,
            origin_work_item_id=origin_work_item_uuid,
        )
        session.add(action)
        try:
            await self._flush_only(session)
        except IntegrityError:
            await self._rollback(session)
            existing = await self._scalar(
                session,
                select(ExternalAction)
                .where(*scope_conditions)
                .limit(1),
            )
            if existing is None:
                raise
            if (
                existing.payload_hash != action_payload_hash
                or list(existing.artifact_hashes or []) != artifact_hashes
                or existing.origin_agent_id != origin_agent_uuid
                or existing.origin_agent_run_id != origin_run_uuid
                or existing.origin_work_item_id != origin_work_item_uuid
            ):
                raise OperationsConflictError("idempotency key is already bound to a different action hash")
            return await self.get_action(session, actor, existing.id)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.proposed",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={"action_version": 1, "version": 1, "payload_hash": action.payload_hash},
        )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    propose_action = create_action

    async def list_actions(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        page_limit, page_offset = _bounded_page(limit, offset)
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        if project_uuid is not None:
            await self._assert_access(session, actor, project_id=project_uuid, permission="read")
            scope_condition = ExternalAction.project_id == project_uuid
        else:
            scope_condition = await self._scope_less_condition(
                session,
                actor,
                ExternalAction,
            )
        conditions: list[Any] = [scope_condition]
        if status:
            conditions.append(ExternalAction.status == str(status))
        rows = await self._scalars(
            session,
            select(ExternalAction)
            .where(*conditions)
            .order_by(ExternalAction.created_at.desc(), ExternalAction.id.desc())
            .limit(page_limit)
            .offset(page_offset),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = row.to_safe_dict()
            # Collection reads are bounded metadata projections.  Exact
            # payload/source inspection belongs to get_action().
            item.pop("payload", None)
            item.pop("source_url", None)
            result.append(item)
        return result

    async def list_work_intelligence_projection(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str,
        limit: int = 24,
    ) -> dict[str, Any]:
        """Return a bounded, ACL-safe projection for shared Work Intelligence.

        Operations remains authoritative for these facts.  This projection is
        intentionally metadata-only: it never exposes captured source text,
        application messages/payloads, idempotency keys, credentials, receipt
        bodies, or evidence notes to the context compiler.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id")
        assert project_uuid is not None
        await self._assert_access(
            session,
            actor,
            project_id=project_uuid,
            permission="read",
        )
        bounded_limit = max(1, min(int(limit or 24), 128))
        opportunities = await self._scalars(
            session,
            select(EngagementOpportunity)
            .where(EngagementOpportunity.project_id == project_uuid)
            .order_by(EngagementOpportunity.updated_at.desc())
            .limit(bounded_limit),
        )
        actions = await self._scalars(
            session,
            select(ExternalAction)
            .where(ExternalAction.project_id == project_uuid)
            .order_by(ExternalAction.updated_at.desc())
            .limit(bounded_limit),
        )
        opportunity_titles = {item.id: item.title for item in opportunities}
        opportunity_sources = {item.id: item.source_url for item in opportunities}
        opportunity_source_texts = {item.id: item.source_text for item in opportunities}
        missing_opportunity_ids = {
            item.opportunity_id
            for item in actions
            if item.opportunity_id not in opportunity_titles
        }
        if missing_opportunity_ids:
            related = await self._scalars(
                session,
                select(EngagementOpportunity).where(
                    EngagementOpportunity.project_id == project_uuid,
                    EngagementOpportunity.id.in_(missing_opportunity_ids),
                ),
            )
            opportunity_titles.update({item.id: item.title for item in related})
            opportunity_sources.update({item.id: item.source_url for item in related})
            opportunity_source_texts.update({item.id: item.source_text for item in related})

        return {
            "schema_version": "operations-work-projection-v1",
            "project_id": str(project_uuid),
            "opportunities": [
                {
                    "id": str(item.id),
                    "project_id": str(project_uuid),
                    "owner_user_id": str(item.owner_user_id),
                    "title": safe_opportunity_title(
                        item.title,
                        item.source_url,
                        opportunity_source_texts.get(item.id),
                    ),
                    "status": item.status,
                    "source_snapshot_hash": item.source_snapshot_hash,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in opportunities
            ],
            "actions": [
                {
                    "id": str(item.id),
                    "project_id": str(project_uuid),
                    "owner_user_id": str(item.owner_user_id),
                    "opportunity_id": str(item.opportunity_id) if item.opportunity_id else None,
                    "title": (
                        safe_opportunity_title(
                            opportunity_titles.get(item.opportunity_id),
                            opportunity_sources.get(item.opportunity_id),
                            opportunity_source_texts.get(item.opportunity_id),
                        )
                        if item.opportunity_id in opportunity_titles
                        else (
                            f"MediaOps {item.platform} action"
                            if item.action_type in MEDIA_ACTION_TYPES and item.platform
                            else "Application action"
                        )
                    ),
                    "operation_key": item.action_type,
                    "platform": item.platform,
                    "media_action": item.action_type in MEDIA_ACTION_TYPES,
                    "content_variant_id": (
                        str(item.content_variant_id)
                        if item.content_variant_id is not None
                        else None
                    ),
                    "status": item.status,
                    "version": int(item.version or 1),
                    "action_version": int(item.action_version or 1),
                    "payload_hash": item.payload_hash,
                    "artifact_hashes": list(item.artifact_hashes or []),
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in actions
            ],
        }

    async def get_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id")
        await self._assert_entity_access(session, actor, action, permission="read")
        payload = action.to_safe_dict()
        approvals = await self._scalars(
            session,
            select(ExternalActionApproval)
            .where(ExternalActionApproval.action_id == action.id)
            .order_by(
                ExternalActionApproval.created_at.desc(),
                ExternalActionApproval.id.desc(),
            )
            .limit(_ACTION_APPROVAL_HISTORY_LIMIT + 1),
        )
        attempts = await self._scalars(
            session,
            select(ExternalActionAttempt)
            .where(ExternalActionAttempt.action_id == action.id)
            .order_by(
                ExternalActionAttempt.started_at.desc(),
                ExternalActionAttempt.id.desc(),
            )
            .limit(_ACTION_ATTEMPT_HISTORY_LIMIT + 1),
        )
        receipt = await self._scalar(
            session,
            select(ExternalActionReceipt)
            .where(ExternalActionReceipt.action_id == action.id)
            .order_by(ExternalActionReceipt.created_at.desc())
            .limit(1),
        )
        events = await self._scalars(
            session,
            select(OperationEvent)
            .where(OperationEvent.entity_type == "action", OperationEvent.entity_id == action.id)
            .order_by(OperationEvent.created_at.desc(), OperationEvent.id.desc())
            .limit(_ACTION_TIMELINE_LIMIT + 1),
        )
        approvals_has_more = len(approvals) > _ACTION_APPROVAL_HISTORY_LIMIT
        attempts_has_more = len(attempts) > _ACTION_ATTEMPT_HISTORY_LIMIT
        timeline_has_more = len(events) > _ACTION_TIMELINE_LIMIT
        approvals = approvals[:_ACTION_APPROVAL_HISTORY_LIMIT]
        attempts = attempts[:_ACTION_ATTEMPT_HISTORY_LIMIT]
        # Events are selected newest-first to retain the authority-changing
        # tail, then rendered chronologically for the durable UI timeline.
        events = list(reversed(events[:_ACTION_TIMELINE_LIMIT]))
        payload["approvals"] = [item.to_safe_dict() for item in approvals]
        payload["attempts"] = [item.to_safe_dict() for item in attempts]
        payload["receipt"] = receipt.to_safe_dict() if receipt is not None else None
        payload["timeline"] = [item.to_safe_dict() for item in events]
        payload["history"] = {
            "approvals_has_more": approvals_has_more,
            "attempts_has_more": attempts_has_more,
            "timeline_has_more": timeline_has_more,
            "approval_limit": _ACTION_APPROVAL_HISTORY_LIMIT,
            "attempt_limit": _ACTION_ATTEMPT_HISTORY_LIMIT,
            "timeline_limit": _ACTION_TIMELINE_LIMIT,
        }
        return payload

    async def revise_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        application_draft_id: UUID | str,
        expected_version: int | None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        await self._assert_entity_access(session, actor, action, permission="write")
        await self._check_expected_version(action, expected_version)
        if action.status not in {"proposed", "approved", "rejected", "failed"}:
            raise OperationsInvalidTransitionError(
                "action cannot be revised in its current state"
            )
        draft = await self._get_or_404(session, ApplicationDraft, application_draft_id, "application_draft_id")
        opportunity = await self._get_or_404(session, EngagementOpportunity, draft.opportunity_id, "opportunity_id")
        await self._assert_entity_access(session, actor, draft, permission="write")
        if draft.opportunity_id != action.opportunity_id or draft.version <= 0:
            raise OperationsValidationError("draft does not belong to action opportunity")
        # Rebind the proposal to the current provider target whenever it is
        # revised.  This refreshes the immutable snapshot and ensures a stale
        # connection cannot be smuggled through a draft-only revision.
        connection, _bound_draft, _bound_opportunity = await self._load_connection_and_draft(
            session,
            actor,
            connection_id=action.connection_id,
            draft_id=draft.id,
        )
        current_draft = await self._get_or_404(session, ApplicationDraft, action.application_draft_id, "application_draft_id")
        if draft.version <= current_draft.version:
            raise OperationsConflictError("revision must reference a newer application draft version")
        artifact_rows = await self._artifact_rows(
            session,
            actor,
            draft.artifact_version_ids or [],
            project_id=action.project_id,
        )
        artifact_hashes = [str(item.sha256).lower() for item in artifact_rows]
        action_payload = {
            "operation_key": "engagement.submit_application",
            "connection_id": str(action.connection_id),
            "connection": _connection_snapshot(connection),
            "opportunity_id": str(action.opportunity_id),
            "message": draft.message,
            "offered_price": draft.offered_price,
            "currency": draft.currency,
            "delivery_estimate": draft.delivery_estimate,
            "application_draft_id": str(draft.id),
            "draft_version": int(draft.version),
        }
        previous = {
            "action_version": int(action.action_version or 1),
            "payload_hash": action.payload_hash,
            "application_draft_id": str(action.application_draft_id),
        }
        action.application_draft_id = draft.id
        action.application_draft_version = int(draft.version)
        action.source_url = sanitize_source_url(opportunity.source_url)
        action.source_snapshot_hash = opportunity.source_snapshot_hash
        action.payload_json = action_payload
        action.payload_hash = sha256_json(action_payload)
        action.artifact_hashes = artifact_hashes
        action.action_version = int(action.action_version or 1) + 1
        action.version = int(action.version or 1) + 1
        action.status = "proposed"
        # Keep the decision history immutable and add an explicit invalidation
        # row rather than deleting or silently overwriting prior approvals.
        prior_approvals = await self._scalars(
            session,
            select(ExternalActionApproval).where(
                ExternalActionApproval.action_id == action.id,
                ExternalActionApproval.action_version == previous["action_version"],
                ExternalActionApproval.decision == "approved",
            ),
        )
        # Approval rows are append-only.  Preserve each original human
        # decision and append an invalidation marker bound to the exact old
        # version/hash instead of rewriting history in place.
        for approval in prior_approvals:
            session.add(
                ExternalActionApproval(
                    action_id=action.id,
                    owner_user_id=action.owner_user_id,
                    action_version=approval.action_version,
                    payload_hash=approval.payload_hash,
                    artifact_hashes=list(approval.artifact_hashes or []),
                    decision="invalidated",
                    reason="proposal revised",
                    decided_by=_actor_id(actor),
                )
            )
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.revised",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "previous": previous,
                "action_version": action.action_version,
                "version": action.version,
                "payload_hash": action.payload_hash,
                "invalidated_approval_count": len(prior_approvals),
            },
        )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    async def _decision(
        self,
        session: Any,
        actor: Any,
        action_id: UUID | str,
        *,
        decision: str,
        expected_version: int | None,
        action_version: int | None = None,
        payload_hash_value: str | None = None,
        artifact_hashes: Sequence[Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        await self._assert_human(actor)
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        await self._assert_entity_access(session, actor, action, permission="write")
        await self._check_expected_version(action, expected_version)
        # MediaOps proposals are bound to immutable variant/revision rows and
        # a canonical typed payload.  Re-check the binding before accepting a
        # human decision so a stale/tampered row cannot become executable.
        if getattr(action, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        await self._assert_media_action_integrity(session, actor, action)
        expected_action_version = int(action.action_version or 1)
        if getattr(action, "action_policy_revision_id", None) is not None:
            # Generic policies never impersonate a human approval. Procurement
            # approval covers a server-observed exact quote, including price.
            if action.authorization_mode != "human_approval":
                raise OperationsConflictError("bounded policy actions do not accept human approval")
            if payload_hash(action.payload_json) != action.payload_hash:
                raise OperationsConflictError("action payload integrity failed")
            if action.action_type == "procurement.place_order" and decision == "approved":
                from .procurement_action import validate_quote
                quote = action.payload_json.get("quote")
                if quote is None:
                    raise OperationsConflictError("a current provider quote is required before approval")
                try:
                    validate_quote(quote, {k: v for k, v in action.payload_json.items() if k != "quote"}, {}, enforce_ceiling=False)
                except (ValueError, TypeError):
                    raise OperationsConflictError("a current provider quote is required before approval") from None
        if action_version is not None and int(action_version) != expected_action_version:
            raise OperationsStaleVersionError("action_version does not match current proposal")
        exact_hash = str(payload_hash_value or action.payload_hash).strip().lower()
        if exact_hash != action.payload_hash:
            raise OperationsConflictError("payload_hash does not match current proposal")
        exact_artifacts = _hash_list(artifact_hashes if artifact_hashes is not None else action.artifact_hashes)
        if exact_artifacts != list(action.artifact_hashes or []):
            raise OperationsConflictError("artifact_hashes do not match current proposal")
        if decision == "approved":
            if action.status not in {"proposed", "rejected", "failed"}:
                raise OperationsInvalidTransitionError("action cannot be approved in its current state")
            next_status = "approved"
        else:
            if action.status not in {"proposed", "approved"}:
                raise OperationsInvalidTransitionError("action cannot be rejected in its current state")
            next_status = "rejected"
        approval = ExternalActionApproval(
            action_id=action.id,
            owner_user_id=action.owner_user_id,
            action_version=expected_action_version,
            payload_hash=exact_hash,
            artifact_hashes=exact_artifacts,
            decision=decision,
            reason=_text(reason, "reason", max_bytes=4_000),
            decided_by=_actor_id(actor),
        )
        session.add(approval)
        action.status = next_status
        action.version = int(action.version or 1) + 1
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type=f"action.{decision}",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "action_version": expected_action_version,
                "version": action.version,
                "payload_hash": exact_hash,
                "artifact_hashes": exact_artifacts,
            },
        )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    async def approve_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        expected_version: int | None,
        action_version: int | None = None,
        payload_hash: str | None = None,
        artifact_hashes: Sequence[Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        return await self._decision(
            self._resolve_session(session),
            actor,
            action_id,
            decision="approved",
            expected_version=expected_version,
            action_version=action_version,
            payload_hash_value=payload_hash,
            artifact_hashes=artifact_hashes,
            reason=reason,
        )

    approve = approve_action

    async def reject_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        expected_version: int | None,
        action_version: int | None = None,
        payload_hash: str | None = None,
        artifact_hashes: Sequence[Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        return await self._decision(
            self._resolve_session(session),
            actor,
            action_id,
            decision="rejected",
            expected_version=expected_version,
            action_version=action_version,
            payload_hash_value=payload_hash,
            artifact_hashes=artifact_hashes,
            reason=reason,
        )

    reject = reject_action

    async def _current_valid_approval(self, session: Any, action: ExternalAction) -> ExternalActionApproval | None:
        return await self._scalar(
            session,
            select(ExternalActionApproval)
            .where(
                ExternalActionApproval.action_id == action.id,
                ExternalActionApproval.action_version == int(action.action_version or 1),
                ExternalActionApproval.decision == "approved",
                ExternalActionApproval.payload_hash == action.payload_hash,
            )
            .order_by(ExternalActionApproval.created_at.desc())
            .limit(1),
        )

    async def _invoke_provider_adapter(
        self,
        action: ExternalAction,
        attempt: ExternalActionAttempt,
    ) -> Any:
        """Invoke an explicitly injected provider adapter with safe fields.

        The production service has no adapter registered until a provider
        contract is verified.  Tests may inject a deterministic adapter to
        exercise receipt/timeout semantics without making a network request.
        The request deliberately excludes credentials, local paths and raw
        connection metadata.
        """

        adapter = self.provider_adapter
        if adapter is None:
            raise OperationsInvalidTransitionError(
                "provider adapter execution is unavailable"
            )
        method = getattr(adapter, "execute", None) or getattr(adapter, "submit", None)
        if not callable(method):
            raise OperationsInvalidTransitionError(
                "provider adapter execution is unavailable"
            )
        raw = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        safe_request = {
            "action_id": str(action.id),
            "action_type": action.action_type,
            "platform": action.platform,
            "payload": raw.get("payload") if isinstance(raw.get("payload"), Mapping) else {},
            "execution_key": getattr(action, "execution_key", None) or action.idempotency_key,
            "idempotency_key": action.idempotency_key,
            "adapter_key": getattr(action, "adapter_key", None),
            "adapter_version": getattr(action, "adapter_version", None),
            "attempt_id": str(attempt.id),
        }
        value = method(**safe_request)
        return await value if isawaitable(value) else value

    @staticmethod
    def _provider_result_projection(value: Any) -> dict[str, Any]:
        """Whitelist a provider result before it reaches receipt columns."""

        if not isinstance(value, Mapping):
            raise OperationsValidationError("provider adapter returned an invalid result")
        rendered_status = str(value.get("status") or value.get("outcome") or "").strip().lower()
        aliases = {
            "success": "succeeded",
            "ok": "succeeded",
            "confirmed": "succeeded",
            "failure": "failed",
            "error": "failed",
            "ambiguous": "uncertain",
            "timeout": "uncertain",
        }
        rendered_status = aliases.get(rendered_status, rendered_status)
        if rendered_status not in {"succeeded", "failed", "uncertain"}:
            raise OperationsValidationError("provider adapter returned an invalid status")
        result: dict[str, Any] = {"status": rendered_status}
        for key, label, limit in (
            ("provider_attempt_ref", "provider_attempt_ref", 255),
            ("provider_receipt_ref", "provider_receipt_ref", 255),
            ("remote_resource_id", "remote_resource_id", 255),
            ("remote_status", "remote_status", 64),
            ("result_summary", "result_summary", 8_000),
            ("evidence_note", "evidence_note", 8_000),
            ("error_message", "error_message", 4_000),
            ("confirmation_level", "confirmation_level", 32),
        ):
            if key in value:
                result[key] = _text(value.get(key), label, max_bytes=limit)
        if "remote_url" in value:
            result["remote_url"] = _validated_external_url(value.get("remote_url"), "remote_url")
        if "provider_observed_at" in value:
            result["provider_observed_at"] = _parse_datetime(value.get("provider_observed_at"))
        evidence = value.get("evidence_artifact_ids", [])
        if evidence is None:
            evidence = []
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise OperationsValidationError("provider evidence_artifact_ids must be a list")
        result["evidence_artifact_ids"] = list(evidence)
        return result

    async def _mark_provider_attempt_uncertain(
        self,
        session: Any,
        actor: Any,
        action: ExternalAction,
        attempt: ExternalActionAttempt,
        *,
        note: str,
    ) -> None:
        """Persist an ambiguous provider outcome without retrying it."""

        attempt.status = "uncertain"
        attempt.error_message = "provider execution outcome is ambiguous"
        attempt.evidence_note = _text(note, "evidence_note", max_bytes=8_000)
        attempt.finished_at = attempt.finished_at or datetime.utcnow()
        action.status = "uncertain"
        action.version = int(action.version or 1) + 1
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.attempt.uncertain",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "attempt_id": str(attempt.id),
                "execution_mode": "provider",
                "action_version": int(action.action_version or 1),
                "version": int(action.version or 1),
            },
        )
        await self._flush_commit(session)

    async def create_attempt(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        expected_version: int | None,
        provider_attempt_ref: str | None = None,
        executor_type: str = "manual",
        execution_mode: str | None = None,
        execution_key: str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        await self._assert_human(actor)
        executor_value = str(executor_type or "manual").strip().lower()
        if executor_value not in {"manual", "provider"}:
            raise OperationsValidationError("executor_type must be manual or provider")
        requested_mode = _normalize_media_execution_mode(
            execution_mode or executor_value,
        )
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        await self._assert_entity_access(session, actor, action, permission="write")
        if getattr(action, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        await self._check_expected_version(action, expected_version)
        await self._assert_media_action_integrity(session, actor, action)
        raw_payload = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        action_mode = _normalize_media_execution_mode(
            getattr(action, "execution_mode", None) or raw_payload.get("execution_mode", "manual")
        )
        if requested_mode != action_mode or executor_value != action_mode:
            raise OperationsConflictError(
                "attempt execution mode does not match the approved action"
            )
        connection = await self._get_or_404(
            session,
            ExternalConnection,
            action.connection_id,
            "connection_id",
        )
        await self._assert_entity_access(session, actor, connection, permission="write")
        action_payload = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        stored_connection_snapshot = action_payload.get("connection")
        if not isinstance(stored_connection_snapshot, Mapping):
            stored_connection_snapshot = {}
        if not _connection_target_matches(stored_connection_snapshot, connection):
            raise OperationsConflictError(
                "connection target changed since proposal; revise the action before attempting"
            )
        if action.status == "uncertain":
            raise OperationsInvalidTransitionError("uncertain actions require explicit reconcile before retry")
        if action.status != "approved":
            raise OperationsInvalidTransitionError("a current human approval is required before attempting")
        approval = await self._current_valid_approval(session, action)
        if approval is None or list(approval.artifact_hashes or []) != list(action.artifact_hashes or []):
            raise OperationsInvalidTransitionError("current approval is missing or does not match action evidence")
        running = await self._scalar(
            session,
            select(ExternalActionAttempt)
            .where(ExternalActionAttempt.action_id == action.id, ExternalActionAttempt.status == "running")
            .limit(1),
        )
        if running is not None:
            raise OperationsConflictError("an action attempt is already running")
        action_execution_key = getattr(action, "execution_key", None) or raw_payload.get("execution_key")
        if action_mode == "provider":
            if self.provider_adapter is None:
                # No provider adapter in this checkout has a verified
                # private/draft contract.  Keep the boundary fail-closed and
                # avoid creating an attempt that could be mistaken for a
                # network submission.
                raise OperationsInvalidTransitionError(
                    "provider adapter execution is unavailable; use the manual adapter"
                )
            if execution_key is not None and _text(execution_key, "execution_key", max_bytes=255) != action_execution_key:
                raise OperationsConflictError("execution_key does not match the approved action")
        attempt = ExternalActionAttempt(
            action_id=action.id,
            owner_user_id=action.owner_user_id,
            action_version=int(action.action_version or 1),
            executor_type=action_mode,
            execution_mode=action_mode,
            status="running",
            provider_attempt_ref=_text(provider_attempt_ref, "provider_attempt_ref", max_bytes=255),
            execution_key=(
                _text(execution_key or action_execution_key, "execution_key", max_bytes=255)
                if execution_key is not None
                else (action_execution_key if action_mode == "provider" else None)
            ),
            provider_key=(action.platform if action_mode == "provider" else None),
            provider_adapter_key=(getattr(action, "adapter_key", None) if action_mode == "provider" else None),
            provider_adapter_version=(getattr(action, "adapter_version", None) if action_mode == "provider" else None),
            capability_snapshot_id=(getattr(action, "capability_snapshot_id", None) if action_mode == "provider" else None),
            capability_snapshot_hash=(getattr(action, "capability_snapshot_hash", None) if action_mode == "provider" else None),
            credential_state_hash=(getattr(action, "credential_state_hash", None) if action_mode == "provider" else None),
            created_by=_actor_id(actor),
        )
        session.add(attempt)
        action.status = "attempting"
        action.version = int(action.version or 1) + 1
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type="action.attempt.started",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={"attempt_id": str(attempt.id), "action_version": attempt.action_version, "version": action.version},
        )
        await self._flush_commit(session)
        if action_mode == "provider":
            # Re-read every immutable authority binding after the durable
            # attempt intent is committed and immediately before invoking the
            # adapter.  Approval/capability/credential changes in the small
            # interval after the initial gate therefore fail closed rather
            # than turning a stale intent into a provider write.
            try:
                action = await self._get_or_404(
                    session,
                    ExternalAction,
                    action.id,
                    "action_id",
                    for_update=True,
                )
                attempt = await self._get_or_404(
                    session,
                    ExternalActionAttempt,
                    attempt.id,
                    "attempt_id",
                    for_update=True,
                )
                if (
                    action.status != "attempting"
                    or int(action.action_version or 0)
                    != int(attempt.action_version or 0)
                    or _normalize_media_execution_mode(
                        getattr(action, "execution_mode", None)
                        or "manual"
                    )
                    != "provider"
                    or attempt.executor_type != "provider"
                    or getattr(attempt, "execution_mode", None) != "provider"
                ):
                    raise OperationsConflictError(
                        "approved action changed before provider invocation"
                    )
                approval = await self._current_valid_approval(session, action)
                if approval is None or list(approval.artifact_hashes or []) != list(
                    action.artifact_hashes or []
                ):
                    raise OperationsInvalidTransitionError(
                        "human approval changed before provider invocation"
                    )
                await self._assert_media_action_integrity(
                    session,
                    actor,
                    action,
                    require_readiness=True,
                )
            except Exception:
                await self._mark_provider_attempt_uncertain(
                    session,
                    actor,
                    action,
                    attempt,
                    note="provider authority changed before adapter invocation",
                )
                provider_preflight_ok = False
            else:
                provider_preflight_ok = True
            if not provider_preflight_ok:
                refreshed_attempt = await self._get_or_404(
                    session,
                    ExternalActionAttempt,
                    attempt.id,
                    "attempt_id",
                )
                return refreshed_attempt.to_safe_dict()
            try:
                provider_result = self._provider_result_projection(
                    await self._invoke_provider_adapter(action, attempt)
                )
            except OperationsInvalidTransitionError:
                # The adapter disappeared or was removed from the registry
                # after the durable intent was written.  This is not a safe
                # retry; mark the attempt ambiguous and require reconcile.
                await self._mark_provider_attempt_uncertain(
                    session,
                    actor,
                    action,
                    attempt,
                    note="provider adapter is unavailable after intent commit",
                )
            except Exception:
                # Any transport/provider exception may have reached the
                # remote service.  Never retry blindly; record uncertainty
                # without persisting exception text or raw responses.
                await self._mark_provider_attempt_uncertain(
                    session,
                    actor,
                    action,
                    attempt,
                    note="provider adapter outcome could not be confirmed",
                )
            else:
                if provider_result.get("provider_attempt_ref"):
                    attempt.provider_attempt_ref = provider_result["provider_attempt_ref"]
                if provider_result["status"] == "uncertain":
                    await self._mark_provider_attempt_uncertain(
                        session,
                        actor,
                        action,
                        attempt,
                        note=str(provider_result.get("evidence_note") or provider_result.get("result_summary") or "provider outcome is ambiguous"),
                    )
                elif provider_result["status"] == "succeeded" and not _has_provider_success_evidence(
                    provider_receipt_ref=provider_result.get("provider_receipt_ref"),
                    remote_resource_id=provider_result.get("remote_resource_id"),
                    remote_status=provider_result.get("remote_status"),
                    provider_observed_at=provider_result.get("provider_observed_at"),
                    evidence_artifact_ids=provider_result.get("evidence_artifact_ids", []),
                    evidence_note=provider_result.get("evidence_note"),
                ):
                    # A provider adapter that returns only a success label (or
                    # a free-form summary) has not supplied a durable receipt or
                    # remote postcondition.  Keep the intent ambiguous and
                    # require explicit reconcile; do not mint a success row.
                    await self._mark_provider_attempt_uncertain(
                        session,
                        actor,
                        action,
                        attempt,
                        note="provider success lacked a receipt or bounded remote postcondition",
                    )
                else:
                    try:
                        await self.complete_attempt(
                            session,
                            actor,
                            action.id,
                            attempt.id,
                            status=provider_result["status"],
                            expected_version=int(action.version or 1),
                            evidence_artifact_ids=provider_result.get("evidence_artifact_ids", []),
                            provider_receipt_ref=provider_result.get("provider_receipt_ref"),
                            result_summary=provider_result.get("result_summary"),
                            remote_resource_id=provider_result.get("remote_resource_id"),
                            remote_url=provider_result.get("remote_url"),
                            remote_status=provider_result.get("remote_status"),
                            provider_observed_at=provider_result.get("provider_observed_at"),
                            evidence_note=provider_result.get("evidence_note"),
                            # A provider adapter result is a machine-confirmed
                            # receipt unless it explicitly supplies another
                            # closed confirmation level; never label it as the
                            # human-only default by accident.
                            confirmation_level=(
                                provider_result.get("confirmation_level")
                                or "provider_confirmed"
                            ),
                            error_message=provider_result.get("error_message"),
                            _provider_confirmation_token=_PROVIDER_ADAPTER_CONFIRMATION_TOKEN,
                        )
                    except Exception:
                        # The durable intent has already been committed.  Any
                        # failed confirmation/authority check is therefore
                        # ambiguous rather than safely retryable.
                        await self._mark_provider_attempt_uncertain(
                            session,
                            actor,
                            action,
                            attempt,
                            note="provider result could not be confirmed",
                        )
            refreshed_attempt = await self._get_or_404(
                session,
                ExternalActionAttempt,
                attempt.id,
                "attempt_id",
            )
            return refreshed_attempt.to_safe_dict()
        return attempt.to_safe_dict()

    start_attempt = create_attempt

    async def _validate_evidence_artifacts(
        self,
        session: Any,
        actor: Any,
        artifact_ids: Sequence[Any] | None,
        *,
        project_id: UUID | None,
    ) -> list[UUID]:
        rows = await self._artifact_rows(session, actor, artifact_ids or [], project_id=project_id)
        return [row.id for row in rows]

    async def complete_attempt(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        attempt_id: UUID | str | None = None,
        *,
        status: str,
        expected_version: int | None,
        evidence_artifact_ids: Sequence[Any] | None = None,
        provider_receipt_ref: str | None = None,
        result_summary: str | None = None,
        remote_resource_id: str | None = None,
        remote_url: str | None = None,
        remote_status: str | None = None,
        provider_observed_at: datetime | str | None = None,
        evidence_note: str | None = None,
        confirmation_level: str | None = None,
        error_message: str | None = None,
        _provider_confirmation_token: object | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or action_id is None or attempt_id is None:
            raise OperationsValidationError("actor, action_id and attempt_id are required")
        await self._assert_human(actor)
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        await self._assert_entity_access(session, actor, action, permission="write")
        if getattr(action, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        await self._check_expected_version(action, expected_version)
        await self._assert_media_action_integrity(session, actor, action)
        attempt = await self._get_or_404(session, ExternalActionAttempt, attempt_id, "attempt_id", for_update=True)
        if attempt.action_id != action.id:
            raise OperationsValidationError("attempt does not belong to action")
        if getattr(attempt, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        action_mode = _normalize_media_execution_mode(
            getattr(action, "execution_mode", None)
            or (
                action.payload_json.get("execution_mode", "manual")
                if isinstance(action.payload_json, Mapping)
                else "manual"
            )
        )
        if action_mode == "provider":
            if attempt.executor_type != "provider" or getattr(attempt, "execution_mode", None) != "provider":
                raise OperationsConflictError("provider attempt execution metadata is stale")
            if (
                getattr(attempt, "capability_snapshot_id", None) != getattr(action, "capability_snapshot_id", None)
                or str(getattr(attempt, "capability_snapshot_hash", "") or "") != str(getattr(action, "capability_snapshot_hash", "") or "")
                or str(getattr(attempt, "provider_adapter_key", "") or "") != str(getattr(action, "adapter_key", "") or "")
                or str(getattr(attempt, "provider_adapter_version", "") or "") != str(getattr(action, "adapter_version", "") or "")
            ):
                raise OperationsConflictError("provider attempt authority changed after execution")
        if attempt.status != "running":
            raise OperationsInvalidTransitionError("attempt has already been completed")
        if int(attempt.action_version or 0) != int(action.action_version or 0):
            raise OperationsStaleVersionError("attempt action_version is stale")
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"success", "succeeded"}:
            normalized_status = "succeeded"
        if normalized_status not in {"succeeded", "failed", "uncertain"}:
            raise OperationsValidationError("status must be succeeded, failed, or uncertain")
        if action.status == "uncertain":
            # A direct completion can never turn an uncertain action into a
            # success/failure; use reconcile() with explicit evidence.
            raise OperationsInvalidTransitionError("uncertain actions require explicit reconcile")
        evidence_ids = await self._validate_evidence_artifacts(
            session,
            actor,
            evidence_artifact_ids,
            project_id=action.project_id,
        )
        if normalized_status == "uncertain" and not evidence_ids and not (evidence_note or result_summary or error_message):
            raise OperationsValidationError("uncertain completion requires an evidence note or artifact")
        # Keep authority/prerequisite checks ahead of URL parsing while still
        # validating before the first action/attempt mutation or receipt.
        remote_url_value = _validated_external_url(remote_url, "remote_url")
        # Normalize every value that can be persisted before mutating either
        # the action or attempt.  Callers may catch a validation exception and
        # commit the same session, so no partial transition may remain dirty.
        result_summary_value = _text(result_summary, "result_summary", max_bytes=8_000)
        evidence_note_value = _text(evidence_note, "evidence_note", max_bytes=8_000)
        error_message_value = _text(error_message, "error_message", max_bytes=4_000)
        provider_receipt_ref_value: str | None = None
        remote_resource_id_value: str | None = None
        remote_status_value: str | None = None
        provider_observed_at_value: datetime | None = None
        confirmation_level_value: str | None = None
        if normalized_status == "succeeded":
            provider_receipt_ref_value = _text(
                provider_receipt_ref,
                "provider_receipt_ref",
                max_bytes=255,
            )
            remote_resource_id_value = _text(
                remote_resource_id,
                "remote_resource_id",
                max_bytes=255,
            )
            remote_status_value = _text(remote_status, "remote_status", max_bytes=64)
            provider_observed_at_value = _parse_datetime(provider_observed_at)
            confirmation_level_value = _normalize_confirmation_level(confirmation_level)
            provider_adapter_confirmed = (
                _provider_confirmation_token is _PROVIDER_ADAPTER_CONFIRMATION_TOKEN
            )
            if action_mode == "provider":
                # Direct human/API completion must never assert that a
                # provider write succeeded.  Only the internal adapter path
                # receives the opaque confirmation sentinel.
                if not provider_adapter_confirmed:
                    raise OperationsInvalidTransitionError(
                        "provider success must be confirmed by the provider adapter"
                    )
                if not _has_provider_success_evidence(
                    provider_receipt_ref=provider_receipt_ref_value,
                    remote_resource_id=remote_resource_id_value,
                    remote_status=remote_status_value,
                    provider_observed_at=provider_observed_at_value,
                    evidence_artifact_ids=evidence_ids,
                    evidence_note=evidence_note_value,
                ):
                    raise OperationsInvalidTransitionError(
                        "provider success requires a receipt or bounded remote postcondition"
                    )
                # ``None`` means the adapter used the default.  The default
                # for a provider result is machine confirmation, while an
                # explicit human label is never accepted on this path.
                if confirmation_level is None:
                    confirmation_level_value = "provider_confirmed"
                elif confirmation_level_value not in {"provider_confirmed", "reconciled"}:
                    raise OperationsInvalidTransitionError(
                        "provider success requires provider_confirmed or reconciled evidence"
                    )
            elif confirmation_level is not None and confirmation_level_value != "human_confirmed":
                # Manual completion may carry only the human confirmation
                # level.  Reconciled/provider labels are reserved for the
                # explicit reconcile or provider-adapter paths.
                raise OperationsInvalidTransitionError(
                    "manual completion may only use human_confirmed"
                )
        now = datetime.utcnow()
        attempt.status = normalized_status
        attempt.evidence_artifact_ids = [str(item) for item in evidence_ids]
        attempt.result_summary = result_summary_value
        attempt.evidence_note = evidence_note_value
        attempt.error_message = error_message_value
        attempt.finished_at = now
        action.status = normalized_status
        action.version = int(action.version or 1) + 1
        receipt = None
        if normalized_status == "succeeded":
            receipt = ExternalActionReceipt(
                action_id=action.id,
                attempt_id=attempt.id,
                owner_user_id=action.owner_user_id,
                action_version=int(action.action_version or 1),
                provider_receipt_ref=provider_receipt_ref_value,
                remote_resource_id=remote_resource_id_value,
                remote_url=remote_url_value,
                remote_status=remote_status_value,
                provider_observed_at=provider_observed_at_value,
                evidence_note=evidence_note_value,
                confirmation_level=confirmation_level_value,
                evidence_artifact_ids=[str(item) for item in evidence_ids],
            )
            session.add(receipt)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type=f"action.attempt.{normalized_status}",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "attempt_id": str(attempt.id),
                "status": normalized_status,
                "action_version": action.action_version,
                "version": action.version,
                "evidence_artifact_ids": [str(item) for item in evidence_ids],
                "receipt_id": str(receipt.id) if receipt is not None else None,
            },
        )
        if receipt is not None:
            await self._event(
                session,
                actor=actor,
                entity_type="action",
                entity_id=action.id,
                event_type="action.receipt.created",
                owner_user_id=action.owner_user_id,
                project_id=action.project_id,
                payload={
                    "attempt_id": str(attempt.id),
                    "receipt_id": str(receipt.id),
                    "action_version": action.action_version,
                    "confirmation_level": receipt.confirmation_level,
                },
            )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    finish_attempt = complete_attempt
    complete_action_attempt = complete_attempt

    async def reconcile_action(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        action_id: UUID | str | None = None,
        *,
        outcome: str,
        expected_version: int | None,
        evidence_artifact_ids: Sequence[Any],
        provider_receipt_ref: str | None = None,
        result_summary: str | None = None,
        remote_resource_id: str | None = None,
        remote_url: str | None = None,
        remote_status: str | None = None,
        provider_observed_at: datetime | str | None = None,
        evidence_note: str | None = None,
        confirmation_level: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or action_id is None:
            raise OperationsValidationError("actor and action_id are required")
        await self._assert_human(actor)
        action = await self._get_or_404(session, ExternalAction, action_id, "action_id", for_update=True)
        await self._assert_entity_access(session, actor, action, permission="write")
        await self._check_expected_version(action, expected_version)
        await self._assert_media_action_integrity(session, actor, action)
        if action.status != "uncertain":
            raise OperationsInvalidTransitionError("only uncertain actions can be reconciled")
        if getattr(action, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        normalized = str(outcome or "").strip().lower()
        if normalized in {"success", "succeeded", "confirmed_remote", "confirmed", "remote_confirmed"}:
            normalized = "succeeded"
        elif normalized in {"not_submitted", "duplicate", "failed", "failure"}:
            normalized = "failed"
        if normalized not in {"succeeded", "failed"}:
            raise OperationsValidationError("outcome must be succeeded or failed")
        evidence_ids = await self._validate_evidence_artifacts(
            session,
            actor,
            evidence_artifact_ids,
            project_id=action.project_id,
        )
        if not evidence_ids and not (evidence_note or reason):
            raise OperationsValidationError("reconcile requires an evidence artifact or evidence note")
        attempts = await self._scalars(
            session,
            select(ExternalActionAttempt)
            .where(ExternalActionAttempt.action_id == action.id)
            .order_by(ExternalActionAttempt.started_at.desc()),
        )
        unresolved = next((item for item in attempts if item.status == "uncertain"), None)
        if unresolved is None:
            raise OperationsInvalidTransitionError("no uncertain attempt is available for reconcile")
        if getattr(unresolved, "legacy_evidence_incomplete", False):
            raise OperationsConflictError("legacy_provider_evidence_incomplete")
        action_mode = _normalize_media_execution_mode(
            getattr(action, "execution_mode", None)
            or (
                action.payload_json.get("execution_mode", "manual")
                if isinstance(action.payload_json, Mapping)
                else "manual"
            )
        )
        generic_service_attempt = (
            getattr(action, "action_policy_revision_id", None) is not None
            and getattr(action, "registry_schema_version", None) is not None
        )
        if generic_service_attempt:
            from .external_action_execution_service import validate_service_attempt_reconciliation
            await validate_service_attempt_reconciliation(session, action, unresolved)
            if normalized == "failed" and (
                str(outcome or "").strip().lower() != "not_submitted"
                or remote_status != "not_submitted"
                or not _parse_datetime(provider_observed_at)
                or not (provider_receipt_ref or remote_resource_id)
            ):
                raise OperationsInvalidTransitionError(
                    "service failure reconciliation requires confirmed not_submitted provider lookup evidence"
                )
        if action_mode == "provider" and not generic_service_attempt and (
            unresolved.executor_type != "provider"
            or getattr(unresolved, "execution_mode", None) != "provider"
            or getattr(unresolved, "capability_snapshot_id", None) != getattr(action, "capability_snapshot_id", None)
            or str(getattr(unresolved, "capability_snapshot_hash", "") or "") != str(getattr(action, "capability_snapshot_hash", "") or "")
            or str(getattr(unresolved, "provider_key", "") or "") != str(getattr(action, "platform", "") or "")
            or str(getattr(unresolved, "provider_adapter_key", "") or "") != str(getattr(action, "adapter_key", "") or "")
            or str(getattr(unresolved, "provider_adapter_version", "") or "") != str(getattr(action, "adapter_version", "") or "")
            or str(getattr(unresolved, "credential_state_hash", "") or "") != str(getattr(action, "credential_state_hash", "") or "")
            or str(getattr(unresolved, "execution_key", "") or "") != str(getattr(action, "execution_key", "") or "")
        ):
            raise OperationsConflictError("provider attempt authority changed before reconcile")
        # Keep authority/prerequisite checks ahead of URL parsing while still
        # rejecting before mutating the unresolved attempt/action or creating
        # a receipt.
        remote_url_value = _validated_external_url(remote_url, "remote_url")
        # As with direct completion, normalize every persisted selected-path
        # field before touching the unresolved attempt or action.  A caller
        # that catches an exception and commits this session must observe no
        # partial reconciliation.
        result_summary_value = _text(result_summary, "result_summary", max_bytes=8_000)
        note_input = evidence_note if evidence_note not in (None, "") else reason
        evidence_note_value = _text(note_input, "evidence_note", max_bytes=8_000)
        reason_value = _text(reason, "reason", max_bytes=4_000)
        provider_receipt_ref_value: str | None = None
        remote_resource_id_value: str | None = None
        remote_status_value: str | None = None
        provider_observed_at_value: datetime | None = None
        confirmation_level_value: str | None = None
        if normalized == "succeeded":
            provider_receipt_ref_value = _text(
                provider_receipt_ref,
                "provider_receipt_ref",
                max_bytes=255,
            )
            remote_resource_id_value = _text(
                remote_resource_id,
                "remote_resource_id",
                max_bytes=255,
            )
            remote_status_value = _text(remote_status, "remote_status", max_bytes=64)
            provider_observed_at_value = _parse_datetime(provider_observed_at)
            confirmation_level_value = _normalize_confirmation_level(
                confirmation_level or "reconciled"
            )
            if action_mode == "provider":
                # Reconciliation is the human path for an ambiguous provider
                # outcome.  It must carry a real provider receipt or a remote
                # resource paired with bounded evidence/postcondition; a bare
                # ``outcome=succeeded``/free-form note cannot mint a receipt.
                if not _has_provider_success_evidence(
                    provider_receipt_ref=provider_receipt_ref_value,
                    remote_resource_id=remote_resource_id_value,
                    remote_status=remote_status_value,
                    provider_observed_at=provider_observed_at_value,
                    evidence_artifact_ids=evidence_ids,
                    evidence_note=evidence_note_value,
                ):
                    raise OperationsInvalidTransitionError(
                        "provider reconciliation requires a receipt or bounded remote postcondition"
                    )
                if confirmation_level_value != "reconciled":
                    raise OperationsInvalidTransitionError(
                        "human provider reconciliation must use reconciled confirmation"
                    )
        if generic_service_attempt and normalized == "succeeded" and (
            not provider_observed_at_value or not remote_status_value
            or not (provider_receipt_ref_value or remote_resource_id_value)
        ):
            raise OperationsInvalidTransitionError("service reconciliation requires a timestamped provider postcondition")
        nonexistent_evidence = None
        if generic_service_attempt and normalized == "failed":
            nonexistent_evidence = canonical_json({
                "provider_lookup_ref": _text(provider_receipt_ref or remote_resource_id, "provider_lookup_ref", max_bytes=255),
                "remote_status": "not_submitted", "observed_at": _parse_datetime(provider_observed_at).isoformat(),
                "evidence_note": evidence_note_value,
            })
        unresolved.status = normalized
        if evidence_ids:
            unresolved.evidence_artifact_ids = [str(item) for item in evidence_ids]
        if result_summary_value:
            unresolved.result_summary = result_summary_value
        if evidence_note_value:
            unresolved.evidence_note = evidence_note_value
        if reason_value:
            unresolved.error_message = reason_value
        if generic_service_attempt and normalized == "failed":
            unresolved.error_message = "provider_confirmed_not_submitted"
            unresolved.evidence_note = nonexistent_evidence
        unresolved.finished_at = unresolved.finished_at or datetime.utcnow()
        action.status = normalized
        action.version = int(action.version or 1) + 1
        receipt = None
        if normalized == "succeeded":
            receipt = ExternalActionReceipt(
                action_id=action.id,
                attempt_id=unresolved.id,
                owner_user_id=action.owner_user_id,
                action_version=int(action.action_version or 1),
                provider_receipt_ref=provider_receipt_ref_value,
                remote_resource_id=remote_resource_id_value,
                remote_url=remote_url_value,
                remote_status=remote_status_value,
                provider_observed_at=provider_observed_at_value,
                evidence_note=evidence_note_value,
                confirmation_level=confirmation_level_value,
                evidence_artifact_ids=[str(item) for item in evidence_ids],
            )
            session.add(receipt)
        await self._flush_only(session)
        await self._event(
            session,
            actor=actor,
            entity_type="action",
            entity_id=action.id,
            event_type=f"action.reconciled.{normalized}",
            owner_user_id=action.owner_user_id,
            project_id=action.project_id,
            payload={
                "attempt_id": str(unresolved.id),
                "outcome": normalized,
                "evidence_artifact_ids": [str(item) for item in evidence_ids],
                "version": action.version,
                "receipt_id": str(receipt.id) if receipt is not None else None,
            },
        )
        if receipt is not None:
            await self._event(
                session,
                actor=actor,
                entity_type="action",
                entity_id=action.id,
                event_type="action.receipt.created",
                owner_user_id=action.owner_user_id,
                project_id=action.project_id,
                payload={
                    "attempt_id": str(unresolved.id),
                    "receipt_id": str(receipt.id),
                    "action_version": action.action_version,
                    "confirmation_level": receipt.confirmation_level,
                    "reconciled": True,
                },
            )
        await self._flush_commit(session)
        return await self.get_action(session, actor, action.id)

    reconcile = reconcile_action

    async def list_events(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        entity_type: str,
        entity_id: UUID | str,
        limit: int = _ACTION_TIMELINE_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise OperationsValidationError("actor is required")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _ACTION_TIMELINE_LIMIT
        ):
            raise OperationsValidationError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise OperationsValidationError("offset must be a non-negative integer")
        parsed = _as_uuid(entity_id, "entity_id")
        assert parsed is not None
        rows = await self._scalars(
            session,
            select(OperationEvent)
            .where(OperationEvent.entity_type == str(entity_type), OperationEvent.entity_id == parsed)
            .order_by(OperationEvent.created_at.desc(), OperationEvent.id.desc())
            .offset(offset)
            .limit(limit),
        )
        result: list[dict[str, Any]] = []
        # Fetch the newest page while rendering each page chronologically.
        for row in reversed(rows):
            await self._assert_entity_access(session, actor, row, permission="read")
            result.append(row.to_safe_dict())
        return result


# Public exception aliases used by routers/integrations.
OperationsServiceError = OperationsError
AuthorizationError = OperationsAuthorizationError
ConflictError = OperationsConflictError
StaleVersionError = OperationsStaleVersionError


__all__ = [
    "OperationsService",
    "OperationsError",
    "OperationsServiceError",
    "OperationsNotFoundError",
    "OperationsAuthorizationError",
    "AuthorizationError",
    "OperationsConflictError",
    "ConflictError",
    "OperationsStaleVersionError",
    "StaleVersionError",
    "OperationsValidationError",
    "OperationsInvalidTransitionError",
    "OperationsHumanRequiredError",
    "MEDIA_ACTION_TYPES",
    "MEDIA_PLATFORM_VALUES",
    "MEDIA_ADAPTER_STATUS",
    "canonical_json",
    "canonicalize_payload",
    "canonicalize_json",
    "default_opportunity_title",
    "safe_opportunity_title",
    "sha256_bytes",
    "sha256_text",
    "sha256_json",
    "payload_hash",
]
