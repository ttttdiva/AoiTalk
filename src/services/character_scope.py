"""User/session scoped character resolution for multi-user deployments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import Request

    from ..api.server import WebChatServer


def _is_isolated_session(session: Any) -> bool:
    return bool(getattr(session, "is_group_chat", False))


def _character_slug_for_catalog(
    character_name: str,
    character_options: list[dict[str, Any]],
) -> str:
    """Resolve a configured or preferred identity to an available catalog slug."""
    from ..services.character_service import canonicalize_character_slug

    value = str(character_name or "").strip()
    if not value:
        return ""

    canonical_value = canonicalize_character_slug(value)
    valid_options = [
        option
        for option in character_options
        if isinstance(option, dict)
        and isinstance(option.get("slug"), str)
        and option["slug"].strip()
        and isinstance(option.get("name"), str)
        and option["name"].strip()
    ]

    slug_matches = [
        option["slug"].strip()
        for option in valid_options
        if canonicalize_character_slug(option["slug"].strip()) == canonical_value
    ]
    if slug_matches:
        # Prefer the canonical project-manager slug when both legacy and
        # canonical entries are present, while keeping a legacy-only catalog
        # addressable by its actual slug.
        return next(
            (slug for slug in slug_matches if slug == canonical_value),
            slug_matches[0],
        )

    name_matches = [
        option["slug"].strip()
        for option in valid_options
        if option["name"].strip() == value
    ]
    return name_matches[0] if len(name_matches) == 1 else ""


async def resolve_request_character_name(
    server: "WebChatServer",
    request: "Request",
    *,
    character_options: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Resolve the effective character for the authenticated request principal."""

    auth_enabled = getattr(server, "auth_enabled", True)
    configured_default = str(
        server.config.get("default_character", "") or ""
    ).strip()
    fallback = str(
        configured_default
        or getattr(server, "character_name", "")
        or "Assistant"
    )

    def catalog_default() -> str:
        if character_options is None:
            return fallback
        return _character_slug_for_catalog(configured_default, character_options)

    if auth_enabled is False:
        return catalog_default()

    user_resolver = getattr(server, "_get_user_info_from_request", None)
    if not callable(user_resolver):
        return fallback
    user_info = await user_resolver(request)
    if not isinstance(user_info, dict):
        return catalog_default()

    user_id = str(user_info.get("id") or user_info.get("user_id") or "").strip()
    if not user_id:
        return catalog_default()

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
    if character_options is not None:
        preferred_slug = _character_slug_for_catalog(preferred, character_options)
        if preferred_slug:
            return preferred_slug
        default_slug = _character_slug_for_catalog(
            configured_default,
            character_options,
        )
        if default_slug:
            return default_slug
        # A catalog-aware caller must not return an identity the client will
        # reject, or smuggle an internal runtime character into that catalog.
        return ""

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
