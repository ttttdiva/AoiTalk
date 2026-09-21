"""Durable background extraction jobs for Scoped Memory."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_db_session
from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    Project,
    ScopedMemoryJob,
)
from .scoped_memory_service import (
    ScopedMemoryService,
    _canonical_actor_id,
    _is_discord_principal,
    _normalized_chat_text,
    _same_actor_id,
    _same_uuid,
    classify_sensitivity,
    classify_sensitivity_fields,
    project_memory_semantic_identity,
)
from .privacy_masking_projection import is_privacy_masking_source
from .outbound_privacy_service import (
    current_effective_privacy_mode,
    get_privacy_policy_context,
    reset_privacy_policy_context,
    set_privacy_policy_context,
)
from .turn_context import reset_turn_context, set_turn_context
from .docs_candidate_service import DocsCandidateService

logger = logging.getLogger(__name__)


def _canonical_chat_message_id(value: Any) -> str | None:
    """Return the canonical UUID for a persisted ConversationMessage.

    Client/external message identifiers are intentionally not coerced into a
    ``chat:`` evidence identity.  Only a server-issued ConversationMessage
    UUID is authoritative for cross-path reconciliation.
    """

    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _canonical_project_id(value: Any) -> str | None:
    """Return a canonical UUID string for a selected Project id."""

    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _project_ids_match(left: Any, right: Any) -> bool:
    """Compare Project ids by UUID value, not caller-controlled casing."""

    left_id = _canonical_project_id(left)
    right_id = _canonical_project_id(right)
    return (
        left_id is not None
        and right_id is not None
        and left_id == right_id
    )


def _safe_mapping(value: Any) -> dict[str, Any]:
    """Copy a provider/client mapping without trusting its concrete type.

    Durable job payloads and privacy snapshots are JSON values, but legacy
    rows and lightweight adapters can still hand the worker a list, scalar, or
    mapping implementation that raises while being copied.  Normalizing at
    the worker boundary keeps malformed metadata from escaping into the
    pre-processing path after a job has been claimed.
    """

    if not isinstance(value, Mapping):
        return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _strongest_privacy_mode(*values: Any) -> str:
    """Return the strongest bounded privacy mode in metadata values."""

    rank = {"direct": 0, "protected": 1, "local_only": 2}
    selected = "direct"
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("privacy_mode")
        mode = str(value or "").strip().casefold()
        if mode in rank and rank[mode] > rank[selected]:
            selected = mode
    return selected


def _conversation_evidence_identity(value: Any) -> str | None:
    """Return the canonical ``chat:<ConversationMessage UUID>`` identity."""

    message_id = _canonical_chat_message_id(value)
    return f"chat:{message_id}" if message_id else None


def _project_memory_semantic_identity(value: Any) -> str:
    """Resolve the shared Project Memory semantic identity helper."""

    return str(project_memory_semantic_identity(value))


# Only these fields are emitted by the server-side job provenance builder.
# Keeping the allow-list here prevents a caller that supplies a Project
# metadata mapping without a matching project id from smuggling arbitrary
# (possibly foreign-project) labels into the durable Project Memory row.
_PROJECT_PROVENANCE_KEYS = frozenset(
    {
        "session_id",
        "project_id",
        "memory_job_id",
        "source_message_id",
        "evidence_identity",
        "privacy_mode",
    }
)


async def _validate_project_job_binding_in_session(
    session: Any,
    *,
    user_id: str,
    session_id: str,
    project_id: str,
    message_id: Any = None,
    user_input: Any = None,
) -> bool:
    """Validate a Project job against an already-open DB transaction."""

    try:
        session_uuid = uuid.UUID(str(session_id).strip())
        project_uuid = uuid.UUID(str(project_id).strip())
    except (TypeError, ValueError, AttributeError):
        return False

    canonical_message_id = None
    if message_id not in (None, ""):
        canonical_message_id = _canonical_chat_message_id(message_id)
        if not canonical_message_id:
            return False

    # Project lifecycle writers lock the Project before bulk-updating linked
    # ConversationSession rows.  Take that parent lock before validating the
    # source chat so a claimed job cannot form a Session -> Project versus
    # Project -> Session deadlock during deletion/rebinding.  Compatibility
    # doubles that expose only ``get`` retain the historical non-locking path.
    execute = getattr(session, "execute", None)
    if callable(execute):
        project_result = await execute(
            select(Project)
            .where(Project.id == project_uuid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        scalar_one_or_none = getattr(project_result, "scalar_one_or_none", None)
        project = (
            scalar_one_or_none()
            if callable(scalar_one_or_none)
            else None
        )
    else:
        project = await session.get(Project, project_uuid)
    if (
        project is None
        or getattr(project, "deleted_at", None) is not None
        or bool(getattr(project, "is_completed", False))
    ):
        return False

    conversation = await session.get(ConversationSession, session_uuid)
    if conversation is None:
        return False
    if not _same_actor_id(getattr(conversation, "user_id", None), user_id):
        return False
    if not _project_ids_match(
        getattr(conversation, "project_id", None), project_uuid
    ):
        return False
    if getattr(conversation, "deleted_at", None) is not None:
        return False
    if canonical_message_id:
        message = await session.get(
            ConversationMessage,
            uuid.UUID(canonical_message_id),
        )
        if message is None:
            return False
        if not _same_uuid(getattr(message, "session_id", None), session_uuid):
            return False
        if str(getattr(message, "role", "")).strip().casefold() != "user":
            return False
        if getattr(message, "deleted_at", None) is not None:
            return False
        if not _same_actor_id(getattr(message, "sender_id", None), user_id):
            return False
        if is_privacy_masking_source(message):
            return False
        if user_input not in (None, "") and (
            not getattr(message, "content", None)
            or _normalized_chat_text(message.content)
            != _normalized_chat_text(user_input)
        ):
            return False
    return True


async def _validate_project_job_binding(
    *,
    user_id: str,
    session_id: str,
    project_id: str,
    message_id: Any = None,
    user_input: Any = None,
) -> bool:
    """Verify that a Project job is grounded in the actor's own chat.

    ``get_settings`` enforces Project ACL, but it cannot prove that an
    arbitrary session/message supplied by a legacy caller belongs to the
    selected Project.  Keep this check at the durable-job ingress so neither
    generic Project Memory nor review-only Project artifacts can be rebound
    across scopes.
    """

    async with await get_db_session() as session:
        return await _validate_project_job_binding_in_session(
            session,
            user_id=user_id,
            session_id=session_id,
            project_id=project_id,
            message_id=message_id,
            user_input=user_input,
        )


def _safe_job_error(exc: BaseException) -> str:
    if isinstance(exc, RuntimeError) and str(exc) == "memory extraction LLM is unavailable":
        return "llm_unavailable"
    return f"{type(exc).__name__}: extraction_failed"


async def _mark_claimed_job_failed(
    session,
    *,
    job_uuid: uuid.UUID,
    attempts: int,
    exc: BaseException,
) -> dict[str, Any]:
    """Persist a retryable failure for a job already claimed as ``running``.

    Claim-time validation runs before the main processing ``try`` block so
    that it can use the claim transaction's lock.  If that validation raises,
    this helper ensures the committed ``running`` row is not left stranded.
    """

    job = await session.get(ScopedMemoryJob, job_uuid)
    if job is None:
        return {"id": str(job_uuid), "status": "missing"}
    job.status = "failed"
    job.error = _safe_job_error(exc)
    job.completed_at = None
    job.next_retry_at = datetime.utcnow() + timedelta(
        seconds=min(3600, 30 * (2 ** max(0, int(attempts) - 1)))
    )
    await session.commit()
    return _serialize(job)


async def _persist_routed_candidate(
    *,
    service: ScopedMemoryService,
    user_id: str,
    normalized: dict[str, Any],
    decision: Any,
    metadata: dict[str, Any],
    session_id: str,
    source_user_input: str | None = None,
) -> dict[str, Any] | None:
    """Persist one already-routed, normalized upsert candidate.

    Project writes go through the service's atomic reconciliation boundary,
    while User writes retain the ordinary ``upsert_memory`` path.  Both keep
    ACL enforcement at the canonical storage boundary; a Project ACL or
    reconciliation failure fails the job rather than silently falling back to
    a User Memory.  ``None`` indicates that the final content sensitivity gate
    rejected the candidate or that an exact cross-path duplicate was found.
    """

    scope_type = str(decision.scope_type or "user")
    target_project_id = (
        _canonical_project_id(decision.project_id)
        if scope_type == "project" and decision.project_id
        else None
    )
    if scope_type == "project" and not target_project_id:
        # A Project destination without a server-bound id cannot be safely
        # persisted or reconciled; never manufacture a sentinel scope id.
        return None
    scope_id = target_project_id if scope_type == "project" else str(user_id)
    structured_data = dict(normalized.get("structured_data") or {})
    raw_metadata = dict(metadata or {})
    if scope_type == "project":
        claimed_project_id = raw_metadata.get("project_id")
        if (
            claimed_project_id not in (None, "")
            and not _project_ids_match(claimed_project_id, target_project_id)
        ):
            # The decision's project is the server-bound target.  A foreign
            # provenance claim must fail closed rather than being rebound and
            # written into the selected Project namespace.
            return None
        # Do not retain arbitrary metadata when the caller omitted the
        # project claim.  A Project write must carry a complete server-bound
        # provenance claim; privacy snapshots may preserve bounded policy
        # without an id, but the durable candidate boundary cannot synthesize
        # one from the extractor decision.
        if claimed_project_id in (None, ""):
            return None
        persisted_metadata = {
            key: value
            for key, value in raw_metadata.items()
            if key in _PROJECT_PROVENANCE_KEYS
        }
        persisted_metadata["project_id"] = str(target_project_id)
    else:
        persisted_metadata = raw_metadata
    structured_data["source_metadata"] = persisted_metadata
    evidence_span = str(structured_data.get("evidence_span") or "").strip()
    content = str(normalized.get("content") or "").strip()
    # The router validates extractor-declared sensitivity, but the content
    # classifier is the final server-side boundary.  This protects against a
    # malformed/mislabelled provider item (and lightweight test doubles that
    # bypass ``ScopedMemoryService.upsert_memory``'s own classifier).
    detected_sensitivity, rejection_reason = classify_sensitivity_fields(
        content,
        normalized.get("title"),
        evidence_span,
        structured_data,
    )
    if detected_sensitivity != "normal" or rejection_reason:
        return None
    dedupe_material = normalized.get("memory_type") or "fact"
    # Keep the historical user idempotency contract unchanged.  Project
    # memories additionally carry a stable semantic/evidence identity so a
    # fast-path extraction and a Project Steward reconciliation can recognize
    # the exact same durable fact without widening scope.
    evidence_identity = ""
    source_ref = f"conversation_session:{session_id}"
    evidence_ref: dict[str, Any] = {
        "type": "conversation",
        "session_id": session_id,
        "project_id": (
            str(target_project_id)
            if scope_type == "project"
            else metadata.get("project_id")
        ),
        "memory_job_id": metadata.get("memory_job_id"),
    }
    if scope_type == "project":
        source_message_id = _canonical_chat_message_id(
            metadata.get("source_message_id")
        )
        # A Project Memory must be grounded in the server-persisted
        # ConversationMessage UUID.  Session/job identifiers are useful for
        # retry idempotency, but they are not durable chat evidence and must
        # never become a substitute when a direct/legacy caller omitted the
        # canonical message provenance.
        if not source_message_id:
            return None
        evidence_identity = ""
        if source_message_id:
            # Never trust an arbitrary metadata value to mint a chat identity;
            # canonicalize it from the persisted ConversationMessage UUID.
            evidence_identity = f"chat:{source_message_id}"
        # Prefer the extractor's explicit semantic key when present.  This is
        # the same deterministic identity used by Project Steward, so a
        # wording change for one durable concept still reconciles with the
        # existing fast-path row.  Keep the content identity as a fallback
        # compatibility key for older rows that predate semantic_key.
        semantic_key = str(structured_data.get("semantic_key") or "").strip()
        if semantic_key:
            semantic_key = semantic_key[:200]
            structured_data["semantic_key"] = semantic_key
        semantic_identity = _project_memory_semantic_identity(
            semantic_key or content
        )
        content_identity = _project_memory_semantic_identity(content)
        semantic_identities = list(
            dict.fromkeys(
                value
                for value in (semantic_identity, content_identity)
                if value
            )
        )
        structured_data["semantic_identity"] = semantic_identity
        structured_data["explicit_evidence"] = bool(
            normalized.get("explicit_evidence") is True
        )
        # Persist an evidence identity only when the source carries a
        # canonical ConversationMessage UUID.  An empty placeholder would be
        # indistinguishable from an explicitly identified event to the
        # reconciliation reader.
        structured_data.pop("evidence_identity", None)
        if evidence_identity:
            structured_data["evidence_identity"] = evidence_identity
        if source_message_id:
            structured_data["source_message_id"] = source_message_id
            evidence_ref["message_id"] = source_message_id
        if evidence_identity:
            evidence_ref["evidence_id"] = evidence_identity
            evidence_ref["source_ref"] = evidence_identity
            source_ref = f"conversation:{evidence_identity}"
        evidence_or_job = evidence_identity or (
            f"memory-job:{metadata.get('memory_job_id') or session_id}"
        )
        project_material = "\n".join(
            (
                str(target_project_id or ""),
                evidence_or_job,
                semantic_identity,
            )
        )
        idempotency_key = hashlib.sha256(
            project_material.encode("utf-8")
        ).hexdigest()
        idempotency_key = f"dreaming-project:{idempotency_key}"
    else:
        # Keep retries idempotent while allowing identical text in separate
        # scopes.  This is intentionally byte-compatible with the historical
        # user-scoped key (including its 128-character bound).
        idempotency_key = (
            f"{session_id}:{scope_type}:{dedupe_material}:{content.casefold()}"
        )[:128]
    write_kwargs = {
        "content": content,
        "memory_type": normalized.get("memory_type") or "fact",
        "title": normalized.get("title"),
        "structured_data": structured_data,
        "source_type": str(decision.source_type),
        "source_ref": source_ref,
        "confidence": normalized.get("confidence", 0.0),
        "importance": normalized.get("importance", 1),
        "evidence_refs": [evidence_ref],
        "evidence_span": {"text": evidence_span} if evidence_span else {},
        "status": str(decision.status),
        "expires_at": normalized.get("expires_at"),
        "idempotency_key": idempotency_key,
    }
    if scope_type == "project":
        atomic_writer = getattr(service, "upsert_project_memory_reconciled", None)
        if not callable(atomic_writer):
            # Reconciliation is a correctness boundary for Project writes;
            # never fall back to an unguarded cross-path upsert.
            raise RuntimeError("atomic Project Memory writer is unavailable")
        result = atomic_writer(
            actor_id=str(user_id),
            project_id=str(target_project_id),
            evidence_ids=(
                [evidence_identity] if evidence_identity else []
            ),
            semantic_identities=semantic_identities,
            source_session_id=str(session_id),
            source_message_id=str(source_message_id),
            source_user_input=source_user_input,
            upsert_kwargs=write_kwargs,
        )
        if inspect.isawaitable(result):
            result = await result
        if (
            isinstance(result, dict)
            and result.get("operation") == "unchanged"
            and result.get("reason") == "project_evidence_semantic_reconciled"
        ):
            return None
    else:
        result = await service.upsert_memory(
            actor_id=str(user_id),
            scope_type="user",
            scope_id=scope_id,
            project_id=None,
            **write_kwargs,
        )
    return result.get("memory") or result


def _message_key(
    *,
    user_id: str,
    session_id: str,
    user_input: str,
    assistant_response: str,
    message_id: str | None,
) -> str:
    stable = message_id or "\0".join(
        (user_id, session_id, user_input.strip(), assistant_response.strip())
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _project_message_key(legacy_key: str, project_id: str | None) -> str:
    """Derive a Project-aware turn key without changing the jobs schema.

    ``ScopedMemoryJob`` predates Project-scoped autosave and its unique turn
    constraint is ``(user_id, session_id, message_key)``.  Prefixing the
    deterministic legacy digest with the selected Project allows the same
    conversation turn to produce one durable job per Project while keeping
    User/global jobs and legacy rows readable.  The digest remains 64 hex
    characters, so it fits the existing column unchanged.
    """

    canonical_project_id = _canonical_project_id(project_id)
    if not canonical_project_id:
        return legacy_key
    material = f"{legacy_key}\0project:{canonical_project_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _serialize(job: ScopedMemoryJob) -> dict[str, Any]:
    payload = job.payload if isinstance(getattr(job, "payload", None), dict) else {}
    processor_flags = payload.get("processor_flags")
    return {
        "id": str(job.id),
        "user_id": job.user_id,
        "principal_key": job.user_id,
        "session_id": str(job.session_id),
        "project_id": str(job.project_id) if job.project_id else None,
        "message_key": job.message_key,
        "source_message_id": job.source_message_id,
        "status": job.status,
        "attempts": job.attempts,
        "error": job.error,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "processor_flags": (
            dict(processor_flags) if isinstance(processor_flags, dict) else None
        ),
    }


def _capture_privacy_scope(
    *,
    user_id: str,
    session_id: str,
    project_id: str | None,
    config: Any | None = None,
    session_context: Any | None = None,
    project_metadata: Any | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe privacy snapshot for a durable background job."""

    inherited = get_privacy_policy_context()
    session_scope = (
        dict(session_context)
        if isinstance(session_context, dict)
        else dict(inherited.session_context or {})
    )
    project_scope = (
        dict(project_metadata)
        if isinstance(project_metadata, dict)
        else dict(inherited.project_metadata or {})
    )
    canonical_project_id = _canonical_project_id(project_id)
    snapshot_project_id = canonical_project_id or (
        str(project_id).strip() if project_id not in (None, "") else None
    )
    if project_id and snapshot_project_id:
        # The selected Project id is server-bound.  A provider/client snapshot
        # claiming another Project is stale or forged; discard that snapshot
        # rather than exposing foreign metadata to the extractor, then bind
        # the effective metadata to the selected id.
        claimed_project_id = project_scope.get("project_id")
        claimed_mode = _strongest_privacy_mode(project_scope)
        if claimed_project_id in (None, ""):
            # ``build_project_context`` carries the project id at the
            # top-level, while its metadata mapping legitimately contains
            # policy such as ``privacy_mode``.  Preserve only that bounded
            # policy field when the id is absent; discard arbitrary labels.
            project_scope = {
                key: value
                for key, value in project_scope.items()
                if key == "privacy_mode"
            }
        elif canonical_project_id:
            if not _project_ids_match(claimed_project_id, canonical_project_id):
                # Drop foreign labels, but retain the strongest bounded
                # privacy policy.  A stale provider snapshot must never turn
                # a protected/local-only turn into a direct external call.
                project_scope = (
                    {"privacy_mode": claimed_mode}
                    if claimed_mode != "direct"
                    else {}
                )
        elif str(claimed_project_id).strip().casefold() != snapshot_project_id.casefold():
            # Opaque identifiers are retained only for direct helper callers
            # and must still match byte-for-byte (ignoring case/whitespace).
            project_scope = (
                {"privacy_mode": claimed_mode}
                if claimed_mode != "direct"
                else {}
            )
        project_scope["project_id"] = snapshot_project_id
    elif project_id:
        # An empty/whitespace-only id has no usable scope.  The enqueue path
        # rejects malformed non-empty ids before reaching this helper.
        project_scope = {}
    else:
        # No active Project means no Project metadata, including anything
        # inherited from a previous request's ContextVar.
        project_scope = {}
    # Persist the effective mode itself.  This prevents a retry after a global
    # config change from silently weakening a protected/local-only turn.
    mode = current_effective_privacy_mode(config)
    effective_mode = _strongest_privacy_mode(
        mode,
        session_scope,
        project_scope,
    )
    if effective_mode != "direct":
        session_scope["privacy_mode"] = effective_mode
    return {
        "user_id": str(user_id),
        "session_id": str(session_id),
        "project_id": snapshot_project_id,
        "session_context": session_scope,
        "project_metadata": project_scope,
        "privacy_mode": effective_mode,
    }


