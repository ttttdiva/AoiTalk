"""Direct, ACL-aware Operations tools for the root LLM runtime.

These tools are intentionally a narrow facade over :class:`OperationsService`.
The service remains the authorization and optimistic-version boundary; this
module only resolves the authenticated turn principal, opens a short-lived DB
session, and returns LLM-safe projections.  In particular, source snapshots,
credential references, and application bodies are never returned by the
read-oriented projections.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional
from uuid import UUID

from ..memory.database import get_database_manager
from ..memory.models.operations import (
    _is_http_url_candidate,
    default_opportunity_title,
    safe_opportunity_title,
    sanitize_source_url,
)
from ..services.operations_service import OperationsService
from ..services.project_context import get_runtime_project_context
from ..services.turn_context import get_turn_context
from .core import ToolDefinition, ToolParam, tool

logger = logging.getLogger(__name__)

# Keep model-facing source ingestion bounded below the service's hard limit.
# The service performs the authoritative validation again.
MAX_SOURCE_TEXT_BYTES = 2_000_000
MAX_SOURCE_URL_BYTES = 4_000
MAX_TITLE_BYTES = 1_000
MAX_MESSAGE_BYTES = 2_000_000

# ``create_generation_plan`` is the only Generation Studio mutation exposed to
# the model.  Keep its request spec semantic and closed: model selection and
# bounded media parameters are useful for planning, while provider workflow
# graphs, paths, credentials, and execution/charge controls are not.  The
# service remains the authoritative validator; this allowlist is an earlier
# model-boundary guard so a future service extension cannot accidentally make
# an unsafe key model-facing.
_GENERATION_PLAN_REQUEST_KEYS = frozenset(
    {
        "prompt",
        "negative_prompt",
        "seed",
        # ``image_model_selection_id`` is the historical image spelling.
        # ``model_selection_id`` is the generic spelling used by video plans
        # (for example ``wsl_...`` selectors).
        "image_model_selection_id",
        "model_selection_id",
        "generation_settings",
        "size_preset_id",
        "width",
        "height",
        "aspect_ratio",
        "duration_seconds",
        "frame_count",
        "storyboard",
        # Existing HTTP plan DTOs retain this as a semantic plan hint.  It is
        # never accepted as an execution authority here; only ``False`` is
        # permitted by the model-facing tool.
        "accept_metered_generation",
    }
)

# These names must not become a backdoor for paid execution if a caller sends
# an arbitrary JSON object as ``request_spec``.  Match normalized spellings so
# camelCase, hyphenated, and underscored variants are all rejected.
_GENERATION_PLAN_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "accept_metered_generation",
        "acknowledge_metered_generation",
        "acknowledge_paid_generation",
        "accept_paid_generation",
        "allow_metered_generation",
        "approve_generation_cost",
        "approve_paid_generation",
        "confirm_generation_cost",
        "confirm_paid_generation",
        "cost_confirmation",
        "paid_generation",
        "paid_execution",
        "charge_authorization",
        "submit_generation",
        "reconcile_generation",
        "select_generation_output",
    }
)


def _generation_plan_key_token(value: Any) -> str:
    """Normalize a request-spec key for the boundary denylist."""

    return re.sub(
        r"[^a-z0-9]+",
        "_",
        str(value or "").strip().casefold(),
    ).strip("_")


def _generation_plan_key_is_forbidden(value: Any) -> bool:
    token = _generation_plan_key_token(value)
    compact = token.replace("_", "")
    if token in _GENERATION_PLAN_FORBIDDEN_KEY_TOKENS:
        return True
    # Catch future spelling variants (camelCase, ``paid``/``charge`` aliases,
    # or execution verbs) without rejecting ordinary semantic fields.
    return any(
        marker in compact
        for marker in (
            "acknowledgemetered",
            "acknowledgepaid",
            "acceptmetered",
            "acceptpaid",
            "allowmetered",
            "approvegenerationcost",
            "approvepaid",
            "confirmgenerationcost",
            "confirmpaid",
            "costconfirmation",
            "chargeauthorization",
            "paidgeneration",
            "paidexecution",
            "submitgeneration",
            "reconcilegeneration",
            "selectgenerationoutput",
            "provider",
            "workflow",
            "graph",
            "path",
            "credential",
            "secret",
            "password",
            "authorization",
            "bearer",
            "token",
        )
    )


def _safe_generation_plan_request_spec(value: Any) -> dict[str, Any]:
    """Return a bounded, model-safe GenerationPlan request specification.

    This is intentionally a shallow semantic projection.  The Generation
    service performs the canonical validation and persists the normalized
    plan; this wrapper prevents model-controlled JSON from introducing
    provider workflow material or a paid-generation acknowledgement.  The
    existing ``accept_metered_generation`` field is retained for DTO
    compatibility but only its non-authoritative ``False`` value is allowed
    through the model-facing path.
    """

    if not isinstance(value, Mapping):
        raise ValueError("request_spec must be an object")

    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("request_spec contains an invalid field")
        key = raw_key.strip()
        if not key:
            raise ValueError("request_spec contains a forbidden field")
        if key == "accept_metered_generation":
            # A model may preserve the explicit default in a plan payload, but
            # it cannot signal consent to a metered/paid execution.
            if raw_value is not False:
                raise ValueError(
                    "request_spec cannot acknowledge paid generation"
                )
            result[key] = False
            continue
        if _generation_plan_key_is_forbidden(key):
            raise ValueError("request_spec contains a forbidden field")
        if key not in _GENERATION_PLAN_REQUEST_KEYS:
            raise ValueError("request_spec contains unsupported fields")

        if key == "generation_settings":
            if not isinstance(raw_value, Mapping):
                raise ValueError("request_spec.generation_settings must be an object")
            if len(raw_value) > 32:
                raise ValueError("request_spec.generation_settings has too many fields")
            settings: dict[str, Any] = {}
            for setting_key, setting_value in raw_value.items():
                if not isinstance(setting_key, str) or not setting_key.strip():
                    raise ValueError("generation_settings contains an invalid field")
                if _generation_plan_key_is_forbidden(setting_key):
                    raise ValueError("generation_settings contains a forbidden field")
                if isinstance(setting_value, (Mapping, Sequence)) and not isinstance(
                    setting_value,
                    (str, bytes, bytearray),
                ):
                    raise ValueError("generation_settings values must be scalar")
                if not (
                    setting_value is None
                    or isinstance(setting_value, (str, int, float, bool))
                ):
                    raise ValueError("generation_settings values must be scalar")
                settings[setting_key.strip()] = setting_value
            result[key] = settings
            continue

        if key == "storyboard":
            if isinstance(raw_value, (str, bytes, bytearray)) or not isinstance(
                raw_value,
                Sequence,
            ):
                raise ValueError("request_spec.storyboard must be a list")
            if len(raw_value) > 100:
                raise ValueError("request_spec.storyboard has too many items")
            storyboard: list[str] = []
            for item in raw_value:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("request_spec.storyboard items must be non-empty strings")
                if len(item.encode("utf-8")) > 8_000:
                    raise ValueError("request_spec.storyboard item is too long")
                storyboard.append(item)
            result[key] = storyboard
            continue

        if key in {"duration_seconds", "frame_count"}:
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError(f"request_spec.{key} must be an integer")
            maximum = 86_400 if key == "duration_seconds" else 100_000
            if raw_value < 1 or raw_value > maximum:
                raise ValueError(f"request_spec.{key} is out of range")
            result[key] = raw_value
            continue

        if key in {"image_model_selection_id", "model_selection_id"}:
            if not isinstance(raw_value, str):
                raise ValueError(f"request_spec.{key} must be an opaque selector")
            selector = raw_value.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{1,254}", selector):
                raise ValueError(f"request_spec.{key} must be an opaque selector")
            result[key] = selector
            continue

        result[key] = raw_value

    # Supplying both spellings with different selectors is ambiguous and can
    # produce a different plan hash depending on service-version behavior.
    image_selector = result.get("image_model_selection_id")
    generic_selector = result.get("model_selection_id")
    if image_selector is not None and generic_selector is not None:
        if str(image_selector).strip() != str(generic_selector).strip():
            raise ValueError(
                "request_spec model selection fields conflict"
            )
    return result


def _turn_actor() -> dict[str, Any]:
    """Resolve an agent actor from the server-bound TurnContext.

    The model cannot provide a user id or role as a tool argument.  The
    OperationsService re-checks this actor's ACL for every call.
    """

    turn = get_turn_context()
    raw_user_id = str(getattr(turn, "user_id", None) or "").strip()
    if not raw_user_id:
        raise PermissionError("authenticated turn identity is required")
    try:
        # Validate before handing the value to SQLAlchemy so malformed context
        # cannot be interpreted as a different identity by a backend adapter.
        UUID(raw_user_id)
    except (TypeError, ValueError) as exc:
        raise PermissionError("authenticated turn identity is invalid") from exc

    actor: dict[str, Any] = {
        "id": raw_user_id,
        "user_id": raw_user_id,
        "actor_type": "agent",
        "is_agent": True,
    }
    runtime_context = get_runtime_project_context()
    if isinstance(runtime_context, Mapping):
        role = runtime_context.get("user_role", runtime_context.get("role"))
        if role:
            actor["role"] = str(role)
        if runtime_context.get("is_admin") is True:
            actor["role"] = "admin"
    return actor


def _effective_project_id(requested: str | None) -> str | None:
    """Bind an optional argument to the server-selected Project scope."""

    requested_value = str(requested or "").strip() or None
    bound_value = str(getattr(get_turn_context(), "project_id", None) or "").strip() or None
    if requested_value and bound_value:
        try:
            same_project = UUID(requested_value) == UUID(bound_value)
        except (TypeError, ValueError):
            same_project = requested_value.casefold() == bound_value.casefold()
        if not same_project:
            raise PermissionError("requested Project does not match the authenticated turn scope")
    return requested_value or bound_value


def _validate_text(value: str | None, label: str, max_bytes: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if required and not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds maximum size")
    return value


def _derive_title(title: str | None, source_url: str | None, source_text: str | None) -> str:
    candidate = _validate_text(title, "title", MAX_TITLE_BYTES) or ""
    if candidate.strip():
        return candidate
    url_value = (source_url or "").strip()
    if url_value:
        candidate = default_opportunity_title(url_value)
    else:
        # Derive a short label from the first non-empty source line.  This is
        # only persisted as the opportunity title and is independently bounded.
        candidate = next((line.strip() for line in (source_text or "").splitlines() if line.strip()), "Opportunity")
        if _is_http_url_candidate(candidate):
            candidate = default_opportunity_title(candidate)
    encoded = candidate.encode("utf-8")
    if len(encoded) > MAX_TITLE_BYTES:
        candidate = encoded[:MAX_TITLE_BYTES].decode("utf-8", "ignore").rstrip()
    return candidate or "Opportunity"


def _safe_connection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the connection projection (never credential references)."""

    return {
        key: value.get(key)
        for key in (
            "id",
            "owner_user_id",
            "project_id",
            "provider_key",
            "display_name",
            "remote_account_ref",
            "auth_status",
            "version",
            "created_at",
            "updated_at",
        )
        if key in value
    }


