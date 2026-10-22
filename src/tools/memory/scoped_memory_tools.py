"""Dedicated tool surface for authoritative Scoped Memory mutations."""

from __future__ import annotations

import json
from typing import Any

from ..core import tool


def _actor_and_context() -> tuple[str, dict[str, Any]]:
    from ...services.turn_context import get_turn_context

    current = get_turn_context()
    if not current.user_id:
        raise PermissionError("authenticated turn context is required for memory tools")
    context = {
        "user_id": current.user_id,
        "project_id": current.project_id,
        "session_id": current.session_id,
        "task_id": current.task_id,
        "message_id": current.message_id,
        "client_message_id": current.client_message_id,
        "tool_call_id": current.tool_call_id,
    }
    # Keep the authenticated human as the memory actor.  Agent identity is
    # additive opaque lineage from the trusted server binding and never a
    # replacement principal or a prompt/model snapshot.
    binding = getattr(current, "agent_execution_binding", None)
    if binding is not None:
        agent_id = str(
            getattr(current, "agent_id", None)
            or getattr(binding, "agent_id", "")
            or ""
        ).strip()
        revision_id = str(
            getattr(current, "agent_revision_id", None)
            or getattr(binding, "agent_revision_id", "")
            or ""
        ).strip()
        if agent_id:
            context["agent_id"] = agent_id
        if revision_id:
            context["agent_revision_id"] = revision_id
        try:
            from ...services.agent_run_service import get_current_agent_run_id

            run_id = str(get_current_agent_run_id() or "").strip()
        except Exception:
            run_id = ""
        if run_id:
            context["agent_run_id"] = run_id
    return str(current.user_id), context


def _agent_retrieval_scope(context: dict[str, Any], scope: str) -> dict[str, Any] | None:
    """Return a narrow shared-memory filter for an active Agent turn."""

    from ...services.turn_context import get_turn_context

    current = get_turn_context()
    if getattr(current, "agent_execution_binding", None) is None:
        return None
    scope_key = str(scope or "").strip().casefold()
    return {
        "allowed_scope_types": (scope_key,),
        "project_id": context.get("project_id"),
        "task_id": context.get("task_id") if scope_key == "task" else None,
        "session_id": context.get("session_id") if scope_key == "session" else None,
    }


def _agent_execution_metadata(context: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(context[key])
        for key in ("agent_id", "agent_revision_id", "agent_run_id")
        if context.get(key) not in (None, "")
    }


def _json_object(value: str) -> dict[str, Any]:
    if not str(value or "").strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("value must be a JSON object")
    return parsed


def _json_refs(value: str) -> list[dict[str, Any]]:
    parsed = json.loads(str(value or ""))
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return [dict(item) for item in parsed]
    raise ValueError("evidence_json must be a JSON object or an array of objects")