# A shallow copy keeps the provider transport reusable, but it also aliases
# the per-turn observation containers used by the native clients.  Keep this
# list explicit: these are reset for a background extraction instead of being
# copied from the parent turn, so its metadata cannot be mixed into the job's
# context/usage/tool/audit result.
_SCOPED_MEMORY_OBSERVATION_STATE_FACTORIES: dict[str, Any] = {
    # FreeTeamClient keeps the currently leased and last-used provider
    # targets on the proxy itself.  A shallow copy would alias those target
    # objects with the foreground proxy; FreeTeam cleanup could then close a
    # live foreground transport after this background turn.  Background
    # extraction must select/own its own ephemeral target instead.
    "_active_client": lambda: None,
    "_last_client": lambda: None,
    "_scoped_memory_cleanup_owned_targets": lambda: False,
    "_last_generation_metrics": lambda: None,
    "_last_context_snapshots": list,
    "_context_request_index": lambda: 0,
    "_last_tool_calls": list,
    "_last_agentic_events": list,
    "_last_model_transcript": list,
    "_model_transcript": list,
    "_history_authoritative_model_transcript": list,
    "_history_active_model_transcript": list,
    "_last_usage": dict,
    "_last_cli_usage": dict,
    "_agent_run_usage": dict,
    "_last_usage_run_id": lambda: None,
    "_last_tool_calls_run_id": lambda: None,
    "_last_tool_loop_messages": list,
    "_last_tool_loop_completion_confirmed": lambda: False,
    "_last_audit_tool_calls": list,
    "_last_turn_tool_rounds_exhausted": lambda: False,
    "_last_turn_tool_loop_failed": lambda: False,
    "_last_generation_metadata": dict,
    "_last_route_metadata": dict,
    "_cli_native_session_info": dict,
    "_recorded_usage_responses": list,
    "_current_context_bundle": lambda: None,
    "_current_context_budget": lambda: None,
    "_current_dynamic_context": list,
    "_current_dynamic_context_metadata": dict,
    "_current_tool_hint_context": lambda: "",
    "_current_turn_system_content": lambda: "",
    "_llama_cpp_generation_lease_tickets": set,
}

