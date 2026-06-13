"""Helpers for reading per-user settings used at prompt-build time."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import select

from ..memory.database import get_database_manager
from ..memory.models import User


def get_user_custom_instructions_sync(user_id: Optional[str]) -> Optional[str]:
    """Fetch trimmed custom instructions for a user from user_settings."""
    if not user_id:
        return None

    try:
        user_uuid = UUID(str(user_id))
    except (ValueError, TypeError):
        return None

    session = None
    try:
        db_manager = get_database_manager()
        session = db_manager.get_sync_session()
        user = session.execute(
            select(User).where(User.id == user_uuid)
        ).scalar_one_or_none()
        if not user:
            return None

        settings = user.user_settings or {}
        if not isinstance(settings, dict):
            return None

        value = settings.get("custom_instructions")
        if value is None:
            return None

        text = str(value).strip()
        return text or None
    except Exception:
        return None
    finally:
        if session is not None:
            session.close()
