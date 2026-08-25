"""ACL helpers for unified Voice Session entry points."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException

if TYPE_CHECKING:
    from ..live_voice_service import LiveVoiceActor


async def _maybe_await(value: Any) -> Any:
    import inspect

    return await value if inspect.isawaitable(value) else value


async def assert_project_write_access(
    server: Any,
    project_id: str | None,
    actor: "LiveVoiceActor",
) -> None:
    normalized = str(project_id or "").strip()
    if not normalized or actor.role == "admin":
        return
    db_manager = getattr(server, "_db_manager", None)
    if db_manager is None:
        return
    try:
        project_uuid = UUID(normalized)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid project id") from exc
    try:
        from ...memory.project_repository import ProjectRepository

        db_session = await db_manager.get_session()
        try:
            allowed = await ProjectRepository.has_permission(
                db_session,
                project_uuid,
                UUID(actor.user_id),
                "write",
            )
        finally:
            await db_session.close()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid actor id") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Project permission is unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="Project write permission denied")


async def assert_conversation_write_access(
    server: Any,
    conversation_session_id: str | None,
    actor: "LiveVoiceActor",
) -> None:
    normalized = str(conversation_session_id or "").strip()
    if not normalized or actor.role == "admin":
        return
    checker = getattr(server, "_websocket_session_allowed", None)
    if checker is None:
        raise HTTPException(
            status_code=503,
            detail="ConversationSession permission is unavailable",
        )
    try:
        allowed = await _maybe_await(
            checker(normalized, actor.user_id, require_write=True, is_admin=False)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="ConversationSession permission is unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="ConversationSession write permission denied")


__all__ = ["assert_conversation_write_access", "assert_project_write_access"]