_SCOPED_MEMORY_PRIVACY_GATEWAY_STATE_FACTORIES: dict[str, Any] = {
    "_raw_to_alias": dict,
    "_alias_to_raw": dict,
    "_counters": dict,
    "audit": list,
}


def _fresh_scoped_history_manager(history_manager: Any) -> Any:
    """Detach provider history without constructing or copying a transport."""

    if history_manager is None:
        return None
    try:
        scoped_history = copy.copy(history_manager)
    except Exception:
        return None
    if scoped_history is history_manager:
        return None
    for name, factory in {
        "history": list,
        "model_history": lambda: None,
        "summary": lambda: "",
        "summary_version": lambda: 0,
        "summary_checkpoint": lambda: None,
    }.items():
        try:
            if hasattr(scoped_history, name):
                setattr(scoped_history, name, factory())
        except Exception:
            logger.debug(
                "Unable to reset scoped memory history field %s",
                name,
                exc_info=True,
            )
            return None
    return scoped_history


def _clone_scoped_privacy_gateway(
    gateway: Any,
    *,
    user_id: str | None,
    session_id: str | None,
    session_context: dict[str, Any],
    project_metadata: dict[str, Any],
) -> Any | None:
    """Copy one provider gateway and bind it to the background job.

    Native AgentTurnRunner keeps its gateway on the runner rather than on the
    public LLM client.  A shallow client copy therefore still aliases that
    gateway unless it is detached explicitly.  Keep this helper independent
    of the owning attribute so both provider layouts receive the same
    fail-closed treatment.
    """

    if gateway is None:
        return None
    try:
        scoped_gateway = copy.copy(gateway)
    except Exception:
        return None
    if scoped_gateway is gateway:
        return None
    for name, factory in _SCOPED_MEMORY_PRIVACY_GATEWAY_STATE_FACTORIES.items():
        try:
            if hasattr(scoped_gateway, name):
                setattr(scoped_gateway, name, factory())
        except Exception:
            logger.debug(
                "Unable to reset scoped memory privacy gateway field %s",
                name,
                exc_info=True,
            )
            return None
    for name, value in (
        ("user_id", str(user_id or "")),
        ("session_id", str(session_id or "")),
        ("session_context", dict(session_context)),
        ("project_metadata", dict(project_metadata)),
    ):
        try:
            if hasattr(scoped_gateway, name):
                setattr(scoped_gateway, name, value)
        except Exception:
            logger.debug(
                "Unable to set scoped memory privacy gateway field %s",
                name,
                exc_info=True,
            )
            return None
    update_policy_context = getattr(scoped_gateway, "update_policy_context", None)
    if callable(update_policy_context):
        try:
            update_policy_context(
                session_context=session_context,
                project_metadata=project_metadata,
            )
        except Exception:
            logger.debug(
                "Unable to refresh scoped memory privacy gateway policy",
                exc_info=True,
            )
            return None
    return scoped_gateway


