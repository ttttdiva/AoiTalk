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
    return str(current.user_id), {
        "user_id": current.user_id,
        "project_id": current.project_id,
        "session_id": current.session_id,
        "message_id": current.message_id,
        "client_message_id": current.client_message_id,
        "tool_call_id": current.tool_call_id,
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

    actor, context = _actor_and_context()
    rows = await ScopedMemoryService().search(
        actor_id=actor,
        query=query,
        project_id=project_id or context.get("project_id"),
        task_id=task_id or None,
        session_id=context.get("session_id"),
        limit=limit,
    )
    if scope and scope != "all":
        rows = [row for row in rows if row.get("scope_type") == scope]
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
    if not reason.strip() or not idempotency_key.strip():
        raise ValueError("reason and idempotency_key are required")
    evidence = _json_refs(evidence_json)
    if not evidence:
        raise ValueError("at least one evidence reference is required")
    evidence_span = _json_object(evidence_span_json)
    project_id = (scope_id or context.get("project_id")) if scope == "project" else None
    task_id = scope_id if scope == "task" else None
    session_id = (scope_id or context.get("session_id")) if scope == "session" else None
    if expected_revision > 0:
        existing = await ScopedMemoryService().search(
            actor_id=actor,
            query=content,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            limit=5,
        )
        same_scope = next(
            (item for item in existing if item.get("scope_type") == scope), None
        )
        if same_scope and int(same_scope.get("version") or 1) != expected_revision:
            raise ValueError("memory revision changed; use memory_update with the latest revision")
    return await ScopedMemoryService().upsert_memory(
        actor_id=actor,
        content=content,
        scope_type=scope,
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
        projection_metadata={"reason": reason},
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
    return await ScopedMemoryService().move_scope(
        memory_id,
        actor_id=actor,
        expected_version=expected_version,
        scope_type=target_scope,
        scope_id=target_scope_id or None,
        project_id=(target_scope_id or context.get("project_id")) if target_scope == "project" else None,
        task_id=target_scope_id if target_scope == "task" else None,
        session_id=(target_scope_id or context.get("session_id")) if target_scope == "session" else None,
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