def _safe_opportunity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist an opportunity without retaining its source body."""

    result = {
        key: value.get(key)
        for key in (
            "id",
            "owner_user_id",
            "project_id",
            "connection_id",
            "title",
            "source_url",
            "source_snapshot_hash",
            "source_hash",
            "source_untrusted",
            "status",
            "created_by",
            "created_at",
            "updated_at",
        )
        if key in value
    }
    raw_source_url = result.get("source_url")
    raw_source_text = value.get("source_text")
    if "source_url" in result:
        result["source_url"] = sanitize_source_url(raw_source_url)
    if "title" in result:
        result["title"] = safe_opportunity_title(
            result.get("title"),
            raw_source_url,
            raw_source_text,
        )
    return result


def _safe_evaluation(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "opportunity_id",
            "owner_user_id",
            "project_id",
            "version",
            "estimated_effort_hours",
            "estimated_cost",
            "estimated_revenue",
            "fit",
            "risks",
            "missing_requirements",
            "summary",
            "evidence_refs",
            "created_by",
            "created_at",
        )
        if key in value
    }


def _safe_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose draft identity/version/hash but not its application message."""

    return {
        key: value.get(key)
        for key in (
            "id",
            "opportunity_id",
            "owner_user_id",
            "project_id",
            "version",
            "offered_price",
            "currency",
            "delivery_estimate",
            "artifact_version_ids",
            "draft_hash",
            "created_by",
            "created_at",
        )
        if key in value
    }


