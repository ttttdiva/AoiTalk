"""User/session scoped character resolution for multi-user deployments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import Request

    from ..api.server import WebChatServer


def _is_isolated_session(session: Any) -> bool:
    session_character = str(getattr(session, "character_name", "") or "")
    session_title = str(getattr(session, "title", "") or "")
    return bool(
        getattr(session, "is_group_chat", False)
        or session_character.startswith(("story_", "trpg_"))
        or session_title.startswith(("[シナリオ]", "[執筆]", "[TRPG]"))
    )


async def resolve_request_character_name(
    server: "WebChatServer",
    request: "Request",
) -> str:
    """Resolve the effective character for the authenticated request principal."""

    auth_enabled = getattr(server, "auth_enabled", True)
    fallback = str(
        getattr(server, "character_name", "")
        or server.config.get("default_character", "Assistant")
        or "Assistant"
    )
    if auth_enabled is False:
        return fallback

    user_resolver = getattr(server, "_get_user_info_from_request", None)
    user_info = await user_resolver(request) if callable(user_resolver) else None
    if not isinstance(user_info, dict):
        return fallback

    user_id = str(user_info.get("id") or user_info.get("user_id") or "").strip()
    if not user_id:
        return fallback

    from ..memory.conversation_repository import ConversationRepository
    from ..memory.user_repository import UserRepository
    from ..services.character_service import canonicalize_character_slug

    requested_session_id = str(request.query_params.get("session_id") or "").strip()
    repo = ConversationRepository()
    target_session = None
    if requested_session_id:
        target_session = await repo.get_session_by_id(
            requested_session_id,
            with_messages=False,
        )
        if target_session is not None and (
            str(target_session.user_id) != user_id
            or not target_session.is_active
            or _is_isolated_session(target_session)
        ):
            target_session = None
    else:
        active_sessions = await repo.get_user_sessions(
            user_id,
            limit=20,
            include_inactive=False,
        )
        target_session = next(
            (session for session in active_sessions if not _is_isolated_session(session)),
            None,
        )

    if target_session is not None:
        session_character = str(getattr(target_session, "character_name", "") or "").strip()
        if session_character:
            return canonicalize_character_slug(session_character)

    from ..memory.database import get_database_manager
    from uuid import UUID

    db_session = await get_database_manager().get_session()
    try:
        user = await UserRepository.get_by_id(db_session, UUID(str(user_id)))
    finally:
        await db_session.close()
    preferred = str(getattr(user, "preferred_character", "") or "").strip() if user else ""
    if preferred:
        return canonicalize_character_slug(preferred)

    return fallback


async def update_user_preferred_character(user_id: str, character_name: str) -> None:
    from uuid import UUID

    from ..memory.database import get_database_manager
    from ..memory.user_repository import UserRepository

    db_session = await get_database_manager().get_session()
    try:
        async with db_session.begin():
            await UserRepository.update_user(
                db_session,
                UUID(str(user_id)),
                preferred_character=character_name,
                commit=False,
            )
    finally:
        await db_session.close()
