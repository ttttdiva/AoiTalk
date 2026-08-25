"""Durable background extraction jobs for Scoped Memory."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_db_session
from ..memory.models import ScopedMemoryJob
from .scoped_memory_service import (
    ScopedMemoryService,
    _canonical_actor_id,
    _is_discord_principal,
)
from .outbound_privacy_service import (
    current_effective_privacy_mode,
    get_privacy_policy_context,
    reset_privacy_policy_context,
    set_privacy_policy_context,
)
from .turn_context import reset_turn_context, set_turn_context
from .docs_candidate_service import DocsCandidateService

logger = logging.getLogger(__name__)


def _safe_job_error(exc: BaseException) -> str:
    if isinstance(exc, RuntimeError) and str(exc) == "memory extraction LLM is unavailable":
        return "llm_unavailable"
    return f"{type(exc).__name__}: extraction_failed"


async def _persist_routed_candidate(
    *,
    service: ScopedMemoryService,
    user_id: str,
    normalized: dict[str, Any],
    decision: Any,
    metadata: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    """Persist one already-routed, normalized upsert candidate.

    This helper intentionally calls ``ScopedMemoryService.upsert_memory`` for
    project writes as well as user writes.  That keeps project ACL enforcement
    at the existing storage boundary and makes an ACL failure fail the job
    rather than silently falling back to a user memory.
    """

    scope_type = str(decision.scope_type or "user")
    target_project_id = (
        str(decision.project_id) if scope_type == "project" and decision.project_id else None
    )
    scope_id = target_project_id if scope_type == "project" else str(user_id)
    structured_data = dict(normalized.get("structured_data") or {})
    structured_data["source_metadata"] = dict(metadata)
    evidence_span = str(structured_data.get("evidence_span") or "").strip()
    content = str(normalized.get("content") or "").strip()
    dedupe_material = normalized.get("memory_type") or "fact"
    # Keep retries idempotent while allowing identical text in separate scopes.
    idempotency_key = (
        f"{session_id}:{scope_type}:{dedupe_material}:{content.casefold()}"
    )[:128]
    result = await service.upsert_memory(
        actor_id=str(user_id),
        content=content,
        scope_type=scope_type,
        scope_id=scope_id,
        project_id=target_project_id,
        memory_type=normalized.get("memory_type") or "fact",
        title=normalized.get("title"),
        structured_data=structured_data,
        source_type=str(decision.source_type),
        source_ref=f"conversation_session:{session_id}",
        confidence=normalized.get("confidence", 0.0),
        importance=normalized.get("importance", 1),
        evidence_refs=[
            {
                "type": "conversation",
                "session_id": session_id,
                "project_id": metadata.get("project_id"),
                "memory_job_id": metadata.get("memory_job_id"),
            }
        ],
        evidence_span={"text": evidence_span} if evidence_span else {},
        status=str(decision.status),
        expires_at=normalized.get("expires_at"),
        idempotency_key=idempotency_key,
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


def _serialize(job: ScopedMemoryJob) -> dict[str, Any]:
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
    if project_id:
        project_scope.setdefault("project_id", str(project_id))
    # Persist the effective mode itself.  This prevents a retry after a global
    # config change from silently weakening a protected/local-only turn.
    mode = current_effective_privacy_mode(config)
    if mode and mode != "direct":
        session_scope["privacy_mode"] = mode
    return {
        "user_id": str(user_id),
        "session_id": str(session_id),
        "project_id": str(project_id) if project_id else None,
        "session_context": session_scope,
        "project_metadata": project_scope,
        "privacy_mode": mode,
    }


# A shallow copy keeps the provider transport reusable, but it also aliases
# the per-turn observation containers used by the native clients.  Keep this
# list explicit: these are reset for a background extraction instead of being
# copied from the parent turn, so its metadata cannot be mixed into the job's
# context/usage/tool/audit result.
_SCOPED_MEMORY_OBSERVATION_STATE_FACTORIES: dict[str, Any] = {
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

    try:
        scoped_history = copy.copy(history_manager)
    except Exception:
        return history_manager
    if scoped_history is history_manager:
        return history_manager
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
    return scoped_history


def _isolate_scoped_memory_privacy_gateway(
    scoped_client: Any,
    *,
    session_context: dict[str, Any],
    project_metadata: dict[str, Any],
) -> None:
    """Give the job its own privacy aliases/audit while retaining config/callbacks."""

    gateway = getattr(scoped_client, "_privacy_gateway", None)
    if gateway is None:
        return
    try:
        scoped_gateway = copy.copy(gateway)
    except Exception:
        return
    if scoped_gateway is gateway:
        return
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
    for name, value in (
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
    try:
        scoped_client._privacy_gateway = scoped_gateway
    except Exception:
        logger.debug(
            "Unable to attach scoped memory privacy gateway",
            exc_info=True,
        )


def _scoped_memory_llm_client(
    llm_client: Any,
    *,
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
        # Preserve the historical compatibility path for third-party clients
        # that explicitly reject copying.  Their own extraction adapter must
        # remain side-effect-free because no generic isolation is possible.
        return llm_client
    if scoped_client is llm_client:
        return llm_client

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
    try:
        if hasattr(scoped_client, "history_manager"):
            scoped_client.history_manager = _fresh_scoped_history_manager(
                getattr(llm_client, "history_manager", None)
            )
    except Exception:
        logger.debug(
            "Unable to detach scoped memory history manager",
            exc_info=True,
        )
    try:
        if hasattr(scoped_client, "conversation_history"):
            scoped_client.conversation_history = []
    except Exception:
        logger.debug(
            "Unable to detach scoped memory conversation history",
            exc_info=True,
        )

    _isolate_scoped_memory_privacy_gateway(
        scoped_client,
        session_context=session_context,
        project_metadata=project_metadata,
    )
    return scoped_client


async def enqueue_scoped_memory_job(
    *,
    user_id: str,
    session_id: str,
    project_id: str | None,
    user_input: str,
    assistant_response: str,
    message_id: str | None = None,
    privacy_config: Any | None = None,
    session_context: Any | None = None,
    project_metadata: Any | None = None,
) -> dict[str, Any] | None:
    """Persist one encrypted job, returning the existing row on turn replay."""
    user_id = _canonical_actor_id(user_id)
    if not user_id or not session_id or not user_input.strip():
        return None
    source_message_id = (
        str(message_id).strip()[:255] if message_id is not None else None
    ) or None
    external_message_replay = _is_discord_principal(user_id) and bool(
        source_message_id
    )
    settings = await ScopedMemoryService().get_settings(
        actor_id=str(user_id), project_id=project_id
    )
    if not settings["user_auto_enabled"]:
        return None
    if project_id and settings.get("project_auto_enabled") is False:
        return None
    privacy_scope = _capture_privacy_scope(
        user_id=str(user_id),
        session_id=str(session_id),
        project_id=project_id,
        config=privacy_config,
        session_context=session_context,
        project_metadata=project_metadata,
    )
    key = _message_key(
        user_id=str(user_id),
        session_id=str(session_id),
        user_input=user_input,
        assistant_response=assistant_response,
        message_id=source_message_id,
    )
    async with await get_db_session() as session:
        lookup = [
            ScopedMemoryJob.user_id == str(user_id),
            ScopedMemoryJob.message_key == key,
        ]
        if external_message_replay:
            lookup.append(ScopedMemoryJob.source_message_id == source_message_id)
        else:
            lookup.append(ScopedMemoryJob.session_id == uuid.UUID(str(session_id)))
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
                "principal_key": str(user_id),
                # Durable jobs are retried by a later request/task. Keep the
                # original privacy scope beside the encrypted turn payload so
                # retries cannot inherit another user's global/current policy.
                "privacy_scope": privacy_scope,
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
                ScopedMemoryJob.message_key == key,
            ]
            if external_message_replay:
                retry_lookup = [
                    ScopedMemoryJob.user_id == str(user_id),
                    ScopedMemoryJob.source_message_id == source_message_id,
                ]
            else:
                retry_lookup.append(
                    ScopedMemoryJob.session_id == uuid.UUID(str(session_id))
                )
            existing = (
                await session.execute(
                    select(ScopedMemoryJob).where(*retry_lookup)
                )
            ).scalar_one()
            return _serialize(existing)
        await session.refresh(job)
        return _serialize(job)


async def process_scoped_memory_job(
    job_id: str,
    *,
    llm_client: Any,
) -> dict[str, Any]:
    """Claim and process one job; failures remain durable and retryable."""
    job_uuid = uuid.UUID(str(job_id))
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
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = datetime.utcnow()
        job.error = None
        payload = dict(job.payload or {})
        user_id = job.user_id
        session_id = str(job.session_id)
        project_id = str(job.project_id) if job.project_id else None
        attempts = job.attempts
        await session.commit()

    privacy_scope = payload.get("privacy_scope")
    if not isinstance(privacy_scope, dict):
        privacy_scope = _capture_privacy_scope(
            user_id=str(user_id),
            session_id=session_id,
            project_id=project_id,
            config=getattr(llm_client, "config", None),
        )
    session_context = dict(privacy_scope.get("session_context") or {})
    project_metadata = dict(privacy_scope.get("project_metadata") or {})
    snapshot_mode = str(privacy_scope.get("privacy_mode") or "").strip().lower()
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
    try:
        settings = await ScopedMemoryService().get_settings(
            actor_id=str(user_id), project_id=project_id
        )
        if not settings["user_auto_enabled"] or (
            project_id and settings.get("project_auto_enabled") is False
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

        existing = await list_memories(user_id)
        # Keep the provider client/transport reusable, but never share the
        # parent turn's mutable generation/usage/snapshot/tool/audit state with
        # this background extraction job.
        scoped_client = _scoped_memory_llm_client(
            llm_client,
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
        source_metadata = {
            "session_id": session_id,
            "project_id": project_id,
            "memory_job_id": str(job_id),
        }
        routed_service = ScopedMemoryService()
        changed: list[dict[str, Any]] = []
        user_delete_operations: list[dict[str, Any]] = []
        user_input = str(payload.get("user_input") or "")
        for extracted in candidates:
            decision = route_extracted_memory(
                extracted_memory=extracted,
                user_id=str(user_id),
                project_id=project_id,
                session_id=session_id,
            )
            if decision.destination == "discard":
                continue
            if decision.destination == "docs_candidate":
                # Docs candidates are review-only suggestions.  They are
                # written to the Project queue only when this job has an
                # active Project; never downgrade a project-less suggestion
                # into a user ContextMemory row.
                if not project_id:
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
                candidate_payload = {
                    "title": normalized.get("title"),
                    "content": normalized.get("content"),
                    "section_hint": structured.get("section_hint")
                    or extracted.get("section_hint"),
                    "source_metadata": {
                        "session_id": session_id,
                        "project_id": project_id,
                        "memory_job_id": str(job_id),
                    },
                }
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
                    )
                )
                continue
            action = str(
                extracted.get("action") if isinstance(extracted, dict) else "upsert"
            ).strip().lower()
            if action in {"delete", "delete_all"}:
                # Existing Dreaming delete semantics remain user-scoped. A
                # project-routed delete is never downgraded to user deletion.
                if decision.destination == "user":
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
            changed.append(
                await _persist_routed_candidate(
                    service=routed_service,
                    user_id=str(user_id),
                    normalized=normalized,
                    decision=decision,
                    metadata=source_metadata,
                    session_id=session_id,
                )
            )

        if user_delete_operations:
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
        logger.warning("Scoped Memory job %s failed: %s", job_id, exc)
        async with await get_db_session() as session:
            job = await session.get(ScopedMemoryJob, job_uuid)
            job.status = "failed"
            job.error = _safe_job_error(exc)
            job.next_retry_at = datetime.utcnow() + timedelta(
                seconds=min(3600, 30 * (2 ** max(0, attempts - 1)))
            )
            await session.commit()
            return _serialize(job)
    finally:
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
