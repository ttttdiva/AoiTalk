"""Explicit, bounded Agent work/activity reference tool."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .core import ToolDefinition, ToolParam


async def _human_session_acl(
    session_id: str,
    *,
    user_id: str | None = None,
    user_role: str | None = None,
) -> bool:
    from ..memory.database import get_database_manager
    from ..memory.models import ConversationParticipant, ConversationSession
    from sqlalchemy import and_, select

    try:
        sid = UUID(str(session_id))
        uid = UUID(str(user_id)) if user_id else None
    except (TypeError, ValueError):
        return False
    manager = get_database_manager()
    session = manager.get_session()
    db = await session if hasattr(session, "__await__") else session
    try:
        row = await db.get(ConversationSession, sid)
        if row is None or getattr(row, "deleted_at", None) is not None:
            return False
        if str(user_role or "").casefold() == "admin":
            return True
        if uid is None:
            return False
        if str(row.user_id) == str(uid):
            return True
        result = await db.execute(
            select(ConversationParticipant).where(
                and_(
                    ConversationParticipant.session_id == sid,
                    ConversationParticipant.participant_id == uid,
                    ConversationParticipant.participant_type == "user",
                    ConversationParticipant.status == "joined",
                )
            )
        )
        return result.scalar_one_or_none() is not None
    finally:
        await db.close()


async def _human_project_acl(
    project_id: str,
    *,
    user_id: str | None = None,
    user_role: str | None = None,
) -> bool:
    from ..memory.database import get_database_manager
    from ..memory.project_repository import ProjectRepository

    try:
        pid = UUID(str(project_id))
        uid = UUID(str(user_id)) if user_id else None
    except (TypeError, ValueError):
        return False
    if uid is None:
        return False
    manager = get_database_manager()
    session = manager.get_session()
    db = await session if hasattr(session, "__await__") else session
    try:
        return bool(
            await ProjectRepository.has_permission(
                db,
                project_id=pid,
                user_id=uid,
                permission="read",
            )
        )
    finally:
        await db.close()


async def _read_agent_activity(limit: int = 20) -> str:
    from ..services.agent_run_service import AgentRunService
    from ..services.turn_context import get_turn_context

    context = get_turn_context()
    agent_id = str(context.agent_id or "").strip()
    user_id = str(context.user_id or "").strip()
    if not agent_id or not user_id:
        return json.dumps(
            {"success": False, "error": "Agent activity requires a trusted Agent/human turn"},
            ensure_ascii=False,
        )
    result = await AgentRunService().list_agent_activity(
        agent_id,
        user_id=user_id,
        session_acl=_human_session_acl,
        project_acl=_human_project_acl,
        limit=max(1, min(int(limit or 20), 50)),
    )
    return json.dumps({"success": True, **result}, ensure_ascii=False)


def build_agent_activity_tool() -> ToolDefinition:
    return ToolDefinition(
        name="agent_activity",
        description=(
            "Read bounded status and safe resource references from the current Agent's "
            "permission-checked work/run ledger. Conversation bodies and tool arguments are excluded."
        ),
        function=_read_agent_activity,
        parameters=[
            ToolParam(
                name="limit",
                type="integer",
                required=False,
                default=20,
                description="Maximum number of bounded activity entries.",
            )
        ],
        is_async=True,
        risk="low",
        side_effect="none",
        requires_approval=False,
        supports_parallel=False,
        owner="agent_activity",
    )


__all__ = ["build_agent_activity_tool"]