def _isolate_scoped_memory_turn_runner(
    scoped_client: Any,
    *,
    user_id: str | None,
    session_id: str | None,
    session_context: dict[str, Any],
    project_metadata: dict[str, Any],
) -> bool:
    """Detach an AgentLLMClient's nested runner from the foreground turn."""

    if not hasattr(scoped_client, "_turn_runner"):
        return True
    runner = getattr(scoped_client, "_turn_runner", None)
    if runner is None:
        return True
    try:
        scoped_runner = copy.copy(runner)
    except Exception:
        return False
    if scoped_runner is runner:
        return False

    # AgentTurnRunner mutates ProviderState and a few scalar run settings on
    # every generation.  Keep the transport client shared, but detach those
    # mutable holders before assigning the runner to the copied client.
    for name in ("provider_state", "context_budget"):
        if not hasattr(runner, name):
            continue
        original = getattr(runner, name, None)
        if original is None:
            continue
        try:
            detached = copy.copy(original)
        except Exception:
            return False
        if detached is original:
            return False
        try:
            setattr(scoped_runner, name, detached)
        except Exception:
            return False

    if hasattr(runner, "privacy_gateway"):
        original_gateway = getattr(runner, "privacy_gateway", None)
        if original_gateway is None:
            detached_gateway = None
        else:
            detached_gateway = _clone_scoped_privacy_gateway(
                original_gateway,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )
            if detached_gateway is None:
                return False
        try:
            scoped_runner.privacy_gateway = detached_gateway
        except Exception:
            return False

    try:
        scoped_client._turn_runner = scoped_runner
    except Exception:
        return False
    return True


def _isolate_scoped_memory_privacy_gateway(
    scoped_client: Any,
    *,
    user_id: str | None,
    session_id: str | None,
    session_context: dict[str, Any],
    project_metadata: dict[str, Any],
) -> bool:
    """Give the job its own privacy aliases/audit while retaining config/callbacks."""

    if not hasattr(scoped_client, "_privacy_gateway"):
        return True
    gateway = getattr(scoped_client, "_privacy_gateway", None)
    if gateway is None:
        # The extractor can wrap clients without a native gateway in its own
        # per-call gateway.  There is no shared gateway state to detach here.
        return True
    scoped_gateway = _clone_scoped_privacy_gateway(
        gateway,
        user_id=user_id,
        session_id=session_id,
        session_context=session_context,
        project_metadata=project_metadata,
    )
    if scoped_gateway is None:
        return False
    try:
        scoped_client._privacy_gateway = scoped_gateway
    except Exception:
        logger.debug(
            "Unable to attach scoped memory privacy gateway",
            exc_info=True,
        )
        return False
    return True


def _scoped_memory_llm_client(
    llm_client: Any,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    session_context: dict[str, Any],
    project_metadata: dict[str, Any],
) -> Any:
    """Create a background-only client view with isolated turn observations.

    Native clients intentionally keep their provider client/transport and
    registries reusable.  The shallow copy is therefore followed by fresh
    per-turn containers and a detached privacy gateway; otherwise extraction
    can append a local request to the parent OpenAI turn snapshot list.
    """

    try:
        scoped_client = copy.copy(llm_client)
    except Exception:
        # A background job must never fall back to the foreground client: an
        # uncopyable provider may alias history, usage, audit, or privacy
        # state.  Failing closed lets the caller record a retryable failure.
        raise RuntimeError("scoped memory LLM client isolation unavailable")
    if scoped_client is llm_client:
        raise RuntimeError("scoped memory LLM client isolation unavailable")

    if not _isolate_scoped_memory_turn_runner(
        scoped_client,
        user_id=user_id,
        session_id=session_id,
        session_context=session_context,
        project_metadata=project_metadata,
    ):
        raise RuntimeError("scoped memory turn runner isolation unavailable")

    for name, factory in _SCOPED_MEMORY_OBSERVATION_STATE_FACTORIES.items():
        try:
            if hasattr(scoped_client, name):
                setattr(scoped_client, name, factory())
        except Exception:
            logger.debug(
                "Unable to reset scoped memory client field %s",
                name,
                exc_info=True,
            )
            raise RuntimeError("scoped memory client isolation unavailable")

    # FreeTeamClient leases an ephemeral provider target per generation and
    # exposes ``cleanup`` for releasing that target.  Mark only clients that
    # carry this proxy-specific state so the job can clean them after use;
    # other providers may share a transport on shallow copy and must never be
    # closed by the background job.
    if hasattr(scoped_client, "_active_client") or hasattr(
        scoped_client, "_last_client"
    ):
        try:
            scoped_client._scoped_memory_cleanup_owned_targets = True
        except Exception:
            raise RuntimeError("scoped memory client isolation unavailable")

    try:
        if hasattr(scoped_client, "session_metadata"):
            try:
                scoped_client.session_metadata = copy.deepcopy(
                    getattr(llm_client, "session_metadata", {})
                )
            except Exception:
                scoped_client.session_metadata = dict(
                    getattr(llm_client, "session_metadata", {}) or {}
                )
    except Exception:
        logger.debug(
            "Unable to detach scoped memory session metadata",
            exc_info=True,
        )
        raise RuntimeError("scoped memory client isolation unavailable")
    for name in ("_privacy_session_context", "_privacy_project_metadata"):
        try:
            if hasattr(scoped_client, name):
                setattr(scoped_client, name, {})
        except Exception:
            logger.debug(
                "Unable to detach scoped memory client field %s",
                name,
                exc_info=True,
            )
            raise RuntimeError("scoped memory client isolation unavailable")
    try:
        if hasattr(scoped_client, "history_manager"):
            original_history = getattr(llm_client, "history_manager", None)
            detached_history = _fresh_scoped_history_manager(original_history)
            if original_history is not None and detached_history is None:
                raise RuntimeError("scoped memory history isolation unavailable")
            scoped_client.history_manager = detached_history
    except Exception:
        logger.debug(
            "Unable to detach scoped memory history manager",
            exc_info=True,
        )
        raise RuntimeError("scoped memory history isolation unavailable")
    try:
        if hasattr(scoped_client, "conversation_history"):
            scoped_client.conversation_history = []
    except Exception:
        logger.debug(
            "Unable to detach scoped memory conversation history",
            exc_info=True,
        )
        raise RuntimeError("scoped memory client isolation unavailable")

    # Native providers derive their privacy-gateway identity from these
    # fields during ``generate_memory_extraction_async``.  ``set_session_context``
    # updates the user metadata but does not update the current conversation id
    # on every provider, so explicitly bind both values after history has been
    # detached (setting the OpenAI-compatible property earlier would clear the
    # parent's aliased history manager).
    for name, value in (
        ("session_user_id", str(user_id) if user_id is not None else None),
        ("current_session_id", str(session_id) if session_id is not None else None),
        ("_current_session_id", str(session_id) if session_id is not None else None),
        (
            "current_project_id",
            str(project_metadata.get("project_id"))
            if project_metadata.get("project_id") not in (None, "")
            else None,
        ),
        ("current_include_project_context", False),
    ):
        try:
            if hasattr(scoped_client, name):
                setattr(scoped_client, name, value)
        except Exception:
            logger.debug(
                "Unable to bind scoped memory client identity field %s",
                name,
                exc_info=True,
            )
            raise RuntimeError("scoped memory client isolation unavailable")

    if not _isolate_scoped_memory_privacy_gateway(
        scoped_client,
        user_id=user_id,
        session_id=session_id,
        session_context=session_context,
        project_metadata=project_metadata,
    ):
        raise RuntimeError("scoped memory privacy isolation unavailable")
    return scoped_client