def _safe_action(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose proposal state without payload bodies, receipts, or credentials."""

    result = {
        key: value.get(key)
        for key in (
            "id",
            "owner_user_id",
            "project_id",
            "opportunity_id",
            "connection_id",
            "application_draft_id",
            "content_item_id",
            "content_variant_id",
            "content_variant_revision_id",
            "persona_revision_id",
            "platform_account_id",
            "platform_account_revision_id",
            "platform",
            "action_type",
            "payload_hash",
            "artifact_hashes",
            "action_version",
            "version",
            "status",
            "created_by",
            "created_at",
            "updated_at",
        )
        if key in value
    }
    payload = value.get("payload")
    if isinstance(payload, Mapping):
        connection = payload.get("connection")
        if isinstance(connection, Mapping):
            result["payload"] = {
                "connection": {
                    key: connection.get(key)
                    for key in ("id", "version", "provider_key", "remote_account_ref")
                    if key in connection
                }
            }
    timeline = value.get("timeline")
    if isinstance(timeline, Sequence) and not isinstance(timeline, (str, bytes, bytearray)):
        safe_timeline: list[dict[str, Any]] = []
        for item in timeline:
            if not isinstance(item, Mapping):
                continue
            safe_timeline.append(
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "event_type",
                        "status",
                        "actor_id",
                        "actor_type",
                        "created_at",
                    )
                    if key in item
                }
            )
        result["timeline"] = safe_timeline
    return result


async def _invoke_media_service(service_instance: Any, method_name: str, **kwargs: Any) -> Any:
    """Invoke one MediaOps service method in the authenticated agent scope."""

    actor = _turn_actor()
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        method = getattr(service_instance, method_name, None)
        if not callable(method):
            raise RuntimeError("MediaOps operation is unavailable")
        result = method(session, actor, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def _safe_media_action(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an agent-safe MediaOps action summary without payload bodies."""

    safe = _safe_action(value)
    safe.pop("payload", None)
    safe.pop("source_url", None)
    safe["media_action"] = True
    return safe


def _safe_opportunity_detail(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _safe_opportunity(value)
    evaluations = value.get("evaluations")
    if isinstance(evaluations, Sequence) and not isinstance(evaluations, (str, bytes, bytearray)):
        result["evaluations"] = [
            _safe_evaluation(item) for item in evaluations if isinstance(item, Mapping)
        ]
    drafts = value.get("drafts")
    if isinstance(drafts, Sequence) and not isinstance(drafts, (str, bytes, bytearray)):
        result["drafts"] = [_safe_draft(item) for item in drafts if isinstance(item, Mapping)]
    return result


async def _invoke(service_method: str, **kwargs: Any) -> Any:
    actor = _turn_actor()
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        return await getattr(OperationsService(), service_method)(session, actor, **kwargs)
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


@tool
async def operations_list_connections(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List manual external connections accessible to the authenticated turn."""

    rows = await _invoke("list_connections", project_id=_effective_project_id(project_id))
    return [_safe_connection(item) for item in rows if isinstance(item, Mapping)]


@tool
async def operations_create_opportunity(
    source_url: Optional[str] = None,
    source_text: Optional[str] = None,
    title: Optional[str] = None,
    connection_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create an opportunity from bounded, untrusted user-supplied URL/text."""

    url_value = _validate_text(source_url, "source_url", MAX_SOURCE_URL_BYTES)
    text_value = _validate_text(source_text, "source_text", MAX_SOURCE_TEXT_BYTES)
    if not url_value and not text_value:
        raise ValueError("source_url or source_text is required")
    project_value = _effective_project_id(project_id)
    title_value = _derive_title(title, url_value, text_value)
    result = await _invoke(
        "create_opportunity",
        title=title_value,
        source_url=url_value,
        source_text=text_value,
        connection_id=connection_id,
        project_id=project_value,
    )
    safe = _safe_opportunity(result if isinstance(result, Mapping) else {})
    # Return a compact summary rather than source text.  The hash is retained
    # as the stable evidence identity for later evaluation/draft operations.
    return {
        "opportunity_id": safe.get("id"),
        "id": safe.get("id"),
        "project_id": safe.get("project_id"),
        "connection_id": safe.get("connection_id"),
        "title": safe.get("title"),
        "source_url": safe.get("source_url"),
        "source_snapshot_hash": safe.get("source_snapshot_hash") or safe.get("source_hash"),
        "status": safe.get("status"),
        "source_untrusted": True,
        "summary": "Opportunity created; source content is retained only as a hashed untrusted snapshot.",
    }


@tool
async def operations_record_evaluation(
    opportunity_id: str,
    estimated_effort_hours: Optional[float] = None,
    estimated_cost: Optional[float] = None,
    estimated_revenue: Optional[float] = None,
    fit: Optional[str] = None,
    risks: Optional[list[Any]] = None,
    missing_requirements: Optional[list[Any]] = None,
    summary: Optional[str] = None,
    evidence_refs: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Record the next immutable evaluation version for an opportunity."""

    result = await _invoke(
        "create_evaluation",
        opportunity_id=opportunity_id,
        estimated_effort_hours=estimated_effort_hours,
        estimated_cost=estimated_cost,
        estimated_revenue=estimated_revenue,
        fit=fit,
        risks=risks,
        missing_requirements=missing_requirements,
        summary=summary,
        evidence_refs=evidence_refs,
    )
    return _safe_evaluation(result if isinstance(result, Mapping) else {})


@tool
async def operations_create_application_draft(
    opportunity_id: str,
    message: str,
    offered_price: Optional[float] = None,
    currency: Optional[str] = None,
    delivery_estimate: Optional[str] = None,
    artifact_version_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Create a versioned application draft for an accessible opportunity."""

    message_value = _validate_text(message, "message", MAX_MESSAGE_BYTES, required=True)
    result = await _invoke(
        "create_draft",
        opportunity_id=opportunity_id,
        message=message_value,
        offered_price=offered_price,
        currency=currency,
        delivery_estimate=delivery_estimate,
        artifact_version_ids=artifact_version_ids,
    )
    safe = _safe_draft(result if isinstance(result, Mapping) else {})
    return {
        **safe,
        "draft_id": safe.get("id"),
        "summary": "Versioned application draft created; the message body is not included in durable tool output.",
    }


@tool
async def operations_propose_action(
    connection_id: str,
    application_draft_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Propose a submit-application action bound to an exact draft version."""

    key_value = _validate_text(idempotency_key, "idempotency_key", 255, required=True)
    result = await _invoke(
        "create_action",
        connection_id=connection_id,
        application_draft_id=application_draft_id,
        idempotency_key=key_value,
    )
    safe = _safe_action(result if isinstance(result, Mapping) else {})
    return {
        **safe,
        "action_id": safe.get("id"),
        "summary": "Action proposal created and awaits explicit human approval before any execution.",
    }


@tool
async def operations_get_opportunity(
    opportunity_id: str,
    include_versions: bool = True,
) -> dict[str, Any]:
    """Read accessible opportunity state and safe evaluation/draft versions."""

    result = await _invoke(
        "get_opportunity",
        opportunity_id=opportunity_id,
        include_versions=bool(include_versions),
    )
    return _safe_opportunity_detail(result if isinstance(result, Mapping) else {})


@tool
async def operations_get_action(action_id: str) -> dict[str, Any]:
    """Read accessible action proposal state without payload or receipt bodies."""

    result = await _invoke("get_action", action_id=action_id)
    return _safe_action(result if isinstance(result, Mapping) else {})


@tool
async def media_operations_get_adapter_status() -> dict[str, Any]:
    """Read the provider-neutral manual adapter status for all six platforms."""

    result = await _invoke_media_service(
        OperationsService(),
        "get_media_adapter_status",
    )
    if not isinstance(result, Mapping):
        return {"provider_calls": False, "platforms": {}}
    # Adapter status is intentionally deterministic and contains no secrets;
    # copy only the stable surface rather than forwarding arbitrary metadata.
    return {
        "schema_version": result.get("schema_version"),
        "provider_calls": False,
        "platforms": dict(result.get("platforms") or {}),
    }


def _safe_media_persona(value: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """Keep Persona projections useful while excluding arbitrary provider data."""

    safe = {
        key: value.get(key)
        for key in (
            "id",
            "owner_user_id",
            "project_id",
            "state",
            "parent_brand_ref",
            "create_hash",
            "created_by",
            "created_at",
        )
        if key in value
    }
    revision = value.get("current_revision")
    if isinstance(revision, Mapping):
        revision_keys = (
            "id", "version", "display_name", "summary", "voice", "audience",
            "niche", "positioning", "platforms", "content_pillars", "public_aliases",
            "visual_identity", "creative_direction", "allowed_subjects",
            "prohibited_subjects", "adult_policy", "sensitive_policy", "ip_policy",
            "disclosure_policy", "monetization_policy", "kpi_objectives",
            "default_language", "locale", "timezone", "research_policy",
            "image_production_policy", "video_production_policy", "content_hash",
            "created_at",
        )
        safe["current_revision"] = {
            key: (
                _safe_policy_projection(revision.get(key))
                if key
                in {
                    "visual_identity",
                    "allowed_subjects",
                    "prohibited_subjects",
                    "monetization_policy",
                    "kpi_objectives",
                    "research_policy",
                    "image_production_policy",
                    "video_production_policy",
                    "public_aliases",
                    "platforms",
                    "content_pillars",
                }
                else revision.get(key)
            )
            for key in revision_keys
            if key in revision
        }
    if detail and isinstance(value.get("revisions"), Sequence):
        safe["revisions"] = [
            {
                key: item.get(key)
                for key in ("id", "version", "display_name", "content_hash", "created_at")
                if key in item
            }
            for item in value["revisions"]
            if isinstance(item, Mapping)
        ][:100]
        safe["revision_history_truncated"] = bool(value.get("revision_history_truncated"))
    return safe


def _safe_media_account(value: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id", "owner_user_id", "project_id", "persona_id", "connection_id",
            "account_type", "remote_url", "status", "platform", "account_ref",
            "create_hash", "created_by", "created_at",
        )
        if key in value
    }
    revision = value.get("current_revision")
    if isinstance(revision, Mapping):
        safe["current_revision"] = {
            key: revision.get(key)
            for key in (
                "id", "version", "display_name", "remote_url", "locale", "timezone",
                "supported_content_modes", "disclosure_defaults", "rating_defaults",
                "adapter_ref", "publish_capability", "media_capability",
                "analytics_capability", "credential_status", "content_hash", "created_at",
            )
            if key in revision
        }
    if detail and isinstance(value.get("revisions"), Sequence):
        safe["revisions"] = [
            {
                key: item.get(key)
                for key in ("id", "version", "display_name", "content_hash", "created_at")
                if key in item
            }
            for item in value["revisions"]
            if isinstance(item, Mapping)
        ][:100]
        safe["revision_history_truncated"] = bool(value.get("revision_history_truncated"))
    return safe


def _safe_media_routine(value: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id", "owner_user_id", "project_id", "persona_id", "platform_account_id",
            "state", "enabled", "last_due_at", "next_due_at", "create_hash",
            "created_by", "created_at",
        )
        if key in value
    }
    revision = value.get("current_revision")
    if isinstance(revision, Mapping):
        safe["current_revision"] = {
            key: revision.get(key)
            for key in (
                "id", "version", "name", "objective", "questions", "target_platforms",
                "cadence", "timezone", "schedule", "source_types", "search_queries",
                "domains", "follow_accounts", "follow_tags", "exclusions",
                "freshness_hours", "max_candidates", "review_policy", "content_hash",
                "created_at",
            )
            if key in revision
        }
    if detail and isinstance(value.get("revisions"), Sequence):
        safe["revisions"] = [
            {
                key: item.get(key)
                for key in ("id", "version", "name", "cadence", "content_hash", "created_at")
                if key in item
            }
            for item in value["revisions"]
            if isinstance(item, Mapping)
        ][:100]
    return safe


def _safe_media_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id", "owner_user_id", "project_id", "research_routine_id", "research_run_id",
            "routine_revision_id", "candidate_key", "title", "summary", "source_published_at",
            "discovered_at", "expires_at", "relevance_score", "freshness_score", "reason",
            "status", "content_item_id", "candidate_hash", "created_by", "created_at", "updated_at",
            "decision_version", "review_state", "ranking_score", "ranking_factors", "ranking_rationale",
            "ranking_policy_revision_id", "ranking_policy_content_hash",
        )
        if key in value
    }
    if "source_url" in value:
        safe["source_url"] = sanitize_source_url(value.get("source_url"))
    # Evidence is business material from an untrusted source.  Preserve only
    # stable provenance references and mark it as non-authoritative.
    evidence = value.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes, bytearray)):
        evidence_refs: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            # Service projections have used both the wire evidence keys and
            # normalized provenance keys over time.  Normalize both forms
            # here, while never returning an unsanitized URL or source body.
            row: dict[str, Any] = {}
            evidence_type = item.get("type", item.get("evidence_type"))
            if isinstance(evidence_type, str) and evidence_type.strip():
                row["type"] = evidence_type.strip()
            source_url = item.get("url", item.get("source_url"))
            if source_url is not None:
                row["url"] = sanitize_source_url(source_url)
            artifact_sha = item.get("sha256", item.get("artifact_sha256"))
            if artifact_sha is not None:
                row["sha256"] = artifact_sha
            if "evidence_hash" in item:
                row["evidence_hash"] = item.get("evidence_hash")
            if row:
                evidence_refs.append(row)
        safe["evidence_refs"] = evidence_refs[:20]
    safe["source_untrusted"] = True
    return safe


def _safe_media_candidate_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist immutable candidate-decision metadata.

    The decision ledger may retain a bounded candidate snapshot for audit, but
    that snapshot is deliberately not exposed to the model-facing tool.  IDs,
    hashes, statuses and actor type are sufficient for an auditable read while
    avoiding source text or provider payload leakage.
    """

    return {
        key: value.get(key)
        for key in (
            "id",
            "candidate_id",
            "sequence",
            "event_type",
            "from_status",
            "to_status",
            "reason",
            "candidate_hash",
            "candidate_snapshot_hash",
            "request_hash",
            "actor_id",
            "actor_type",
            "content_item_id",
            "decision_hash",
            "prev_decision_hash",
            "prev_event_hash",
            "event_hash",
            "decided_at",
            "created_at",
        )
        if key in value
    }


def _safe_media_content(value: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id", "owner_user_id", "project_id", "editorial_program_id", "title", "brief",
            "version", "status", "persona_revision_id", "objective", "content_type",
            "content_pillar", "intended_audience", "source_refs", "candidate_refs",
            "desired_assets", "monetization_ref", "experiment_ref", "scheduled_at",
            "content_hash", "created_by", "created_at", "source_finding_ids", "source_candidate_ids",
        )
        if key in value
    }
    if detail:
        safe["source_candidates"] = [
            _safe_media_candidate(item)
            for item in (value.get("source_candidates") or [])
            if isinstance(item, Mapping)
        ][:20]
    return safe


# ---------------------------------------------------------------------------
# Character-first Agent facade projections
# ---------------------------------------------------------------------------

# The service/HTTP dashboard is already ACL-scoped, but the model-facing
# boundary remains an independent contract.  Keep a second, deliberately
# closed projection here so a future service field (for example a provider
# path or a credential reference) cannot become visible merely by being added
# to a dashboard DTO.
_CHARACTER_PAGE_LIMIT = 100
_CHARACTER_PAGE_MAX_OFFSET = 10_000_000


def _character_page_args(limit: Any, offset: Any) -> tuple[int, int]:
    """Validate the bounded page arguments used by Character tools."""

    if isinstance(limit, bool) or isinstance(offset, bool):
        raise ValueError("limit and offset must be integers")
    try:
        page_limit = int(limit)
        page_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    if page_limit < 1 or page_limit > _CHARACTER_PAGE_LIMIT:
        raise ValueError("limit must be between 1 and 100")
    if page_offset < 0 or page_offset > _CHARACTER_PAGE_MAX_OFFSET:
        raise ValueError("offset must be a non-negative integer")
    return page_limit, page_offset


def _safe_page_projection(
    value: Any,
    *,
    projector: Any = None,
    default_limit: int = _CHARACTER_PAGE_LIMIT,
) -> dict[str, Any]:
    """Project one dashboard page while retaining pagination metadata only."""

    raw = value if isinstance(value, Mapping) else {}
    raw_items = raw.get("items", [])
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        raw_items = []
    try:
        page_limit = int(raw.get("limit", default_limit))
    except (TypeError, ValueError):
        page_limit = default_limit
    page_limit = max(1, min(_CHARACTER_PAGE_LIMIT, page_limit))
    try:
        page_offset = int(raw.get("offset", 0))
    except (TypeError, ValueError):
        page_offset = 0
    page_offset = max(0, min(_CHARACTER_PAGE_MAX_OFFSET, page_offset))
    try:
        count = int(raw.get("count", raw.get("total", len(raw_items))))
    except (TypeError, ValueError):
        count = len(raw_items)
    count = max(0, count)
    safe_items: list[dict[str, Any]] = []
    for item in list(raw_items)[:_CHARACTER_PAGE_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        projected = projector(item) if callable(projector) else dict(item)
        if isinstance(projected, Mapping):
            safe_items.append(dict(projected))
    return {
        "items": safe_items,
        "count": count,
        "limit": page_limit,
        "offset": page_offset,
        "has_more": bool(raw.get("has_more", page_offset + page_limit < count)),
    }


_SAFE_CAPABILITY_KEYS = frozenset(
    {
        "identity_verification",
        "oauth",
        "api_token",
        "cookie_export",
        "text_posting",
        "image_posting",
        "video_posting",
        "scheduling",
        "edit_delete",
        "analytics",
        "revenue",
        "token_refresh",
        "revoke",
        "remote_reconciliation",
        "publish",
        "status",
        "mode",
        "provider_calls",
        "available",
        "verified",
        "reason",
    }
)


def _safe_capability_projection(value: Any, *, depth: int = 0) -> Any:
    """Keep capability/status values while excluding credential material."""

    if depth > 2:
        return None
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            token = _generation_plan_key_token(key)
            if token not in _SAFE_CAPABILITY_KEYS:
                continue
            projected = _safe_capability_projection(raw_value, depth=depth + 1)
            if projected is not None:
                safe[key] = projected
        return safe
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


_POLICY_FORBIDDEN_MARKERS = (
    "secret",
    "token",
    "password",
    "cookie",
    "credential",
    "authorization",
    "bearer",
    "api_key",
    "access_key",
    "private_key",
    "provider",
    "workflow",
    "graph",
    "filesystem",
    "file_path",
    "local_path",
)


def _safe_policy_projection(value: Any, *, depth: int = 0) -> Any:
    """Bound policy JSON and remove secret/provider implementation markers."""

    if depth > 3:
        return None
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            token = _generation_plan_key_token(key)
            if not key or any(marker in token for marker in _POLICY_FORBIDDEN_MARKERS):
                continue
            projected = _safe_policy_projection(raw_value, depth=depth + 1)
            if projected is not None:
                safe[key] = projected
        return safe
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items = [
            _safe_policy_projection(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
        return [item for item in projected_items if item is not None]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _safe_character_account(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project an account binding without remote credentials or provider data."""

    safe = {
        key: value.get(key)
        for key in ("id", "platform", "account_ref", "status")
        if key in value
    }
    for key in (
        "capability",
        "capabilities",
        "media_capability",
        "publish_capability",
        "analytics_capability",
        "credential_status",
    ):
        if key in value:
            safe[key] = _safe_capability_projection(value.get(key))
    # Some service versions expose capabilities only under current_revision.
    revision = value.get("current_revision")
    if isinstance(revision, Mapping):
        for key in (
            "media_capability",
            "publish_capability",
            "analytics_capability",
            "credential_status",
        ):
            if key in revision and key not in safe:
                safe[key] = _safe_capability_projection(revision.get(key))
    return safe


def _safe_dashboard_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "title",
            "summary",
            "status",
            "review_state",
            "reason",
            "candidate_hash",
            "decision_version",
            "content_item_id",
            "latest_decision",
            "discovered_at",
            "expires_at",
            "relevance_score",
            "freshness_score",
            "ranking_score",
            "ranking_factors",
            "ranking_rationale",
            "source_untrusted",
            "evidence_refs",
        )
        if key in value
    }


def _safe_dashboard_content(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "title",
            "status",
            "scheduled_at",
            "content_type",
            "content_item_id",
            "content_hash",
            "persona_revision_id",
        )
        if key in value
    }


def _safe_dashboard_variant(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "content_variant_id",
            "content_item_id",
            "platform",
            "version",
            "created_at",
        )
        if key in value
    }


def _safe_dashboard_assessment(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "content_variant_id",
            "content_variant_revision_id",
            "result",
            "created_at",
        )
        if key in value
    }


def _safe_dashboard_publication(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id",
            "action_type",
            "status",
            "content_item_id",
            "content_variant_id",
            "content_variant_revision_id",
            "persona_revision_id",
            "platform_account_id",
            "platform",
            "created_at",
        )
        if key in value
    }
    receipts = value.get("receipts")
    if isinstance(receipts, Sequence) and not isinstance(
        receipts, (str, bytes, bytearray)
    ):
        safe["receipts"] = [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "action_id",
                    "action_version",
                    "confirmation_level",
                    "remote_status",
                    "created_at",
                )
                if key in item
            }
            for item in receipts[:100]
            if isinstance(item, Mapping)
        ]
    return safe


def _safe_dashboard_metric(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id",
            "persona_ref",
            "platform_account_ref",
            "content_variant_ref",
            "publication_ref",
            "observed_at",
        )
        if key in value
    }
    metrics = value.get("metrics")
    if isinstance(metrics, Mapping):
        safe["metrics"] = {
            str(key): metric
            for key, metric in metrics.items()
            if isinstance(metric, (int, float)) and not isinstance(metric, bool)
        }
    return safe


def _safe_dashboard_revenue(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "persona_ref",
            "platform_account_ref",
            "content_ref",
            "publication_ref",
            "platform",
            "currency",
            "gross_amount",
            "net_amount",
            "event_at",
        )
        if key in value
    }


def _safe_dashboard_experiment(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value.get(key)
        for key in (
            "id",
            "name",
            "status",
            "persona_refs",
            "account_refs",
            "created_at",
        )
        if key in value
    }
    results = value.get("results")
    if isinstance(results, Sequence) and not isinstance(
        results, (str, bytes, bytearray)
    ):
        safe["results"] = [
            {
                key: item.get(key)
                for key in ("id", "status", "sample_size", "winner_variant_ref", "created_at")
                if key in item
            }
            for item in results[:100]
            if isinstance(item, Mapping)
        ]
    return safe


def _safe_dashboard_learning(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("id", "title", "proposal_type", "status", "created_at")
        if key in value
    }


def _safe_dashboard_calendar(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("id", "kind", "title", "starts_at", "status")
        if key in value
    }


def _safe_dashboard_generation_item(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "id",
            "name",
            "status",
            "persona_revision_id",
            "content_item_id",
            "content_variant_id",
            "created_at",
            "started_at",
            "finished_at",
        )
        if key in value
    }


def _safe_character_dashboard(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded, secret-free Character dashboard/context projection."""

    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    character = value.get("character")
    if isinstance(character, Mapping):
        safe_character = _safe_media_persona(character, detail=True)
        revision = safe_character.get("current_revision")
        if isinstance(revision, Mapping):
            policy_keys = (
                "adult_policy",
                "sensitive_policy",
                "ip_policy",
                "disclosure_policy",
                "monetization_policy",
                "research_policy",
                "image_production_policy",
                "video_production_policy",
                "creative_direction",
                "content_pillars",
                "allowed_subjects",
                "prohibited_subjects",
            )
            safe_character["approved_policies"] = {
                key: _safe_policy_projection(revision.get(key))
                for key in policy_keys
                if key in revision
            }
        safe["character"] = safe_character

    accounts = value.get("connected_accounts")
    if isinstance(accounts, Sequence) and not isinstance(accounts, (str, bytes, bytearray)):
        safe["connected_accounts"] = [
            _safe_character_account(item)
            for item in list(accounts)[:_CHARACTER_PAGE_LIMIT]
            if isinstance(item, Mapping)
        ]
    candidates = value.get("research_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes, bytearray)):
        safe["research_candidates"] = [
            _safe_dashboard_candidate(item)
            for item in list(candidates)[:_CHARACTER_PAGE_LIMIT]
            if isinstance(item, Mapping)
        ]

    page_projectors = {
        "content": _safe_dashboard_content,
        "variants": _safe_dashboard_variant,
        "qa": _safe_dashboard_assessment,
        "rights": _safe_dashboard_assessment,
        "publications": _safe_dashboard_publication,
        "metrics": _safe_dashboard_metric,
        "revenue": _safe_dashboard_revenue,
        "experiments": _safe_dashboard_experiment,
        "learning": _safe_dashboard_learning,
        "calendar": _safe_dashboard_calendar,
    }
    for key, projector in page_projectors.items():
        if key in value:
            safe[key] = _safe_page_projection(value.get(key), projector=projector)

    generation = value.get("generation")
    if isinstance(generation, Mapping):
        generation_safe: dict[str, Any] = {}
        for key in ("recipes", "plans", "runs"):
            items = generation.get(key)
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
                generation_safe[key] = [
                    _safe_dashboard_generation_item(item)
                    for item in list(items)[:_CHARACTER_PAGE_LIMIT]
                    if isinstance(item, Mapping)
                ]
        for key in ("recipes_page", "plans_page", "runs_page"):
            if key in generation:
                generation_safe[key] = _safe_page_projection(
                    generation.get(key), projector=_safe_dashboard_generation_item
                )
        safe["generation"] = generation_safe

    # Results/learning are summary projections and are copied through closed
    # key sets, never as arbitrary nested JSON.
    results = value.get("results")
    if isinstance(results, Mapping):
        metrics = results.get("metrics")
        safe["results"] = {
            key: results.get(key)
            for key in ("snapshot_count", "last_observed_at")
            if key in results
        }
        if isinstance(metrics, Mapping):
            safe["results"]["metrics"] = {
                str(key): metric
                for key, metric in metrics.items()
                if isinstance(metric, (int, float)) and not isinstance(metric, bool)
            }
    learning = value.get("learning")
    if isinstance(learning, Mapping):
        safe["learning"] = {
            key: learning.get(key)
            for key in ("count", "pending_review_count")
            if key in learning
        }
        if "items" in learning:
            items = learning.get("items")
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
                safe["learning"]["items"] = [
                    _safe_dashboard_learning(item)
                    for item in list(items)[:_CHARACTER_PAGE_LIMIT]
                    if isinstance(item, Mapping)
                ]
        if "page" in learning:
            safe["learning"]["page"] = _safe_page_projection(
                learning.get("page"), projector=_safe_dashboard_learning
            )

    # Preserve independent pagination for clients that need to load more than
    # the compact top-level arrays.  Every page gets the same strict projector.
    page_aliases = {
        "accounts_page": _safe_character_account,
        "research_page": _safe_dashboard_candidate,
        "content_page": _safe_dashboard_content,
        "variants_page": _safe_dashboard_variant,
        "qa_page": _safe_dashboard_assessment,
        "rights_page": _safe_dashboard_assessment,
        "recipes_page": _safe_dashboard_generation_item,
        "plans_page": _safe_dashboard_generation_item,
        "runs_page": _safe_dashboard_generation_item,
        "publications_page": _safe_dashboard_publication,
        "metrics_page": _safe_dashboard_metric,
        "revenue_page": _safe_dashboard_revenue,
        "experiments_page": _safe_dashboard_experiment,
        "learning_page": _safe_dashboard_learning,
        "calendar_page": _safe_dashboard_calendar,
    }
    for key, projector in page_aliases.items():
        if key in value:
            safe[key] = _safe_page_projection(value.get(key), projector=projector)
    pagination = value.get("pagination")
    if isinstance(pagination, Mapping):
        safe["pagination"] = {
            key: _safe_page_projection(pagination.get(key), projector=projector)
            for key, projector in page_projectors.items()
            if key in pagination
        }
    return safe


@tool
async def media_operations_list_personas(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List accessible Persona identities and current revision summaries."""

    from ..services.media_operations_service import MediaOperationsService

    result = await _invoke_media_service(
        MediaOperationsService(), "list_personas",
        project_id=_effective_project_id(project_id), limit=100, offset=0,
    )
    return [_safe_media_persona(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_characters(
    project_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List slot-free Characters with bounded pagination and search."""

    from ..services.media_operations_service import MediaOperationsService

    page_limit, page_offset = _character_page_args(limit, offset)
    result = await _invoke_media_service(
        MediaOperationsService(),
        "list_characters",
        project_id=_effective_project_id(project_id),
        limit=page_limit,
        offset=page_offset,
        search=_validate_text(search, "search", 200),
    )
    return [
        _safe_media_persona(item)
        for item in result
        if isinstance(item, Mapping)
    ] if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)) else []


@tool
async def media_operations_get_character(character_id: str) -> dict[str, Any]:
    """Read one Character and its immutable revision metadata."""

    from ..services.media_operations_service import MediaOperationsService

    result = await _invoke_media_service(
        MediaOperationsService(),
        "get_character",
        character_id=character_id,
    )
    return _safe_media_persona(result, detail=True) if isinstance(result, Mapping) else {}


async def _get_character_dashboard_projection(
    character_id: str,
    *,
    limit: int,
    accounts_offset: int,
    research_offset: int,
    content_offset: int,
    variants_offset: int,
    recipes_offset: int,
    runs_offset: int,
    qa_offset: int,
    rights_offset: int,
    publications_offset: int,
    metrics_offset: int,
    revenue_offset: int,
    experiments_offset: int,
    learning_offset: int,
    calendar_offset: int,
) -> dict[str, Any]:
    """Shared Character dashboard/context implementation."""

    from ..services.media_operations_overview_service import MediaOperationsOverviewService

    page_limit, _ = _character_page_args(limit, 0)
    offsets = {
        "accounts_offset": accounts_offset,
        "research_offset": research_offset,
        "content_offset": content_offset,
        "variants_offset": variants_offset,
        "recipes_offset": recipes_offset,
        "runs_offset": runs_offset,
        "qa_offset": qa_offset,
        "rights_offset": rights_offset,
        "publications_offset": publications_offset,
        "metrics_offset": metrics_offset,
        "revenue_offset": revenue_offset,
        "experiments_offset": experiments_offset,
        "learning_offset": learning_offset,
        "calendar_offset": calendar_offset,
    }
    normalized_offsets = {
        key: _character_page_args(page_limit, value)[1]
        for key, value in offsets.items()
    }
    result = await _invoke_media_service(
        MediaOperationsOverviewService(),
        "get_character_dashboard",
        character_id=character_id,
        limit=page_limit,
        **normalized_offsets,
    )
    return _safe_character_dashboard(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_get_character_dashboard(
    character_id: str,
    limit: int = 100,
    accounts_offset: int = 0,
    research_offset: int = 0,
    content_offset: int = 0,
    variants_offset: int = 0,
    recipes_offset: int = 0,
    runs_offset: int = 0,
    qa_offset: int = 0,
    rights_offset: int = 0,
    publications_offset: int = 0,
    metrics_offset: int = 0,
    revenue_offset: int = 0,
    experiments_offset: int = 0,
    learning_offset: int = 0,
    calendar_offset: int = 0,
) -> dict[str, Any]:
    """Read the bounded, ACL-scoped Character Dashboard projection."""

    return await _get_character_dashboard_projection(
        character_id,
        limit=limit,
        accounts_offset=accounts_offset,
        research_offset=research_offset,
        content_offset=content_offset,
        variants_offset=variants_offset,
        recipes_offset=recipes_offset,
        runs_offset=runs_offset,
        qa_offset=qa_offset,
        rights_offset=rights_offset,
        publications_offset=publications_offset,
        metrics_offset=metrics_offset,
        revenue_offset=revenue_offset,
        experiments_offset=experiments_offset,
        learning_offset=learning_offset,
        calendar_offset=calendar_offset,
    )


@tool
async def media_operations_get_character_context(
    character_id: str,
    limit: int = 100,
    accounts_offset: int = 0,
    research_offset: int = 0,
    content_offset: int = 0,
    variants_offset: int = 0,
    recipes_offset: int = 0,
    runs_offset: int = 0,
    qa_offset: int = 0,
    rights_offset: int = 0,
    publications_offset: int = 0,
    metrics_offset: int = 0,
    revenue_offset: int = 0,
    experiments_offset: int = 0,
    learning_offset: int = 0,
    calendar_offset: int = 0,
) -> dict[str, Any]:
    """Alias of Character Dashboard for agent context retrieval."""

    return await _get_character_dashboard_projection(
        character_id,
        limit=limit,
        accounts_offset=accounts_offset,
        research_offset=research_offset,
        content_offset=content_offset,
        variants_offset=variants_offset,
        recipes_offset=recipes_offset,
        runs_offset=runs_offset,
        qa_offset=qa_offset,
        rights_offset=rights_offset,
        publications_offset=publications_offset,
        metrics_offset=metrics_offset,
        revenue_offset=revenue_offset,
        experiments_offset=experiments_offset,
        learning_offset=learning_offset,
        calendar_offset=calendar_offset,
    )


@tool
async def media_operations_get_persona(persona_id: str) -> dict[str, Any]:
    """Read one accessible Persona and its immutable revision metadata."""

    from ..services.media_operations_service import MediaOperationsService

    result = await _invoke_media_service(MediaOperationsService(), "get_persona", persona_id=persona_id)
    return _safe_media_persona(result, detail=True) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_persona_resources(persona_id: str) -> list[dict[str, Any]]:
    """Read only the selected Persona's linked resource references."""

    from ..services.media_operations_service import MediaOperationsService

    result = await _invoke_media_service(MediaOperationsService(), "list_persona_resources", persona_id=persona_id, limit=100, offset=0)
    safe_rows: list[dict[str, Any]] = []
    for item in result if isinstance(result, Sequence) else ():
        if not isinstance(item, Mapping):
            continue
        row = {
            key: item.get(key)
            for key in ("id", "persona_id", "project_id", "resource_kind", "platform", "label", "provenance_type", "artifact_sha256", "artifact_mime_type", "resource_hash", "created_at")
            if key in item
        }
        if "source_url" in item:
            row["source_url"] = sanitize_source_url(item.get("source_url"))
        safe_rows.append(row)
    return safe_rows


@tool
async def media_operations_list_platform_accounts(project_id: Optional[str] = None, persona_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List accessible platform account bindings without credentials."""

    from ..services.media_operations_setup_service import MediaOperationsSetupService

    result = await _invoke_media_service(
        MediaOperationsSetupService(), "list_platform_accounts",
        project_id=_effective_project_id(project_id), persona_id=persona_id, limit=100, offset=0,
    )
    return [_safe_media_account(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_platform_account(account_id: str) -> dict[str, Any]:
    """Read one platform account and its immutable capability revisions."""

    from ..services.media_operations_setup_service import MediaOperationsSetupService

    result = await _invoke_media_service(MediaOperationsSetupService(), "get_platform_account", account_id=account_id)
    return _safe_media_account(result, detail=True) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_research_routines(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List recurring research policies in the authenticated scope."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_research_routines", project_id=_effective_project_id(project_id), limit=100, offset=0)
    return [_safe_media_routine(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_due_research(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List durable research routines currently due for inspection."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_due_research_routines", project_id=_effective_project_id(project_id), limit=100)
    return [_safe_media_routine(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_create_research_routine(
    name: str,
    objective: str,
    questions: list[str],
    idempotency_key: str,
    persona_id: Optional[str] = None,
    platform_account_id: Optional[str] = None,
    target_platforms: Optional[list[str]] = None,
    cadence: str = "manual",
    timezone: str = "UTC",
    source_types: Optional[list[str]] = None,
    search_queries: Optional[list[str]] = None,
    domains: Optional[list[str]] = None,
    follow_accounts: Optional[list[str]] = None,
    follow_tags: Optional[list[str]] = None,
    exclusions: Optional[list[str]] = None,
    freshness_hours: int = 168,
    max_candidates: int = 20,
    review_policy: str = "human_review",
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a draft Persona-scoped research routine without scheduling it."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    if not isinstance(questions, list) or not questions or len(questions) > 20:
        raise ValueError("questions must contain 1 to 20 strings")
    normalized_questions = [
        _validate_text(item, "question", 500, required=True)
        for item in questions
    ]
    result = await _invoke_media_service(
        MediaOperationsResearchService(),
        "create_research_routine",
        name=_validate_text(name, "name", 255, required=True),
        objective=_validate_text(objective, "objective", 4000, required=True),
        questions=normalized_questions,
        target_platforms=target_platforms,
        cadence=_validate_text(cadence, "cadence", 32, required=True),
        timezone=_validate_text(timezone, "timezone", 64, required=True),
        source_types=source_types,
        search_queries=search_queries,
        domains=domains,
        follow_accounts=follow_accounts,
        follow_tags=follow_tags,
        exclusions=exclusions,
        freshness_hours=freshness_hours,
        max_candidates=max_candidates,
        review_policy=_validate_text(review_policy, "review_policy", 64, required=True),
        state="draft",
        enabled=False,
        persona_id=persona_id,
        platform_account_id=platform_account_id,
        project_id=_effective_project_id(project_id),
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    safe = _safe_media_routine(result, detail=True) if isinstance(result, Mapping) else {}
    return {
        **safe,
        "draft": True,
        "human_review_required": True,
        "summary": "ResearchRoutine draft created; it is disabled until a human activates the schedule.",
    }


@tool
async def media_operations_get_research_routine(routine_id: str) -> dict[str, Any]:
    """Read one recurring research routine and its exact revision."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "get_research_routine", routine_id=routine_id)
    return _safe_media_routine(result, detail=True) if isinstance(result, Mapping) else {}


@tool
async def media_operations_start_research_run(routine_id: str, routine_version: int, idempotency_key: str, focus_note: Optional[str] = None) -> dict[str, Any]:
    """Start an auditable, source-only research run; it never publishes."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "start_research_run", research_routine_id=routine_id, routine_version=routine_version, focus_note=_validate_text(focus_note, "focus_note", 4000), idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True))
    if not isinstance(result, Mapping):
        return {}
    return {key: result.get(key) for key in ("id", "research_routine_id", "research_routine_revision_id", "status", "started_at", "finished_at", "run_hash", "created_at") if key in result}


@tool
async def media_operations_list_research_runs(project_id: Optional[str] = None, routine_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List bounded research-run status and provenance metadata."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_research_runs", project_id=_effective_project_id(project_id), research_routine_id=routine_id, limit=100, offset=0)
    return [{key: item.get(key) for key in ("id", "research_routine_id", "research_routine_revision_id", "status", "started_at", "finished_at", "source_refs", "omissions", "run_hash", "created_at") if key in item} for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_research_candidates(project_id: Optional[str] = None, routine_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
    """List untrusted research candidates for human triage."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_research_candidates", project_id=_effective_project_id(project_id), research_routine_id=routine_id, status=status, limit=100, offset=0)
    return [_safe_media_candidate(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_character_candidates(
    character_id: str,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List a Character's bounded Research candidates and decision metadata."""

    page_limit, page_offset = _character_page_args(limit, offset)
    # The OverviewService follows the authorized Character FK graph before it
    # admits candidates.  Do not approximate this with a project-wide query or
    # an opaque candidate ID comparison in the tool layer.
    dashboard = await _get_character_dashboard_projection(
        character_id,
        limit=page_limit,
        accounts_offset=0,
        research_offset=page_offset,
        content_offset=0,
        variants_offset=0,
        recipes_offset=0,
        runs_offset=0,
        qa_offset=0,
        rights_offset=0,
        publications_offset=0,
        metrics_offset=0,
        revenue_offset=0,
        experiments_offset=0,
        learning_offset=0,
        calendar_offset=0,
    )
    page = dashboard.get("research_page") if isinstance(dashboard, Mapping) else None
    if not isinstance(page, Mapping):
        return {
            "character_id": character_id,
            "items": [],
            "count": 0,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": False,
        }
    items = [
        dict(item)
        for item in page.get("items", [])
        if isinstance(item, Mapping)
    ]
    status_value = _validate_text(status, "status", 32)
    if status_value:
        normalized_status = status_value.strip().lower()
        if normalized_status == "pending":
            normalized_statuses = {"discovered", "triaged", "pending"}
        else:
            normalized_statuses = {normalized_status}
        items = [
            item
            for item in items
            if str(item.get("status") or "").strip().lower() in normalized_statuses
            or str(item.get("review_state") or "").strip().lower() in normalized_statuses
        ]
    return {
        "character_id": character_id,
        "items": items[:_CHARACTER_PAGE_LIMIT],
        "count": len(items),
        "limit": page_limit,
        "offset": page_offset,
        "has_more": bool(page.get("has_more")) and not status_value,
    }


@tool
async def media_operations_get_research_candidate(candidate_id: str) -> dict[str, Any]:
    """Read one untrusted candidate without granting it tool authority."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "get_research_candidate", candidate_id=candidate_id)
    return _safe_media_candidate(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_research_candidate_decisions(
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Read immutable, safe decision history for one research candidate."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(
        MediaOperationsResearchService(),
        "list_research_candidate_decisions",
        candidate_id=candidate_id,
        limit=100,
        offset=0,
    )
    # The HTTP boundary returns a page envelope.  Keep the model-facing tool
    # compact and backward-compatible by exposing only its safe items.
    items = (
        result.get("items", [])
        if isinstance(result, Mapping)
        else result
    )
    return [
        _safe_media_candidate_decision(item)
        for item in items
        if isinstance(item, Mapping)
    ] if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)) else []


async def _assert_candidate_character(
    candidate_id: str,
    character_id: str,
) -> dict[str, Any]:
    """Verify candidate → routine → Character ownership before a proposal."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    candidate = await _invoke_media_service(
        MediaOperationsResearchService(),
        "get_research_candidate",
        candidate_id=candidate_id,
    )
    if not isinstance(candidate, Mapping):
        raise PermissionError("candidate is not visible in the authenticated scope")
    routine_id = candidate.get("research_routine_id")
    if not routine_id:
        raise PermissionError("candidate Character graph is unavailable")
    routine = await _invoke_media_service(
        MediaOperationsResearchService(),
        "get_research_routine",
        routine_id=routine_id,
    )
    if not isinstance(routine, Mapping) or str(routine.get("persona_id") or "") != str(character_id):
        raise PermissionError("candidate does not belong to the requested Character")
    return dict(candidate)


@tool
async def media_operations_list_character_candidate_decisions(
    candidate_id: str,
    character_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read a candidate's append-only decision/reason history safely."""

    page_limit, page_offset = _character_page_args(limit, offset)
    if character_id:
        await _assert_candidate_character(candidate_id, character_id)
    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(
        MediaOperationsResearchService(),
        "list_research_candidate_decisions",
        candidate_id=candidate_id,
        limit=page_limit,
        offset=page_offset,
    )
    if not isinstance(result, Mapping):
        return {
            "candidate_id": candidate_id,
            "items": [],
            "total": 0,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": False,
        }
    items = result.get("items", [])
    safe_items = [
        _safe_media_candidate_decision(item)
        for item in items
        if isinstance(item, Mapping)
    ] if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)) else []
    return {
        "candidate_id": result.get("candidate_id", candidate_id),
        "current_status": result.get("current_status"),
        "current_decision_version": result.get("current_decision_version"),
        "candidate_hash": result.get("candidate_hash"),
        "items": safe_items,
        "total": result.get("total", len(safe_items)),
        "limit": page_limit,
        "offset": page_offset,
        "has_more": bool(result.get("has_more", page_offset + page_limit < int(result.get("total", len(safe_items))))),
    }


@tool
async def media_operations_triage_research_candidate(
    candidate_id: str,
    idempotency_key: str,
    expected_status: str,
    expected_decision_version: int,
    expected_candidate_hash: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Mark an untrusted candidate as triaged for later human acceptance."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    # Agents may organize a discovery queue, but accepting or rejecting a
    # source-backed candidate remains an explicit human decision in the
    # service.  Keep this tool's status fixed rather than exposing that gate
    # as a model-controlled argument.
    result = await _invoke_media_service(
        MediaOperationsResearchService(),
        "triage_research_candidate",
        candidate_id=candidate_id,
        status="triaged",
        reason=_validate_text(reason, "reason", 4000),
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
        expected_status=_validate_text(expected_status, "expected_status", 16, required=True),
        expected_decision_version=expected_decision_version,
        expected_candidate_hash=_validate_text(expected_candidate_hash, "expected_candidate_hash", 64, required=True),
    )
    return _safe_media_candidate(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_propose_candidate_triage(
    candidate_id: str,
    idempotency_key: str,
    expected_status: str,
    expected_decision_version: int,
    expected_candidate_hash: str,
    reason: Optional[str] = None,
    character_id: Optional[str] = None,
) -> dict[str, Any]:
    """Propose queue triage without exposing final accept/reject authority."""

    if character_id:
        await _assert_candidate_character(candidate_id, character_id)
    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(
        MediaOperationsResearchService(),
        "triage_research_candidate",
        candidate_id=candidate_id,
        status="triaged",
        reason=_validate_text(reason, "reason", 4000),
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
        expected_status=_validate_text(expected_status, "expected_status", 16, required=True),
        expected_decision_version=expected_decision_version,
        expected_candidate_hash=_validate_text(expected_candidate_hash, "expected_candidate_hash", 64, required=True),
    )
    safe = _safe_media_candidate(result) if isinstance(result, Mapping) else {}
    return {
        **safe,
        "proposal": True,
        "human_review_required": True,
        "execution_allowed": False,
        "summary": "Candidate triage proposal recorded; final acceptance or rejection remains human-only.",
    }


@tool
async def media_operations_list_editorial_programs(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List Persona-scoped editorial programs and current revision metadata."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_editorial_programs", project_id=_effective_project_id(project_id), limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_due_editorial_programs(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List active editorial programs whose durable due marker elapsed."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_due_editorial_programs", project_id=_effective_project_id(project_id), limit=100)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_content_items(project_id: Optional[str] = None, editorial_program_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List platform-independent ContentItems in the selected scope."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "list_content_items", project_id=_effective_project_id(project_id), editorial_program_id=editorial_program_id, limit=100, offset=0)
    return [_safe_media_content(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_content_item(content_item_id: str) -> dict[str, Any]:
    """Read one ContentItem and bounded source-candidate provenance."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(MediaOperationsResearchService(), "get_content_item", content_item_id=content_item_id)
    return _safe_media_content(result, detail=True) if isinstance(result, Mapping) else {}


@tool
async def media_operations_propose_content_promotion(
    candidate_id: str,
    idempotency_key: str,
    character_id: Optional[str] = None,
    accepted_decision_id: Optional[str] = None,
    title: Optional[str] = None,
    brief: Optional[str] = None,
) -> dict[str, Any]:
    """Prepare an accepted Research candidate for human Content promotion.

    Promotion itself remains a human-only mutation in
    ``MediaOperationsResearchService.promote_research_candidate``.  This tool
    only validates the Character graph and latest acceptance ledger, then
    returns a bounded proposal envelope; it never creates a ContentItem.
    """

    from ..services.media_operations_research_service import MediaOperationsResearchService

    candidate = (
        await _assert_candidate_character(candidate_id, character_id)
        if character_id
        else await _invoke_media_service(
            MediaOperationsResearchService(),
            "get_research_candidate",
            candidate_id=candidate_id,
        )
    )
    if not isinstance(candidate, Mapping):
        raise PermissionError("candidate is not visible in the authenticated scope")
    decisions = await _invoke_media_service(
        MediaOperationsResearchService(),
        "list_research_candidate_decisions",
        candidate_id=candidate_id,
        limit=100,
        offset=0,
    )
    rows = decisions.get("items", []) if isinstance(decisions, Mapping) else []
    safe_rows = [
        _safe_media_candidate_decision(item)
        for item in rows
        if isinstance(item, Mapping)
    ] if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else []
    latest_accept = next(
        (
            row
            for row in reversed(safe_rows)
            if str(row.get("event_type") or "").strip().lower() == "accept"
            and str(row.get("to_status") or "").strip().lower() == "accepted"
        ),
        None,
    )
    if str(candidate.get("status") or "").strip().lower() != "accepted" or latest_accept is None:
        raise ValueError("only a candidate with a latest human acceptance can be promoted")
    accepted_id = str(accepted_decision_id or latest_accept.get("id") or "").strip()
    if not accepted_id or accepted_id != str(latest_accept.get("id") or ""):
        raise ValueError("accepted_decision_id must reference the latest acceptance decision")
    title_value = _validate_text(title, "title", 500) or str(candidate.get("title") or "").strip()
    brief_value = _validate_text(brief, "brief", 8000) or str(candidate.get("summary") or "").strip()
    if not title_value or not brief_value:
        raise ValueError("promotion title and brief are required")
    key_value = _validate_text(idempotency_key, "idempotency_key", 255, required=True)
    return {
        "proposal": True,
        "human_review_required": True,
        "execution_allowed": False,
        "operation": "media_promote_research_candidate",
        "candidate_id": str(candidate.get("id") or candidate_id),
        "candidate_hash": candidate.get("candidate_hash"),
        "accepted_decision_id": accepted_id,
        "accepted_decision_version": latest_accept.get("sequence"),
        "title": title_value,
        "brief": brief_value,
        "idempotency_key": key_value,
        "summary": "ContentItem promotion proposal prepared; a human must execute promotion after revalidating the acceptance decision.",
    }


@tool
async def media_operations_propose_persona(
    intake_slot: int,
    display_name: str,
    idempotency_key: str,
    summary: Optional[str] = None,
    voice: Optional[str] = None,
    audience: Optional[str] = None,
    platforms: Optional[list[str]] = None,
    content_pillars: Optional[list[str]] = None,
    niche: Optional[str] = None,
    positioning: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a draft Persona proposal; activation remains a human decision."""

    from ..services.media_operations_service import MediaOperationsService

    result = await _invoke_media_service(
        MediaOperationsService(), "create_persona",
        intake_slot=intake_slot,
        display_name=_validate_text(display_name, "display_name", 4000, required=True),
        summary=_validate_text(summary, "summary", 4000),
        voice=_validate_text(voice, "voice", 4000),
        audience=_validate_text(audience, "audience", 4000),
        platforms=platforms,
        content_pillars=content_pillars,
        niche=_validate_text(niche, "niche", 4000),
        positioning=_validate_text(positioning, "positioning", 4000),
        state="draft",
        project_id=_effective_project_id(project_id),
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return {
        **(_safe_media_persona(result, detail=True) if isinstance(result, Mapping) else {}),
        "proposal": True,
        "human_review_required": True,
        "summary": "Draft Persona proposal recorded; no Persona was activated automatically.",
    }


@tool
async def media_operations_create_editorial_program(
    persona_id: str,
    name: str,
    objective: str,
    idempotency_key: str,
    content_type: str = "article",
    cadence: str = "manual",
    target_platforms: Optional[list[str]] = None,
    content_pillar: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a draft editorial program bound to one Persona."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(
        MediaOperationsResearchService(), "create_editorial_program",
        persona_id=persona_id,
        name=_validate_text(name, "name", 255, required=True),
        objective=_validate_text(objective, "objective", 8000, required=True),
        content_type=_validate_text(content_type, "content_type", 64, required=True),
        cadence=_validate_text(cadence, "cadence", 32, required=True),
        target_platforms=target_platforms,
        content_pillar=_validate_text(content_pillar, "content_pillar", 200),
        state="draft",
        enabled=False,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_create_content_item(
    editorial_program_id: str,
    title: str,
    brief: str,
    idempotency_key: str,
    candidate_ids: Optional[list[str]] = None,
    finding_ids: Optional[list[str]] = None,
    content_type: str = "article",
    content_pillar: Optional[str] = None,
    scheduled_at: Optional[str] = None,
) -> dict[str, Any]:
    """Create a draft ContentItem from bounded research evidence."""

    from ..services.media_operations_research_service import MediaOperationsResearchService

    result = await _invoke_media_service(
        MediaOperationsResearchService(), "create_content_item",
        editorial_program_id=editorial_program_id,
        title=_validate_text(title, "title", 500, required=True),
        brief=_validate_text(brief, "brief", 8000, required=True),
        candidate_ids=candidate_ids,
        finding_ids=finding_ids,
        content_type=_validate_text(content_type, "content_type", 64, required=True),
        content_pillar=_validate_text(content_pillar, "content_pillar", 200),
        scheduled_at=_validate_text(scheduled_at, "scheduled_at", 128),
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return _safe_media_content(result, detail=True) if isinstance(result, Mapping) else {}


@tool
async def media_operations_create_content_variant(
    content_item_id: str,
    platform: str,
    persona_revision_id: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    platform_account_id: Optional[str] = None,
    platform_account_revision_id: Optional[str] = None,
    generation_output_refs: Optional[list[Any]] = None,
    source_evidence: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Create one independently versioned, typed platform variant."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    result = await _invoke_media_service(
        MediaOperationsContentService(), "create_variant",
        content_item_id=content_item_id,
        platform=platform,
        persona_revision_id=persona_revision_id,
        platform_account_id=platform_account_id,
        platform_account_revision_id=platform_account_revision_id,
        payload=dict(payload) if isinstance(payload, Mapping) else payload,
        generation_output_refs=generation_output_refs,
        source_evidence=source_evidence,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_propose_qa(
    content_variant_revision_id: str,
    result: str,
    idempotency_key: str,
    checks: Optional[list[Any]] = None,
    findings: Optional[list[Any]] = None,
    evidence: Optional[list[Any]] = None,
    policy_revision_id: Optional[str] = None,
    policy_revision_hash: Optional[str] = None,
) -> dict[str, Any]:
    """Record a QA proposal; only an explicitly human actor can mark PASS."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    outcome = await _invoke_media_service(
        MediaOperationsContentService(), "record_qa",
        content_variant_revision_id=content_variant_revision_id,
        result=result,
        checks=checks,
        findings=findings,
        evidence=evidence,
        policy_revision_id=policy_revision_id,
        policy_revision_hash=policy_revision_hash,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return {
        **(dict(outcome) if isinstance(outcome, Mapping) else {}),
        "proposal": True,
        "human_review_required": str(result).casefold() == "passed",
    }


@tool
async def media_operations_propose_rights(
    content_variant_revision_id: str,
    result: str,
    idempotency_key: str,
    checks: Optional[list[Any]] = None,
    findings: Optional[list[Any]] = None,
    evidence: Optional[list[Any]] = None,
    policy_revision_id: Optional[str] = None,
    policy_revision_hash: Optional[str] = None,
) -> dict[str, Any]:
    """Record a Rights proposal; clearing rights is human-only."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    outcome = await _invoke_media_service(
        MediaOperationsContentService(), "record_rights",
        content_variant_revision_id=content_variant_revision_id,
        result=result,
        checks=checks,
        findings=findings,
        evidence=evidence,
        policy_revision_id=policy_revision_id,
        policy_revision_hash=policy_revision_hash,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return {
        **(dict(outcome) if isinstance(outcome, Mapping) else {}),
        "proposal": True,
        "human_review_required": str(result).casefold() == "cleared",
    }


@tool
async def media_operations_list_creative_recipes(project_id: Optional[str] = None, persona_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List Persona-bound CreativeRecipe revisions through the Studio boundary."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "list_creative_recipes", project_id=_effective_project_id(project_id), persona_id=persona_id, limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_creative_recipe(recipe_id: str) -> dict[str, Any]:
    """Read one opaque Generation Studio CreativeRecipe reference."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "get_creative_recipe", recipe_id=recipe_id)
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_generation_workspaces(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List configured Generation Studio workspace bindings without paths or secrets."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "list_generation_workspaces", project_id=_effective_project_id(project_id), limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_generation_plans(project_id: Optional[str] = None, workspace_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List GenerationPlans bound to exact Persona/content/recipe revisions."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "list_generation_plans", project_id=_effective_project_id(project_id), workspace_id=workspace_id, limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_generation_plan(plan_id: str) -> dict[str, Any]:
    """Read a GenerationPlan and safe run/output status."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "get_generation_plan", plan_id=plan_id)
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_generation_runs(project_id: Optional[str] = None, plan_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List Generation Studio run observations and immutable outputs."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "list_generation_runs", project_id=_effective_project_id(project_id), plan_id=plan_id, limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_generation_run(run_id: str) -> dict[str, Any]:
    """Read one Generation Studio run status and output provenance."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(MediaOperationsGenerationService(), "get_generation_run", run_id=run_id)
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_create_generation_plan(
    persona_revision_id: str,
    creative_recipe_revision_id: str,
    workspace_id: str,
    request_spec: dict[str, Any],
    idempotency_key: str,
    requested_outputs: int = 1,
    content_item_id: Optional[str] = None,
    content_variant_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a cost-scoped GenerationPlan; external execution stays separate."""

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(
        MediaOperationsGenerationService(), "create_generation_plan",
        persona_revision_id=persona_revision_id,
        creative_recipe_revision_id=creative_recipe_revision_id,
        workspace_id=workspace_id,
        request_spec=_safe_generation_plan_request_spec(request_spec),
        requested_outputs=requested_outputs,
        content_item_id=content_item_id,
        content_variant_id=content_variant_id,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_propose_generation(
    persona_revision_id: str,
    creative_recipe_revision_id: str,
    workspace_id: str,
    request_spec: dict[str, Any],
    idempotency_key: str,
    requested_outputs: int = 1,
    content_item_id: Optional[str] = None,
    content_variant_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a semantic GenerationPlan proposal without provider execution.

    The underlying plan service records only the Character/content intent and
    opaque workspace/model selector.  Submission, paid acknowledgement,
    reconciliation and output selection are deliberately unavailable to the
    model-facing facade.
    """

    from ..services.media_operations_generation_service import MediaOperationsGenerationService

    result = await _invoke_media_service(
        MediaOperationsGenerationService(),
        "create_generation_plan",
        persona_revision_id=persona_revision_id,
        creative_recipe_revision_id=creative_recipe_revision_id,
        workspace_id=workspace_id,
        request_spec=_safe_generation_plan_request_spec(request_spec),
        requested_outputs=requested_outputs,
        content_item_id=content_item_id,
        content_variant_id=content_variant_id,
        idempotency_key=_validate_text(idempotency_key, "idempotency_key", 255, required=True),
    )
    safe = dict(result) if isinstance(result, Mapping) else {}
    # Never pass through provider/workflow fields if a legacy adapter returns
    # them alongside the plan summary.
    allowed = {
        "id",
        "owner_user_id",
        "project_id",
        "persona_revision_id",
        "creative_recipe_revision_id",
        "workspace_id",
        "content_item_id",
        "content_variant_id",
        "request_spec",
        "request_hash",
        "generation_kind",
        "requested_outputs",
        "status",
        "created_at",
    }
    safe = {key: safe.get(key) for key in allowed if key in safe}
    return {
        **safe,
        "proposal": True,
        "human_review_required": True,
        "execution_allowed": False,
        "summary": "Generation plan proposal recorded; paid/provider execution requires explicit human approval.",
    }


@tool
async def media_operations_get_calendar(project_id: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None) -> dict[str, Any]:
    """Read the bounded Calendar projection of due work, reviews and publications."""

    from ..services.media_operations_overview_service import MediaOperationsOverviewService

    result = await _invoke_media_service(MediaOperationsOverviewService(), "get_calendar", project_id=_effective_project_id(project_id), start=start, end=end, limit=100, offset=0)
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_get_results(project_id: Optional[str] = None) -> dict[str, Any]:
    """Read bounded Persona/platform Results summaries without raw metric dumps."""

    from ..services.media_operations_overview_service import MediaOperationsOverviewService

    result = await _invoke_media_service(
        MediaOperationsOverviewService(),
        "get_results",
        project_id=_effective_project_id(project_id),
    )
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_get_experiment(experiment_id: str) -> dict[str, Any]:
    """Read one Experiment and its evidence-backed result status."""

    from ..services.media_operations_metrics_service import MediaOperationsMetricsService

    result = await _invoke_media_service(MediaOperationsMetricsService(), "get_experiment", experiment_id=experiment_id)
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_list_revenue_events(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List revenue event summaries and correction links."""

    from ..services.media_operations_metrics_service import MediaOperationsMetricsService

    result = await _invoke_media_service(MediaOperationsMetricsService(), "list_revenue_events", project_id=_effective_project_id(project_id), limit=100, offset=0)
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_content_variants(
    project_id: Optional[str] = None,
    content_item_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List ACL-visible ContentVariants and their readiness metadata."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    result = await _invoke_media_service(
        MediaOperationsContentService(),
        "list_variants",
        project_id=_effective_project_id(project_id),
        content_item_id=content_item_id,
        platform=platform,
        limit=100,
        offset=0,
    )
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_get_content_variant(variant_id: str) -> dict[str, Any]:
    """Read one ACL-visible ContentVariant, revisions and readiness projection."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    result = await _invoke_media_service(
        MediaOperationsContentService(),
        "get_variant",
        variant_id=variant_id,
    )
    return dict(result) if isinstance(result, Mapping) else {}


@tool
async def media_operations_get_variant_readiness(variant_id: str) -> dict[str, Any]:
    """Read fail-closed QA/Rights readiness for the current ContentVariant revision."""

    from ..services.media_operations_content_service import MediaOperationsContentService

    result = await _invoke_media_service(
        MediaOperationsContentService(),
        "get_readiness",
        variant_id=variant_id,
    )
    return dict(result) if isinstance(result, Mapping) else {"ready": False, "blocking_reasons": ["unavailable"]}


@tool
async def media_operations_list_actions(
    project_id: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List ACL-visible MediaOps proposals without payload or receipt bodies."""

    result = await _invoke_media_service(
        OperationsService(),
        "list_media_actions",
        project_id=_effective_project_id(project_id),
        platform=platform,
        status=status,
        limit=100,
        offset=0,
    )
    return [_safe_media_action(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_propose_action(
    action_type: str,
    platform: str,
    content_item_id: str,
    content_variant_id: str,
    content_variant_revision_id: str,
    persona_revision_id: str,
    connection_id: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
    platform_account_id: Optional[str] = None,
    platform_account_revision_id: Optional[str] = None,
    project_id: Optional[str] = None,
    artifact_hashes: Optional[list[str]] = None,
    content_item_hash: Optional[str] = None,
    content_variant_hash: Optional[str] = None,
    content_variant_revision_hash: Optional[str] = None,
    persona_revision_hash: Optional[str] = None,
    platform_account_revision_hash: Optional[str] = None,
    content_variant_revision_version: Optional[int] = None,
    payload_hash: Optional[str] = None,
    qa: Optional[Mapping[str, Any]] = None,
    rights: Optional[Mapping[str, Any]] = None,
    schedule: Optional[Mapping[str, Any]] = None,
    adapter_target: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Propose a manual MediaOps action; approval/execution stays human-only."""

    key_value = _validate_text(idempotency_key, "idempotency_key", 255, required=True)
    result = await _invoke_media_service(
        OperationsService(),
        "create_media_action",
        action_type=action_type,
        platform=platform,
        content_item_id=content_item_id,
        content_variant_id=content_variant_id,
        content_variant_revision_id=content_variant_revision_id,
        persona_revision_id=persona_revision_id,
        connection_id=connection_id,
        idempotency_key=key_value,
        payload=dict(payload) if isinstance(payload, Mapping) else payload,
        platform_account_id=platform_account_id,
        platform_account_revision_id=platform_account_revision_id,
        project_id=_effective_project_id(project_id),
        artifact_hashes=artifact_hashes,
        content_item_hash=content_item_hash,
        content_variant_hash=content_variant_hash,
        content_variant_revision_hash=content_variant_revision_hash,
        persona_revision_hash=persona_revision_hash,
        platform_account_revision_hash=platform_account_revision_hash,
        content_variant_revision_version=content_variant_revision_version,
        payload_hash=payload_hash,
        qa=qa,
        rights=rights,
        schedule=schedule,
        adapter_target=adapter_target,
    )
    safe = _safe_media_action(result if isinstance(result, Mapping) else {})
    return {
        **safe,
        "action_id": safe.get("id"),
        "summary": "MediaOps proposal recorded; human approval and manual execution are required.",
    }


@tool
async def media_operations_propose_publication(
    content_variant_id: str,
    idempotency_key: str,
    content_variant_revision_id: Optional[str] = None,
    platform_account_id: Optional[str] = None,
    platform: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    caption_summary: Optional[str] = None,
) -> dict[str, Any]:
    """Prepare a publication proposal without approval or provider execution.

    The Trusted Operations Kernel remains the only path that can create or
    approve an ExternalAction.  This facade reads the exact variant/readiness
    projection and returns opaque references for a human review queue; no
    payload, provider target, credential or receipt body is accepted here.
    """

    from ..services.media_operations_content_service import MediaOperationsContentService

    variant = await _invoke_media_service(
        MediaOperationsContentService(),
        "get_variant",
        variant_id=content_variant_id,
    )
    if not isinstance(variant, Mapping):
        raise PermissionError("content variant is not visible in the authenticated scope")
    current = variant.get("current_revision")
    if not isinstance(current, Mapping):
        raise ValueError("content variant has no immutable current revision")
    current_revision_id = str(current.get("id") or "").strip()
    requested_revision_id = str(content_variant_revision_id or current_revision_id).strip()
    if not requested_revision_id or requested_revision_id != current_revision_id:
        raise ValueError("content_variant_revision_id must reference the current immutable revision")
    readiness = variant.get("readiness")
    if not isinstance(readiness, Mapping):
        readiness = await _invoke_media_service(
            MediaOperationsContentService(),
            "get_readiness",
            variant_id=content_variant_id,
        )
    safe_readiness = {
        key: readiness.get(key)
        for key in (
            "content_variant_id",
            "variant_id",
            "revision_id",
            "revision_hash",
            "ready",
            "publication_allowed",
            "status",
            "qa_result",
            "rights_result",
            "blockers",
            "blocking_reasons",
        )
        if key in readiness
    } if isinstance(readiness, Mapping) else {"ready": False, "status": "blocked"}
    schedule_value = _validate_text(scheduled_at, "scheduled_at", 128)
    caption_value = _validate_text(caption_summary, "caption_summary", 2000)
    platform_value = _validate_text(platform, "platform", 32)
    account_value = _validate_text(platform_account_id, "platform_account_id", 255)
    key_value = _validate_text(idempotency_key, "idempotency_key", 255, required=True)
    return {
        "proposal": True,
        "human_review_required": True,
        "execution_allowed": False,
        "operation": "media_publication",
        "content_variant_id": str(variant.get("id") or content_variant_id),
        "content_variant_revision_id": requested_revision_id,
        "platform": platform_value or variant.get("platform"),
        "platform_account_id": account_value,
        "scheduled_at": schedule_value,
        "caption_summary": caption_value,
        "readiness": safe_readiness,
        "status": "proposed" if safe_readiness.get("ready") else "blocked",
        "idempotency_key": key_value,
        "summary": "Publication proposal prepared; human approval and Trusted Operations execution are required.",
    }


@tool
async def media_operations_list_metric_snapshots(
    project_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List ACL-visible metric snapshot metadata without raw metric bodies."""

    from ..services.media_operations_metrics_service import MediaOperationsMetricsService

    result = await _invoke_media_service(
        MediaOperationsMetricsService(),
        "list_metric_snapshots",
        project_id=_effective_project_id(project_id),
        limit=100,
        offset=0,
    )
    return [
        {
            key: item.get(key)
            for key in ("id", "project_id", "observed_at", "source", "provider", "completeness", "ingestion_status", "snapshot_hash")
            if key in item
        }
        for item in result
        if isinstance(item, Mapping)
    ] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_experiments(
    project_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List ACL-visible experiment definitions and statuses."""

    from ..services.media_operations_metrics_service import MediaOperationsMetricsService

    result = await _invoke_media_service(
        MediaOperationsMetricsService(),
        "list_experiments",
        project_id=_effective_project_id(project_id),
        limit=100,
        offset=0,
    )
    return [
        {
            key: item.get(key)
            for key in ("id", "project_id", "name", "primary_metric", "window_start", "window_end", "minimum_sample_size", "status", "create_hash")
            if key in item
        }
        for item in result
        if isinstance(item, Mapping)
    ] if isinstance(result, Sequence) else []


@tool
async def media_operations_list_learning_proposals(
    project_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List evidence-backed learning proposals awaiting human review."""

    from ..services.media_operations_learning_service import MediaOperationsLearningService

    result = await _invoke_media_service(
        MediaOperationsLearningService(),
        "list_learning_proposals",
        project_id=_effective_project_id(project_id),
        limit=100,
        offset=0,
    )
    return [dict(item) for item in result if isinstance(item, Mapping)] if isinstance(result, Sequence) else []


@tool
async def media_operations_propose_learning(
    subject_type: str,
    subject_ref: str,
    title: str,
    summary: str,
    recommendation: str,
    evidence_refs: list[Mapping[str, Any]],
    window_start: str,
    window_end: str,
    confidence: float,
    uncertainty: float,
    idempotency_key: str,
    proposal_type: str = "learning",
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Propose evidence-backed learning for human review; never auto-applies it."""

    from ..services.media_operations_learning_service import MediaOperationsLearningService

    key_value = _validate_text(idempotency_key, "idempotency_key", 255, required=True)
    result = await _invoke_media_service(
        MediaOperationsLearningService(),
        "create_learning_proposal",
        subject_type=subject_type,
        subject_ref=subject_ref,
        title=title,
        summary=summary,
        recommendation=recommendation,
        evidence_refs=[dict(item) for item in evidence_refs],
        window_start=window_start,
        window_end=window_end,
        confidence=confidence,
        uncertainty=uncertainty,
        proposal_type=proposal_type,
        project_id=_effective_project_id(project_id),
        idempotency_key=key_value,
    )
    safe = dict(result) if isinstance(result, Mapping) else {}
    safe.pop("evidence_refs", None)
    return {
        **safe,
        "proposal_id": safe.get("id"),
        "status": "pending_review",
        "human_review_required": True,
        "summary": "Learning proposal recorded for human review; no Memory or Skill was changed.",
    }


# Compatibility aliases for integrations that use the service/API vocabulary.
# They intentionally point to the canonical ToolDefinitions and are not
# separately registered, avoiding duplicate model-facing entry points.
operations_ingest_opportunity = operations_create_opportunity
operations_create_evaluation = operations_record_evaluation
operations_create_draft = operations_create_application_draft
operations_read_opportunity = operations_get_opportunity
operations_read_action = operations_get_action

# MediaOps aliases are kept for callers using the shorter vocabulary, while
# only the canonical definitions below are registered with the model runtime.
media_list_metric_snapshots = media_operations_list_metric_snapshots
media_list_learning_proposals = media_operations_list_learning_proposals
media_propose_learning = media_operations_propose_learning

OPERATIONS_READ_TOOL_NAMES = frozenset(
    {
        "operations_list_connections",
        "operations_get_opportunity",
        "operations_get_action",
    }
)
OPERATIONS_MUTATION_TOOL_NAMES = frozenset(
    {
        "operations_create_opportunity",
        "operations_record_evaluation",
        "operations_create_application_draft",
        "operations_propose_action",
    }
)
MEDIA_OPERATIONS_READ_TOOL_NAMES = frozenset(
    {
        "media_operations_get_adapter_status",
        "media_operations_list_personas",
        "media_operations_get_persona",
        "media_operations_list_characters",
        "media_operations_get_character",
        "media_operations_get_character_dashboard",
        "media_operations_get_character_context",
        "media_operations_list_persona_resources",
        "media_operations_list_platform_accounts",
        "media_operations_get_platform_account",
        "media_operations_list_research_routines",
        "media_operations_list_due_research",
        "media_operations_get_research_routine",
        "media_operations_list_research_runs",
        "media_operations_list_research_candidates",
        "media_operations_list_character_candidates",
        "media_operations_get_research_candidate",
        "media_operations_list_research_candidate_decisions",
        "media_operations_list_character_candidate_decisions",
        "media_operations_list_editorial_programs",
        "media_operations_list_due_editorial_programs",
        "media_operations_list_content_items",
        "media_operations_get_content_item",
        "media_operations_list_content_variants",
        "media_operations_get_content_variant",
        "media_operations_get_variant_readiness",
        "media_operations_list_creative_recipes",
        "media_operations_get_creative_recipe",
        "media_operations_list_generation_workspaces",
        "media_operations_list_generation_plans",
        "media_operations_get_generation_plan",
        "media_operations_list_generation_runs",
        "media_operations_get_generation_run",
        "media_operations_list_actions",
        "media_operations_list_metric_snapshots",
        "media_operations_list_experiments",
        "media_operations_get_experiment",
        "media_operations_list_learning_proposals",
        "media_operations_get_calendar",
        "media_operations_get_results",
        "media_operations_list_revenue_events",
    }
)
MEDIA_OPERATIONS_MUTATION_TOOL_NAMES = frozenset(
    {
        "media_operations_propose_persona",
        "media_operations_create_editorial_program",
        "media_operations_start_research_run",
        "media_operations_create_research_routine",
        "media_operations_triage_research_candidate",
        "media_operations_propose_candidate_triage",
        "media_operations_create_content_item",
        "media_operations_propose_content_promotion",
        "media_operations_create_content_variant",
        "media_operations_create_generation_plan",
        "media_operations_propose_generation",
        "media_operations_propose_qa",
        "media_operations_propose_rights",
        "media_operations_propose_action",
        "media_operations_propose_publication",
        "media_operations_propose_learning",
    }
)
MEDIA_OPERATIONS_TOOL_NAMES = MEDIA_OPERATIONS_READ_TOOL_NAMES | MEDIA_OPERATIONS_MUTATION_TOOL_NAMES

ENGAGEMENT_OPERATIONS_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    operations_list_connections,
    operations_create_opportunity,
    operations_record_evaluation,
    operations_create_application_draft,
    operations_propose_action,
    operations_get_opportunity,
    operations_get_action,
)

MEDIA_OPERATIONS_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    media_operations_get_adapter_status,
    media_operations_list_personas,
    media_operations_get_persona,
    media_operations_list_characters,
    media_operations_get_character,
    media_operations_get_character_dashboard,
    media_operations_get_character_context,
    media_operations_list_persona_resources,
    media_operations_list_platform_accounts,
    media_operations_get_platform_account,
    media_operations_list_research_routines,
    media_operations_list_due_research,
    media_operations_get_research_routine,
    media_operations_create_research_routine,
    media_operations_start_research_run,
    media_operations_list_research_runs,
    media_operations_list_research_candidates,
    media_operations_list_character_candidates,
    media_operations_get_research_candidate,
    media_operations_list_research_candidate_decisions,
    media_operations_list_character_candidate_decisions,
    media_operations_triage_research_candidate,
    media_operations_propose_candidate_triage,
    media_operations_list_editorial_programs,
    media_operations_list_due_editorial_programs,
    media_operations_list_content_items,
    media_operations_get_content_item,
    media_operations_propose_content_promotion,
    media_operations_propose_persona,
    media_operations_create_editorial_program,
    media_operations_create_content_item,
    media_operations_create_content_variant,
    media_operations_propose_qa,
    media_operations_propose_rights,
    media_operations_list_creative_recipes,
    media_operations_get_creative_recipe,
    media_operations_list_generation_workspaces,
    media_operations_list_generation_plans,
    media_operations_get_generation_plan,
    media_operations_list_generation_runs,
    media_operations_get_generation_run,
    media_operations_create_generation_plan,
    media_operations_propose_generation,
    media_operations_list_content_variants,
    media_operations_get_content_variant,
    media_operations_get_variant_readiness,
    media_operations_list_actions,
    media_operations_propose_action,
    media_operations_propose_publication,
    media_operations_list_metric_snapshots,
    media_operations_list_experiments,
    media_operations_get_experiment,
    media_operations_list_learning_proposals,
    media_operations_propose_learning,
    media_operations_get_calendar,
    media_operations_get_results,
    media_operations_list_revenue_events,
)

# Keep the historical EngagementOps collection stable for callers that use it
# as a compatibility boundary.  MediaOps is exposed through its own explicit
# collection and the combined collection is what the runtime registry uses.
OPERATIONS_TOOL_DEFINITIONS = ENGAGEMENT_OPERATIONS_TOOL_DEFINITIONS
ALL_OPERATIONS_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    *ENGAGEMENT_OPERATIONS_TOOL_DEFINITIONS,
    *MEDIA_OPERATIONS_TOOL_DEFINITIONS,
)
# Short alias used by focused EngagementOps tests/integrations.
OPERATIONS_TOOLS = ENGAGEMENT_OPERATIONS_TOOL_DEFINITIONS
MEDIA_OPERATIONS_TOOLS = MEDIA_OPERATIONS_TOOL_DEFINITIONS

__all__ = [
    "OPERATIONS_MUTATION_TOOL_NAMES",
    "OPERATIONS_READ_TOOL_NAMES",
    "MEDIA_OPERATIONS_MUTATION_TOOL_NAMES",
    "MEDIA_OPERATIONS_READ_TOOL_NAMES",
    "MEDIA_OPERATIONS_TOOL_NAMES",
    "ENGAGEMENT_OPERATIONS_TOOL_DEFINITIONS",
    "MEDIA_OPERATIONS_TOOL_DEFINITIONS",
    "MEDIA_OPERATIONS_TOOLS",
    "ALL_OPERATIONS_TOOL_DEFINITIONS",
    "OPERATIONS_TOOL_DEFINITIONS",
    "OPERATIONS_TOOLS",
    "operations_create_application_draft",
    "operations_create_draft",
    "operations_create_evaluation",
    "operations_create_opportunity",
    "operations_get_action",
    "operations_get_opportunity",
    "operations_ingest_opportunity",
    "operations_list_connections",
    "operations_propose_action",
    "operations_read_action",
    "operations_read_opportunity",
    "operations_record_evaluation",
    "media_operations_get_adapter_status",
    "media_operations_list_personas",
    "media_operations_get_persona",
    "media_operations_list_characters",
    "media_operations_get_character",
    "media_operations_get_character_dashboard",
    "media_operations_get_character_context",
    "media_operations_list_persona_resources",
    "media_operations_list_platform_accounts",
    "media_operations_get_platform_account",
    "media_operations_list_research_routines",
    "media_operations_list_due_research",
    "media_operations_get_research_routine",
    "media_operations_create_research_routine",
    "media_operations_start_research_run",
    "media_operations_list_research_runs",
    "media_operations_list_research_candidates",
    "media_operations_list_character_candidates",
    "media_operations_get_research_candidate",
    "media_operations_list_research_candidate_decisions",
    "media_operations_list_character_candidate_decisions",
    "media_operations_triage_research_candidate",
    "media_operations_propose_candidate_triage",
    "media_operations_list_editorial_programs",
    "media_operations_list_due_editorial_programs",
    "media_operations_list_content_items",
    "media_operations_get_content_item",
    "media_operations_propose_content_promotion",
    "media_operations_propose_persona",
    "media_operations_create_editorial_program",
    "media_operations_create_content_item",
    "media_operations_create_content_variant",
    "media_operations_propose_qa",
    "media_operations_propose_rights",
    "media_operations_list_creative_recipes",
    "media_operations_get_creative_recipe",
    "media_operations_list_generation_workspaces",
    "media_operations_list_generation_plans",
    "media_operations_get_generation_plan",
    "media_operations_list_generation_runs",
    "media_operations_get_generation_run",
    "media_operations_create_generation_plan",
    "media_operations_propose_generation",
    "media_operations_list_content_variants",
    "media_operations_get_content_variant",
    "media_operations_get_variant_readiness",
    "media_operations_list_actions",
    "media_operations_propose_action",
    "media_operations_propose_publication",
    "media_operations_list_metric_snapshots",
    "media_operations_list_experiments",
    "media_operations_get_experiment",
    "media_operations_list_learning_proposals",
    "media_operations_propose_learning",
    "media_operations_get_calendar",
    "media_operations_get_results",
    "media_operations_list_revenue_events",
    "media_list_metric_snapshots",
    "media_list_learning_proposals",
    "media_propose_learning",
]