@tool
async def memory_search(
    scope: str,
    query: str,
    project_id: str = "",
    task_id: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Search memories visible in the current user/project/task/session scopes."""
    from ...services.scoped_memory_service import ScopedMemoryService
    from ...services.turn_context import get_turn_context, is_project_context_enabled

    actor, context = _actor_and_context()
    scope_key = str(scope or "").strip().casefold()
    implicit_project = context.get("project_id") if is_project_context_enabled(get_turn_context()) else None
    implicit_task = context.get("task_id") if scope_key == "task" else None
    rows = await ScopedMemoryService().search(
        actor_id=actor,
        query=query,
        project_id=project_id or implicit_project,
        task_id=task_id or implicit_task,
        session_id=context.get("session_id"),
        limit=limit,
        scope_type=scope_key,
        retrieval_scope=_agent_retrieval_scope(context, scope_key),
    )
    return {"success": True, "memories": rows, "count": len(rows)}


@tool
async def memory_get(memory_id: str) -> dict[str, Any]:
    """Read one memory, including evidence, trust, status, and lineage identifiers."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, _ = _actor_and_context()
    return {"success": True, "memory": await ScopedMemoryService().get_memory(memory_id, actor_id=actor)}


@tool
async def memory_upsert(
    content: str,
    scope: str,
    scope_id: str,
    memory_type: str,
    evidence_json: str,
    evidence_span_json: str,
    reason: str,
    idempotency_key: str,
    expected_revision: int = 0,
    title: str = "",
    importance: int = 5,
    pin: bool = False,
) -> dict[str, Any]:
    """Create or deduplicate memory through the scoped pipeline; never use Docs tools for memory."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, context = _actor_and_context()
    scope_key = str(scope or "").strip().casefold()
    if not reason.strip() or not idempotency_key.strip():
        raise ValueError("reason and idempotency_key are required")
    evidence = _json_refs(evidence_json)
    if not evidence:
        raise ValueError("at least one evidence reference is required")
    evidence_span = _json_object(evidence_span_json)
    project_id = (scope_id or context.get("project_id")) if scope_key == "project" else None
    task_id = (scope_id or context.get("task_id")) if scope_key == "task" else None
    session_id = (scope_id or context.get("session_id")) if scope_key == "session" else None
    agent_scope = _agent_retrieval_scope(context, scope_key)
    if expected_revision > 0:
        existing = await ScopedMemoryService().search(
            actor_id=actor,
            query=content,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            limit=5,
            retrieval_scope=agent_scope,
        )
        same_scope = next(
            (item for item in existing if item.get("scope_type") == scope), None
        )
        if same_scope and int(same_scope.get("version") or 1) != expected_revision:
            raise ValueError("memory revision changed; use memory_update with the latest revision")
    return await ScopedMemoryService().upsert_memory(
        actor_id=actor,
        content=content,
        scope_type=scope_key,
        scope_id=scope_id or None,
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
        memory_type=memory_type,
        title=title or None,
        source_type="agent_tool",
        evidence_refs=evidence,
        evidence_span=evidence_span,
        importance=importance,
        is_pinned=pin,
        status="active",
        turn_context=context,
        projection_metadata={
            "reason": reason,
            **(
                {"agent_execution": _agent_execution_metadata(context)}
                if _agent_execution_metadata(context)
                else {}
            ),
        },
        idempotency_key=idempotency_key,
    )


@tool
async def memory_update(
    memory_id: str,
    expected_version: int,
    changes_json: str,
) -> dict[str, Any]:
    """Update memory with mandatory optimistic-lock version and retained lineage."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, context = _actor_and_context()
    return await ScopedMemoryService().update_memory(
        memory_id,
        actor_id=actor,
        changes=_json_object(changes_json),
        expected_version=expected_version,
        turn_context=context,
    )


@tool
async def memory_forget(
    memory_id: str,
    expected_version: int,
    reason: str = "explicit_forget",
) -> dict[str, Any]:
    """Soft-forget one memory. This does not hard-delete history."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, context = _actor_and_context()
    return await ScopedMemoryService().forget_memory(
        memory_id,
        actor_id=actor,
        expected_version=expected_version,
        reason=reason,
        turn_context=context,
    )


@tool
async def memory_move_scope(
    memory_id: str,
    expected_version: int,
    target_scope: str,
    target_scope_id: str,
    reason: str,
) -> dict[str, Any]:
    """Move memory to the current user, project, or session scope with lineage."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, context = _actor_and_context()
    target_scope_key = str(target_scope or "").strip().casefold()
    return await ScopedMemoryService().move_scope(
        memory_id,
        actor_id=actor,
        expected_version=expected_version,
        scope_type=target_scope_key,
        scope_id=target_scope_id or None,
        project_id=(target_scope_id or context.get("project_id")) if target_scope_key == "project" else None,
        task_id=(target_scope_id or context.get("task_id")) if target_scope_key == "task" else None,
        session_id=(target_scope_id or context.get("session_id")) if target_scope_key == "session" else None,
        reason=reason,
        turn_context=context,
    )


@tool
async def memory_promote_to_project_information(
    memory_id: str,
    expected_version: int,
    target_section: str,
    source_refs_json: str,
) -> dict[str, Any]:
    """Explicitly promote a project memory to canonical Project Information Docs."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, _ = _actor_and_context()
    source_refs = _json_refs(source_refs_json)
    return await ScopedMemoryService().promote_to_project_information(
        memory_id,
        actor_id=actor,
        expected_version=expected_version,
        target_section=target_section,
        source_refs=source_refs,
    )


@tool
async def memory_explain(memory_id: str) -> dict[str, Any]:
    """Explain memory scope, evidence, trust, dedupe identity, and lineage."""
    from ...services.scoped_memory_service import ScopedMemoryService

    actor, _ = _actor_and_context()
    return {"success": True, **(await ScopedMemoryService().explain(memory_id, actor_id=actor))}


SCOPED_MEMORY_TOOLS = [
    memory_search,
    memory_get,
    memory_upsert,
    memory_update,
    memory_forget,
    memory_move_scope,
    memory_promote_to_project_information,
    memory_explain,
]


__all__ = ["SCOPED_MEMORY_TOOLS"]
