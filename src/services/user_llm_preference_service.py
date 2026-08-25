"""Per-user last-used LLM route persistence in users.user_settings."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ..memory.database import get_database_manager
from ..memory.user_repository import UserRepository

logger = logging.getLogger(__name__)

LAST_USED_LLM_ROUTE_KEY = "last_used_llm_route"
LAST_USED_UPDATED_AT_KEY = "updated_at"

_EMPTY_ROUTE: dict[str, str] = {"provider": "", "model": "", "effort": ""}

# Bounded so the one-time backfill scan cannot walk a large session history.
_BACKFILL_SESSION_SCAN_LIMIT = 50


def normalize_last_used_main_route(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    normalized = {
        "provider": str(raw.get("provider") or "").strip().lower(),
        "model": str(raw.get("model") or "").strip(),
        "effort": str(raw.get("effort") or raw.get("reasoning_effort") or "").strip(),
    }
    if not (normalized["provider"] and normalized["model"]):
        return dict(_EMPTY_ROUTE)
    return normalized


def has_explicit_last_used_route(route: Any) -> bool:
    normalized = normalize_last_used_main_route(route)
    return bool(normalized.get("provider") and normalized.get("model"))


def read_last_used_main_route(user_settings: Any) -> dict[str, str]:
    if not isinstance(user_settings, dict):
        return dict(_EMPTY_ROUTE)
    stored = user_settings.get(LAST_USED_LLM_ROUTE_KEY)
    return normalize_last_used_main_route(stored)


def last_used_route_updated_at(raw: Any) -> int:
    """Return stored/incoming last-used write time as epoch ms. Missing values are 0."""
    value = raw.get(LAST_USED_UPDATED_AT_KEY) if isinstance(raw, dict) else raw
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        timestamp = int(value)
        # Unix seconds sit between 1e9 and 1e12. Smaller values are already ms
        # (or a monotonic client clock used by latest-write-wins tests).
        if 1_000_000_000 <= timestamp < 1_000_000_000_000:
            return timestamp * 1000
        return max(timestamp, 0)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            return last_used_route_updated_at(int(text))
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(int(parsed.timestamp() * 1000), 0)
        except ValueError:
            return 0
    return 0


def merge_last_used_main_route(user_settings: Any, route: Any) -> dict[str, Any]:
    base = dict(user_settings) if isinstance(user_settings, dict) else {}
    if not has_explicit_last_used_route(route):
        return base
    merged = dict(base)
    stored = normalize_last_used_main_route(route)
    updated_at = last_used_route_updated_at(route)
    if updated_at > 0:
        stored[LAST_USED_UPDATED_AT_KEY] = updated_at
    merged[LAST_USED_LLM_ROUTE_KEY] = stored
    return merged


def apply_last_used_route_latest_write(
    user_settings: Any,
    route: Any,
    updated_at: Any = None,
) -> dict[str, Any]:
    """Keep the newest last-used write. Older timestamps do not overwrite."""
    base = dict(user_settings) if isinstance(user_settings, dict) else {}
    if not has_explicit_last_used_route(route):
        return base
    incoming_ts = last_used_route_updated_at(
        updated_at if updated_at is not None else route
    )
    current = base.get(LAST_USED_LLM_ROUTE_KEY)
    current_ts = last_used_route_updated_at(current)
    if current_ts > incoming_ts:
        return base
    stored = normalize_last_used_main_route(route)
    stored[LAST_USED_UPDATED_AT_KEY] = incoming_ts
    next_settings = dict(base)
    next_settings[LAST_USED_LLM_ROUTE_KEY] = stored
    return next_settings


async def _derive_last_used_from_sessions(db_session: Any, user_id: str) -> dict[str, str]:
    """Seed the preference from sessions saved before it had its own storage."""
    from sqlalchemy import desc, func, select

    from ..memory.models.conversations import ConversationSession

    stmt = (
        select(ConversationSession.context)
        .where(ConversationSession.user_id == user_id)
        .order_by(
            desc(
                func.coalesce(
                    ConversationSession.last_activity,
                    ConversationSession.session_start,
                )
            )
        )
        .limit(_BACKFILL_SESSION_SCAN_LIMIT)
    )
    result = await db_session.execute(stmt)
    for (context,) in result.all():
        if not isinstance(context, dict):
            continue
        stored = context.get("chat_llm_settings")
        if not isinstance(stored, dict):
            continue
        route = normalize_last_used_main_route(stored.get("main_route"))
        if has_explicit_last_used_route(route):
            return route
    return dict(_EMPTY_ROUTE)


def _parse_user_uuid(user_id: Any) -> UUID | None:
    if not user_id:
        return None
    try:
        return UUID(str(user_id))
    except (ValueError, TypeError):
        return None


async def get_user_last_used_main_route(user_id: Any) -> dict[str, str]:
    user_uuid = _parse_user_uuid(user_id)
    if user_uuid is None:
        return dict(_EMPTY_ROUTE)

    db_session = None
    try:
        db_manager = get_database_manager()
        db_session = await db_manager.get_session()
        user = await UserRepository.get_by_id(db_session, user_uuid)
        if not user:
            return dict(_EMPTY_ROUTE)
        settings = user.user_settings if isinstance(user.user_settings, dict) else {}
        stored_route = read_last_used_main_route(settings)
        if has_explicit_last_used_route(stored_route):
            return stored_route

        derived = await _derive_last_used_from_sessions(db_session, str(user_uuid))
        if not has_explicit_last_used_route(derived):
            return dict(_EMPTY_ROUTE)
        await UserRepository.patch_user_settings(
            db_session,
            user_uuid,
            {LAST_USED_LLM_ROUTE_KEY: derived},
            commit=True,
        )
        return derived
    except Exception:
        logger.debug("Failed to read last-used LLM route", exc_info=True)
        return dict(_EMPTY_ROUTE)
    finally:
        if db_session is not None:
            await db_session.close()


async def record_user_last_used_main_route(
    user_id: Any,
    route: Any,
    *,
    updated_at: Any = None,
) -> bool:
    if not has_explicit_last_used_route(route):
        return False

    user_uuid = _parse_user_uuid(user_id)
    if user_uuid is None:
        return False

    normalized = normalize_last_used_main_route(route)
    incoming_ts = last_used_route_updated_at(
        updated_at if updated_at is not None else route
    )
    if incoming_ts <= 0:
        incoming_ts = int(time.time() * 1000)

    db_session = None
    try:
        db_manager = get_database_manager()
        db_session = await db_manager.get_session()
        user = await UserRepository.get_by_id(db_session, user_uuid)
        if not user:
            return False
        current_settings = user.user_settings if isinstance(user.user_settings, dict) else {}
        if (
            read_last_used_main_route(current_settings) == normalized
            and last_used_route_updated_at(current_settings.get(LAST_USED_LLM_ROUTE_KEY))
            >= incoming_ts
        ):
            return True

        def _transform(merged: dict[str, Any]) -> dict[str, Any]:
            return apply_last_used_route_latest_write(merged, normalized, incoming_ts)

        updated = await UserRepository.patch_user_settings(
            db_session,
            user_uuid,
            {},
            commit=True,
            transform=_transform,
        )
        return updated is not None
    except Exception:
        logger.debug("Failed to record last-used LLM route", exc_info=True)
        return False
    finally:
        if db_session is not None:
            await db_session.close()


__all__ = [
    "LAST_USED_LLM_ROUTE_KEY",
    "LAST_USED_UPDATED_AT_KEY",
    "apply_last_used_route_latest_write",
    "get_user_last_used_main_route",
    "has_explicit_last_used_route",
    "last_used_route_updated_at",
    "merge_last_used_main_route",
    "normalize_last_used_main_route",
    "read_last_used_main_route",
    "record_user_last_used_main_route",
]