async def enqueue_scoped_memory_job(
    *,
    user_id: str,
    session_id: str,
    project_id: str | None,
    user_input: str,
    assistant_response: str,
    message_id: str | None = None,
    agent_run_id: str | None = None,
    privacy_config: Any | None = None,
    session_context: Any | None = None,
    project_metadata: Any | None = None,
) -> dict[str, Any] | None:
    """Persist one encrypted job, returning the existing row on turn replay."""
    user_id = _canonical_actor_id(user_id)
    if not user_id or not session_id or not user_input.strip():
        return None
    try:
        canonical_session_id = str(uuid.UUID(str(session_id).strip()))
    except (TypeError, ValueError, AttributeError):
        return None
    session_id = canonical_session_id
    canonical_project_id = _canonical_project_id(project_id)
    if project_id not in (None, "") and canonical_project_id is None:
        # A malformed selected Project must never be silently treated as a
        # project-less/user-scoped turn.
        return None
    if canonical_project_id:
        # Persist and propagate one canonical spelling so UUID case cannot
        # split reconciliation or privacy-snapshot identity.
        project_id = canonical_project_id
    source_message_id = (
        str(message_id).strip()[:255] if message_id is not None else None
    ) or None
    source_agent_run_id = None
    if agent_run_id:
        try:
            # AgentRun ids are server-created UUIDs.  Persist only a canonical
            # value so a caller cannot inject arbitrary correlation text into
            # the curation provenance payload.
            source_agent_run_id = str(uuid.UUID(str(agent_run_id)))
        except (TypeError, ValueError, AttributeError):
            source_agent_run_id = None
    external_message_replay = _is_discord_principal(user_id) and bool(
        source_message_id
    )
    settings = await ScopedMemoryService().get_settings(
        actor_id=str(user_id), project_id=project_id
    )
    if not isinstance(settings, Mapping):
        # A malformed settings adapter must fail closed rather than granting
        # either autosave processor through an accidental truthy value.
        settings = {}
    # Generic Memory and Project curation are independent processors.  A
    # Project turn must still reach the durable extractor when Memory
    # autosave is disabled so semantic Q&A/Docs review candidates are not
    # silently lost.  Persist the flags with the job so a retry can preserve
    # the original processor intent while rechecking current settings below.
    # ``project_auto_enabled`` is a trusted settings value, not extractor
    # output.  Require the exact boolean ``True`` so a missing/malformed
    # setting fails closed instead of implicitly enabling Project writes.
    project_auto_enabled = bool(
        project_id and settings.get("project_auto_enabled") is True
    )
    processor_flags = {
        "user_memory": settings.get("user_auto_enabled") is True,
        # Keep the historical flag for compatibility while persisting the
        # validated setting explicitly for retries and router activation.
        "project_memory": project_auto_enabled,
        "project_auto_enabled": project_auto_enabled,
        "project_curation": bool(project_id),
    }
    if not any(processor_flags.values()):
        return None

    # Resolve an existing job by its canonical message identity before
    # re-validating transport text.  A retried Web turn may carry a different
    # client-side rendering of the same persisted message; idempotency must
    # return the already-created job rather than treating that replay as a new
    # write.  Scope the lookup by actor, session, and Project so this read-only
    # fast path cannot return a job from another namespace.  The first enqueue
    # still requires full chat binding below, and processing rechecks it again
    # before any mutation.
    legacy_key = _message_key(
        user_id=str(user_id),
        session_id=str(session_id),
        user_input=user_input,
        assistant_response=assistant_response,
        message_id=source_message_id,
    )
    # Project jobs use a namespace-aware digest so the pre-existing
    # ``(user_id, session_id, message_key)`` unique constraint cannot collapse
    # two legitimate Project captures for one turn.  Legacy rows are still
    # probed below with ``legacy_key`` for a same-Project replay.
    key = _project_message_key(legacy_key, project_id)
    session_uuid = uuid.UUID(str(session_id))
    project_uuid = uuid.UUID(str(project_id)) if project_id else None
    message_key_predicate = (
        or_(
            ScopedMemoryJob.message_key == key,
            ScopedMemoryJob.message_key == legacy_key,
        )
        if project_id and key != legacy_key
        else ScopedMemoryJob.message_key == key
    )
    async with await get_db_session() as session:
        lookup = [
            ScopedMemoryJob.user_id == str(user_id),
            message_key_predicate,
            ScopedMemoryJob.session_id == session_uuid,
        ]
        lookup.append(
            ScopedMemoryJob.project_id == project_uuid
            if project_uuid is not None
            else ScopedMemoryJob.project_id.is_(None)
        )
        if external_message_replay:
            lookup.append(ScopedMemoryJob.source_message_id == source_message_id)
        existing = (
            await session.execute(select(ScopedMemoryJob).where(*lookup))
        ).scalar_one_or_none()
        if existing is not None:
            if project_id:
                existing_payload = (
                    existing.payload if isinstance(existing.payload, Mapping) else {}
                )
                # Replays may alter transport text, but they must still prove
                # that the original server-bound message remains in the
                # selected Project and actor scope before this fast-path read.
                # Processing performs the same check again before mutation.
                if not await _validate_project_job_binding(
                    user_id=str(user_id),
                    session_id=str(existing.session_id),
                    project_id=str(existing.project_id or project_id),
                    message_id=(
                        existing.source_message_id
                        or existing_payload.get("message_id")
                        or existing_payload.get("source_message_id")
                    ),
                    user_input=existing_payload.get("user_input"),
                ):
                    return None
            return _serialize(existing)

    if project_id and not await _validate_project_job_binding(
        user_id=str(user_id),
        session_id=str(session_id),
        project_id=str(project_id),
        message_id=source_message_id,
        user_input=user_input,
    ):
        # A Project job is useful only when the supplied conversation belongs
        # to the selected Project and actor.  Failing closed here also keeps
        # review-only curation artifacts from crossing Project boundaries.
        return None
    privacy_scope = _capture_privacy_scope(
        user_id=str(user_id),
        session_id=str(session_id),
        project_id=project_id,
        config=privacy_config,
        session_context=session_context,
        project_metadata=project_metadata,
    )
    async with await get_db_session() as session:
        lookup = [
            ScopedMemoryJob.user_id == str(user_id),
            message_key_predicate,
            (
                ScopedMemoryJob.project_id == project_uuid
                if project_uuid is not None
                else ScopedMemoryJob.project_id.is_(None)
            ),
        ]
        if external_message_replay:
            lookup.append(ScopedMemoryJob.source_message_id == source_message_id)
        else:
            lookup.append(ScopedMemoryJob.session_id == session_uuid)
        existing = (
            await session.execute(
                select(ScopedMemoryJob).where(*lookup)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _serialize(existing)
        job = ScopedMemoryJob(
            id=uuid.uuid4(),
            user_id=str(user_id),
            session_id=uuid.UUID(str(session_id)),
            project_id=uuid.UUID(str(project_id)) if project_id else None,
            message_key=key,
            source_message_id=source_message_id if external_message_replay else None,
            payload={
                "user_input": user_input,
                "assistant_response": assistant_response,
                # Preserve the legacy payload contract for UUID/web callers;
                # the dedicated source column is populated only for Discord.
                "message_id": (
                    source_message_id
                    if external_message_replay
                    else message_id
                ),
                # A trusted server-side run id lets curation reconcile an
                # explicit Docs mutation against the durable AgentRun ledger.
                # It is never accepted from prompt text or persisted client
                # metadata; callers obtain it from the active run ContextVar.
                "agent_run_id": source_agent_run_id,
                "principal_key": str(user_id),
                # Durable jobs are retried by a later request/task. Keep the
                # original privacy scope beside the encrypted turn payload so
                # retries cannot inherit another user's global/current policy.
                "privacy_scope": privacy_scope,
                "processor_flags": processor_flags,
            },
            status="pending",
            attempts=0,
        )
        session.add(job)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            retry_lookup = [
                ScopedMemoryJob.user_id == str(user_id),
                message_key_predicate,
                (
                    ScopedMemoryJob.project_id == project_uuid
                    if project_uuid is not None
                    else ScopedMemoryJob.project_id.is_(None)
                ),
            ]
            if external_message_replay:
                retry_lookup = [
                    ScopedMemoryJob.user_id == str(user_id),
                    ScopedMemoryJob.source_message_id == source_message_id,
                    (
                        ScopedMemoryJob.project_id == project_uuid
                        if project_uuid is not None
                        else ScopedMemoryJob.project_id.is_(None)
                    ),
                    message_key_predicate,
                ]
            else:
                retry_lookup.append(ScopedMemoryJob.session_id == session_uuid)
            existing = (
                await session.execute(
                    select(ScopedMemoryJob).where(*retry_lookup)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _serialize(existing)

            # The historical unique constraints intentionally do not include
            # ``project_id``.  If the same turn/source message is already
            # occupied by another Project, the insert can still fail even
            # though the project-aware lookup above found no row.  Check that
            # legacy idempotency key explicitly and fail closed instead of
            # returning the foreign Project job as if it were a replay.
            conflict_lookup = [ScopedMemoryJob.user_id == str(user_id)]
            if external_message_replay:
                conflict_lookup.append(
                    ScopedMemoryJob.source_message_id == source_message_id
                )
            else:
                conflict_lookup.extend(
                    [
                        ScopedMemoryJob.session_id == session_uuid,
                        ScopedMemoryJob.message_key == key,
                    ]
                )
            conflict = (
                await session.execute(
                    select(ScopedMemoryJob).where(*conflict_lookup)
                )
            ).scalar_one_or_none()
            if conflict is not None:
                logger.info(
                    "Scoped Memory job idempotency key is bound to another Project: "
                    "user_id=%s session_id=%s message_key=%s project_id=%s",
                    str(user_id),
                    str(session_id),
                    key,
                    str(getattr(conflict, "project_id", None) or ""),
                )
                return None
            # Preserve the original database error when no row explains the
            # conflict; it may be an unrelated constraint failure that should
            # remain visible to the caller/retry machinery.
            raise
        await session.refresh(job)
        return _serialize(job)


async def process_scoped_memory_job(
    job_id: str,
    *,
    llm_client: Any,
) -> dict[str, Any]:
    """Claim and process one job; failures remain durable and retryable."""
    job_uuid = uuid.UUID(str(job_id))
    # Keep the retry counter available to the outer failure handler even when
    # a legacy/corrupt row exposes a non-integer value while the claim is being
    # prepared.  Database-backed rows normally use an integer column, but
    # adapters and old fixtures can still surface malformed JSON-like values.
    attempts = 0
    async with await get_db_session() as session:
        job = (
            await session.execute(
                select(ScopedMemoryJob)
                .where(ScopedMemoryJob.id == job_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return {"id": str(job_id), "status": "missing"}
        if job.status == "completed":
            return _serialize(job)
        if job.status == "running" and job.started_at and job.started_at > datetime.utcnow() - timedelta(minutes=10):
            return _serialize(job)
        job.status = "running"
        try:
            previous_attempts = int(job.attempts or 0)
        except (TypeError, ValueError, OverflowError):
            previous_attempts = 0
        attempts = max(0, previous_attempts) + 1
        job.attempts = attempts
        job.started_at = datetime.utcnow()
        job.error = None
        # JSON columns are expected to contain mappings, but old/corrupt rows
        # and test adapters can expose arbitrary JSON values.  Normalize
        # before committing the claimed ``running`` state so malformed values
        # cannot raise and strand a job outside the durable error handler.
        payload = _safe_mapping(getattr(job, "payload", None))
        user_id = job.user_id
        session_id = str(job.session_id)
        project_id = str(job.project_id) if job.project_id else None
        # Commit the durable claim before checking mutable Project/chat
        # bindings.  A driver/validator failure after the claim must be
        # represented as a retryable ``failed`` row rather than strand the job
        # in ``running`` until the stale-claim timeout.
        await session.commit()
        if project_id:
            try:
                binding_valid = await _validate_project_job_binding_in_session(
                    session,
                    user_id=str(user_id),
                    session_id=session_id,
                    project_id=project_id,
                    message_id=payload.get("message_id") or job.source_message_id,
                    user_input=payload.get("user_input"),
                )
            except asyncio.CancelledError:
                await session.rollback()
                # Match the main cancellation path: make the claimed job
                # immediately eligible for a later retry, then propagate
                # shutdown cancellation to the caller.
                retry_job = await session.get(ScopedMemoryJob, job_uuid)
                if retry_job is not None:
                    retry_job.status = "pending"
                    retry_job.error = "cancelled_before_completion"
                    retry_job.next_retry_at = datetime.utcnow()
                    retry_job.completed_at = None
                    await session.commit()
                raise
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                return await _mark_claimed_job_failed(
                    session,
                    job_uuid=job_uuid,
                    attempts=attempts,
                    exc=exc,
                )
            if not binding_valid:
                # The conversation may have been deleted, reassigned, or
                # replaced after enqueue.  Do not process either Project
                # Memory or its review artifacts from stale/foreign evidence,
                # and never downgrade the job to a User Memory write.
                skipped_job = await session.get(ScopedMemoryJob, job_uuid)
                if skipped_job is None:
                    return {"id": str(job_id), "status": "missing"}
                skipped_job.status = "skipped"
                skipped_job.error = "project_binding_invalid"
                skipped_job.completed_at = datetime.utcnow()
                skipped_job.next_retry_at = None
                await session.commit()
                return _serialize(skipped_job)

    privacy_scope = _safe_mapping(payload.get("privacy_scope"))
    if not privacy_scope:
        privacy_scope = _capture_privacy_scope(
            user_id=str(user_id),
            session_id=session_id,
            project_id=project_id,
            config=getattr(llm_client, "config", None),
        )
    # Nested privacy fields are provider-controlled JSON and may not be
    # mappings even when the outer snapshot is valid.  Treat malformed values
    # as empty bounded metadata rather than raising after the job was
    # committed as ``running``.
    session_context = _safe_mapping(privacy_scope.get("session_context"))
    project_metadata = _safe_mapping(privacy_scope.get("project_metadata"))
    if project_id:
        claimed_project_id = project_metadata.get("project_id")
        claimed_mode = _strongest_privacy_mode(project_metadata)
        if claimed_project_id in (None, ""):
            project_metadata = {
                key: value
                for key, value in project_metadata.items()
                if key == "privacy_mode"
            }
        elif not _project_ids_match(claimed_project_id, project_id):
            # Rebind only the selected Project identity.  Preserve a bounded
            # stronger privacy mode from the stale snapshot so a foreign
            # project label cannot also downgrade the outbound policy.
            project_metadata = (
                {"privacy_mode": claimed_mode}
                if claimed_mode != "direct"
                else {}
            )
        project_metadata["project_id"] = str(project_id)
    else:
        project_metadata = {}
    snapshot_mode = _strongest_privacy_mode(
        privacy_scope.get("privacy_mode"),
        session_context,
        project_metadata,
    )
    if snapshot_mode in {"protected", "local_only"}:
        # The mode snapshot is stronger than a later global config and cannot
        # be weakened by retrying the job in a different request.
        session_context["privacy_mode"] = snapshot_mode
    privacy_token = set_privacy_policy_context(
        session_context=session_context,
        project_metadata=project_metadata,
    )
    turn_token = set_turn_context(
        user_id=str(user_id),
        project_id=project_id,
        session_id=session_id,
        include_project_context=bool(project_id),
    )
    scoped_client: Any | None = None
    try:
        settings = await ScopedMemoryService().get_settings(
            actor_id=str(user_id), project_id=project_id
        )
        if not isinstance(settings, Mapping):
            # Treat a malformed/missing settings payload as all processors
            # disabled.  The job remains durable and is marked skipped below
            # instead of attempting extraction with an ambiguous consent
            # state.
            settings = {}
        persisted_flags = payload.get("processor_flags")
        if not isinstance(persisted_flags, dict):
            # Jobs written before processor flags existed are reconstructed
            # from the current settings, with the Project gate fail-closed.
            # New jobs carry all four booleans below.
            current_project_auto_enabled = bool(
                project_id and settings.get("project_auto_enabled") is True
            )
            persisted_flags = {
                "user_memory": settings.get("user_auto_enabled") is True,
                "project_memory": current_project_auto_enabled,
                "project_auto_enabled": current_project_auto_enabled,
                "project_curation": bool(project_id),
            }
        persisted_project_auto = persisted_flags.get("project_auto_enabled")
        if "project_auto_enabled" not in persisted_flags:
            # Short migration compatibility for rows written with only the
            # historical project_memory flag.  A present but malformed value
            # is never promoted to True (fail closed).
            persisted_project_auto = persisted_flags.get("project_memory") is True
        else:
            persisted_project_auto = persisted_project_auto is True
        current_project_auto_enabled = bool(
            project_id and settings.get("project_auto_enabled") is True
        )
        project_auto_enabled = bool(
            project_id
            and persisted_project_auto is True
            and current_project_auto_enabled
        )
        user_memory_enabled = (
            persisted_flags.get("user_memory") is True
            and settings.get("user_auto_enabled") is True
        )
        project_memory_enabled = (
            persisted_flags.get("project_memory") is True
            and project_auto_enabled
        )
        project_curation_enabled = (
            persisted_flags.get("project_curation") is True
            and bool(project_id)
        )
        if not any(
            (user_memory_enabled, project_memory_enabled, project_curation_enabled)
        ):
            async with await get_db_session() as session:
                job = await session.get(ScopedMemoryJob, job_uuid)
                if job is None:
                    return {"id": str(job_id), "status": "missing"}
                job.status = "skipped"
                job.error = "disabled_by_settings"
                job.completed_at = datetime.utcnow()
                job.next_retry_at = None
                await session.commit()
                return _serialize(job)
        if llm_client is None:
            raise RuntimeError("memory extraction LLM is unavailable")
        from ..memory.dreaming_extractor import DreamingMemoryExtractor
        from .dreaming_memory_service import (
            _normalize_candidate,
            bulk_create_memories,
            list_memories,
        )
        from .memory_router import route_extracted_memory
        from .project_qa_candidate_service import (
            PROJECT_QA_MAX_CANDIDATES,
            has_matching_successful_docs_mutation,
            is_project_qa_artifact,
            persist_project_qa_candidates_for_job,
        )

        # Do not even load user-scoped memories when the generic Memory
        # processor is disabled.  Project curation remains eligible, but a
        # disabled personal auto-save setting must not expose unrelated
        # personal memory content to the background extractor.
        existing = await list_memories(user_id) if user_memory_enabled else []
        # Keep the provider client/transport reusable, but never share the
        # parent turn's mutable generation/usage/snapshot/tool/audit state with
        # this background extraction job.
        scoped_client = _scoped_memory_llm_client(
            llm_client,
            user_id=str(user_id),
            session_id=session_id,
            session_context=session_context,
            project_metadata=project_metadata,
        )
        setter = getattr(scoped_client, "set_session_context", None)
        if callable(setter):
            metadata = {
                "session_id": session_id,
                "project_id": project_id,
                "privacy_mode": session_context.get("privacy_mode") or snapshot_mode,
            }
            try:
                setter(str(user_id), metadata=metadata)
            except TypeError:
                try:
                    setter(user_id=str(user_id), metadata=metadata)
                except TypeError:
                    setter(str(user_id), metadata)
        # The copied client may carry mutable policy fields from the active
        # turn.  Overwrite both maps with the durable job snapshot so its
        # provider-specific gateway agrees with the ContextVars above even
        # after a retry or session/project rotation.
        if hasattr(scoped_client, "_privacy_session_context"):
            scoped_client._privacy_session_context = dict(session_context)
        if hasattr(scoped_client, "_privacy_project_metadata"):
            scoped_client._privacy_project_metadata = dict(project_metadata)
        extractor = DreamingMemoryExtractor()
        extract_kwargs: dict[str, Any] = {
            "llm_client": scoped_client,
            "user_id": str(user_id),
            "session_id": session_id,
            "session_context": session_context,
            "project_metadata": project_metadata,
        }
        # Preserve compatibility with integrations that monkeypatch/override
        # the historical three-positional-argument extractor contract.
        try:
            parameters = inspect.signature(extractor.extract).parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_kwargs:
                extract_kwargs = {
                    key: value
                    for key, value in extract_kwargs.items()
                    if key in parameters
                }
        except (TypeError, ValueError):
            pass
        candidates = await extractor.extract(
            str(payload.get("user_input") or ""),
            str(payload.get("assistant_response") or ""),
            existing,
            **extract_kwargs,
        )
        # A provider adapter returning ``None``/an object is a malformed
        # extraction, not an instruction to persist raw turn text.  Treat it
        # as an empty completed-turn result and keep the durable job retry
        # semantics intact.
        if not isinstance(candidates, list):
            candidates = []
        # ``message_id`` is normally the server-persisted ConversationMessage
        # UUID for Web turns.  Preserve the original bounded provenance value,
        # but only mint a ``chat:`` identity after strict UUID validation;
        # external/client identifiers (for example Discord snowflakes) remain
        # useful for job idempotency without being promoted to chat evidence.
        source_message_id = str(
            payload.get("message_id") or payload.get("source_message_id") or ""
        ).strip()[:255] or None
        evidence_identity = _conversation_evidence_identity(
            payload.get("message_id") or payload.get("source_message_id")
        )
        source_metadata = {
            "session_id": session_id,
            "project_id": project_id,
            "memory_job_id": str(job_id),
            "source_message_id": source_message_id,
            "evidence_identity": evidence_identity,
        }

        async def project_binding_valid() -> bool:
            """Recheck Project chat ownership immediately before curation writes."""

            if not project_id:
                return True
            return await _validate_project_job_binding(
                user_id=str(user_id),
                session_id=session_id,
                project_id=project_id,
                message_id=payload.get("message_id") or payload.get("source_message_id"),
                user_input=payload.get("user_input"),
            )

        routed_service = ScopedMemoryService()
        changed: list[dict[str, Any]] = []
        user_delete_operations: list[dict[str, Any]] = []
        user_input = str(payload.get("user_input") or "")
        qa_candidates: list[dict[str, Any]] = []
        for extracted in candidates:
            if is_project_qa_artifact(extracted):
                # Project Q&A is a separate review-only artifact.  Keep it out
                # of the generic memory router so a semantic question cannot
                # be downgraded to a user/project ContextMemory row.
                if (
                    project_curation_enabled
                    and isinstance(extracted, dict)
                    and len(qa_candidates) < PROJECT_QA_MAX_CANDIDATES
                ):
                    qa_candidates.append(extracted)
                continue
            decision = route_extracted_memory(
                extracted_memory=extracted,
                user_id=str(user_id),
                project_id=project_id,
                session_id=session_id,
                project_auto_enabled=project_auto_enabled,
            )
            if decision.destination == "discard":
                continue
            if decision.destination == "docs_candidate":
                # Docs candidates are review-only suggestions.  They are
                # written to the Project queue only when this job has an
                # active Project; never downgrade a project-less suggestion
                # into a user ContextMemory row.
                if (
                    not project_id
                    or not project_curation_enabled
                    or not await project_binding_valid()
                ):
                    continue
                docs_action = str(
                    extracted.get("action") if isinstance(extracted, dict) else ""
                ).strip().lower()
                if docs_action in {"delete", "delete_all"}:
                    # Docs candidates are additive review suggestions only;
                    # deletion semantics remain explicit Docs tooling and
                    # must never be inferred from Dreaming extraction.
                    continue
                try:
                    uuid.UUID(str(user_id))
                except (TypeError, ValueError, AttributeError):
                    # DocsCandidate.created_by is a real User FK.  External
                    # principals (for example ``discord:*``) have no such
                    # row, so fail closed instead of fabricating ownership.
                    logger.info(
                        "Skipping Docs candidate for non-user principal %s",
                        type(user_id).__name__,
                    )
                    continue
                normalized = _normalize_candidate(
                    extracted,
                    user_input=user_input,
                    source_type=str(decision.source_type),
                    project_id=str(project_id),
                    routed_scope="project",
                )
                if normalized is None:
                    continue
                structured = normalized.get("structured_data")
                structured = structured if isinstance(structured, dict) else {}
                evidence_span = str(
                    structured.get("evidence_span")
                    or extracted.get("evidence_span")
                    or ""
                ).strip()[:500]
                if not evidence_span:
                    continue
                detected_sensitivity, rejection_reason = classify_sensitivity_fields(
                    normalized.get("content"),
                    normalized.get("title"),
                    evidence_span,
                    structured,
                    structured.get("section_hint") or extracted.get("section_hint"),
                )
                if detected_sensitivity != "normal" or rejection_reason:
                    continue
                candidate_payload = {
                    "title": normalized.get("title"),
                    "content": normalized.get("content"),
                    "section_hint": structured.get("section_hint")
                    or extracted.get("section_hint"),
                    "source_metadata": dict(source_metadata),
                }
                # A successful canonical Docs mutation in this exact turn is
                # authoritative evidence that this same suggestion was
                # already fulfilled.  Suppress only a matching candidate;
                # unrelated inferred suggestions remain reviewable.
                if await has_matching_successful_docs_mutation(
                    project_id=str(project_id),
                    user_id=str(user_id),
                    source_message_id=payload.get("message_id"),
                    agent_run_id=payload.get("agent_run_id"),
                    content=candidate_payload,
                ):
                    continue
                changed.append(
                    await DocsCandidateService.create_candidate(
                        project_id=str(project_id),
                        created_by=str(user_id),
                        source_type=str(decision.source_type),
                        content_json=candidate_payload,
                        confidence=normalized.get("confidence", 0.0),
                        importance=normalized.get("importance", 1),
                        sensitivity="normal",
                        evidence_hash=hashlib.sha256(
                            evidence_span.encode("utf-8")
                        ).hexdigest(),
                        evidence_span=evidence_span,
                        source_job_id=str(job_id),
                        source_session_id=session_id,
                        source_message_id=payload.get("message_id"),
                        source_user_input=user_input,
                    )
                )
                continue
            # Generic Memory writes obey their individual user/project
            # settings.  Project curation above is intentionally independent
            # and remains review-only when these flags are disabled.
            if decision.destination == "project" and not project_memory_enabled:
                continue
            if decision.destination == "user" and not user_memory_enabled:
                continue
            action = str(
                extracted.get("action") if isinstance(extracted, dict) else "upsert"
            ).strip().lower()
            if action in {"delete", "delete_all"}:
                # Existing Dreaming delete semantics remain user-scoped. A
                # project-routed delete is never downgraded to user deletion.
                if decision.destination == "user" and user_memory_enabled:
                    user_delete_operations.append(extracted)
                continue
            normalized = _normalize_candidate(
                extracted,
                user_input=user_input,
                source_type=str(decision.source_type),
                project_id=(
                    str(decision.project_id)
                    if decision.destination == "project" and decision.project_id
                    else project_id
                ),
                routed_scope=str(decision.scope_type or "user"),
            )
            if normalized is None:
                continue
            persisted = await _persist_routed_candidate(
                service=routed_service,
                user_id=str(user_id),
                normalized=normalized,
                decision=decision,
                metadata=source_metadata,
                session_id=session_id,
                source_user_input=user_input,
            )
            if persisted is not None:
                changed.append(persisted)

        qa_result = {"created": 0, "updated": 0, "skipped": 0}
        if (
            qa_candidates
            and project_id
            and project_curation_enabled
            and await project_binding_valid()
        ):
            qa_result = await persist_project_qa_candidates_for_job(
                project_id=str(project_id),
                user_id=str(user_id),
                session_id=session_id,
                source_message_id=payload.get("message_id"),
                source_job_id=str(job_id),
                candidates=qa_candidates,
                user_input=user_input,
                assistant_response=str(payload.get("assistant_response") or ""),
                agent_run_id=payload.get("agent_run_id"),
            )

        if user_delete_operations and user_memory_enabled:
            changed.extend(
                await bulk_create_memories(
                    user_id,
                    user_delete_operations,
                    metadata=source_metadata,
                    user_input=user_input,
                )
            )
        async with await get_db_session() as session:
            job = await session.get(ScopedMemoryJob, job_uuid)
            job.status = "completed"
            job.completed_at = datetime.utcnow()
            job.next_retry_at = None
            job.error = None
            await session.commit()
            result = _serialize(job)
            result["candidate_count"] = len(candidates)
            result["mutation_count"] = len(changed)
            result["qa_candidate_count"] = len(qa_candidates)
            result["qa_created"] = int(qa_result.get("created", 0))
            result["qa_updated"] = int(qa_result.get("updated", 0))
            result["qa_skipped"] = int(qa_result.get("skipped", 0))
            return result
    except asyncio.CancelledError:
        # Shutdown cancellation must not strand a claimed job in "running" for
        # the stale-job timeout window. Put it back immediately for recovery.
        async with await get_db_session() as session:
            job = await session.get(ScopedMemoryJob, job_uuid)
            if job is not None:
                job.status = "pending"
                job.error = "cancelled_before_completion"
                job.next_retry_at = datetime.utcnow()
                await session.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Scoped Memory job %s failed: exception_type=%s",
            job_id,
            type(exc).__name__,
        )
        async with await get_db_session() as session:
            job = await session.get(ScopedMemoryJob, job_uuid)
            if job is None:
                return {"id": str(job_uuid), "status": "missing"}
            job.status = "failed"
            job.error = _safe_job_error(exc)
            job.next_retry_at = datetime.utcnow() + timedelta(
                seconds=min(3600, 30 * (2 ** max(0, attempts - 1)))
            )
            await session.commit()
            return _serialize(job)
    finally:
        if scoped_client is not None and getattr(
            scoped_client,
            "_scoped_memory_cleanup_owned_targets",
            False,
        ):
            cleanup = getattr(scoped_client, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup_result = cleanup()
                    if inspect.isawaitable(cleanup_result):
                        await cleanup_result
                except Exception:
                    # Cleanup must not hide the job's durable result/error, but
                    # a leaked provider target is operationally significant.
                    logger.warning(
                        "Scoped Memory provider cleanup failed",
                        exc_info=True,
                    )
        reset_turn_context(turn_token)
        reset_privacy_policy_context(privacy_token)


async def process_pending_scoped_memory_jobs(
    *,
    llm_client: Any,
    user_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Retry due/pending jobs whenever an active LLM client is available."""
    if user_id:
        user_id = _canonical_actor_id(user_id)
    now = datetime.utcnow()
    async with await get_db_session() as session:
        stmt = select(ScopedMemoryJob.id).where(
            or_(
                ScopedMemoryJob.status == "pending",
                (
                    (ScopedMemoryJob.status == "failed")
                    & (ScopedMemoryJob.next_retry_at <= now)
                ),
                (
                    (ScopedMemoryJob.status == "running")
                    & (ScopedMemoryJob.started_at < now - timedelta(minutes=10))
                ),
            )
        )
        if user_id:
            stmt = stmt.where(ScopedMemoryJob.user_id == str(user_id))
        ids = list(
            (
                await session.execute(
                    stmt.order_by(ScopedMemoryJob.created_at).limit(max(1, min(limit, 20)))
                )
            ).scalars().all()
        )
    results = []
    for pending_id in ids:
        results.append(
            await process_scoped_memory_job(str(pending_id), llm_client=llm_client)
        )
    return results


__all__ = [
    "enqueue_scoped_memory_job",
    "process_pending_scoped_memory_jobs",
    "process_scoped_memory_job",
]
