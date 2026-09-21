"""Fail-closed Project-bound chat retrieval for Knowledge Capture.

This module intentionally does not import or delegate to ``search_past_chats``
or ``read_chat_session``.  Those legacy tools are user-scoped and their broad
search mode is not a sufficient Project boundary for a curator.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select

from ..memory.models import ConversationMessage, ConversationParticipant, ConversationSession, Project
from .privacy_masking_projection import is_privacy_masking_source, public_model_message_metadata


MAX_CHAT_RESULT_LIMIT = 64
MAX_CHAT_PAGE_SIZE = 200
MAX_CHAT_EXCERPT_CHARS = 1_200
MAX_CHAT_CONTENT_CHARS = 8_000


class ProjectChatSearchError(RuntimeError):
    """A Project-bound chat read was denied or malformed."""


class ProjectChatAccessDenied(ProjectChatSearchError, PermissionError):
    """The actor cannot read this exact Project/session."""


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _uuid(value: Any, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectChatSearchError(f"invalid {field_name}") from exc


def _actor_id(actor: Any) -> str:
    if isinstance(actor, Mapping):
        value = actor.get("user_id", actor.get("actor_id", actor.get("id")))
    else:
        value = getattr(actor, "user_id", getattr(actor, "actor_id", getattr(actor, "id", actor)))
    value = str(value or "").strip()
    if not value:
        raise ProjectChatAccessDenied("actor identity is required")
    return value


def _actor_role(actor: Any) -> str:
    if isinstance(actor, Mapping):
        return str(actor.get("role") or actor.get("user_role") or "").casefold().strip()
    return str(getattr(actor, "role", getattr(actor, "user_role", "")) or "").casefold().strip()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    return str(value)


def _content(message: Any) -> str:
    try:
        return str(_field(message, "content", "") or "")
    except Exception:
        return ""


def _terms(query: Any) -> list[str]:
    return [part.casefold() for part in str(query or "").strip().split() if part.strip()]


def _excerpt(content: str, terms: list[str]) -> str:
    content = content[:MAX_CHAT_CONTENT_CHARS]
    folded = content.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, min(positions or [0]) - 360)
    end = min(len(content), start + MAX_CHAT_EXCERPT_CHARS)
    value = content[start:end]
    if start:
        value = "…" + value
    if end < len(content):
        value += "…"
    return value


def _session_payload(session: Any) -> dict[str, Any]:
    return {
        "session_id": str(_field(session, "id")),
        "project_id": str(_field(session, "project_id")) if _field(session, "project_id") else None,
        "title": str(_field(session, "title", "") or "")[:240],
        "character_name": str(_field(session, "character_name", "") or "")[:200],
        "message_count": max(0, int(_field(session, "message_count", 0) or 0)),
        "session_start": _iso(_field(session, "session_start")),
        "last_activity": _iso(_field(session, "last_activity")),
        "is_active": bool(_field(session, "is_active", False)),
    }


def _message_payload(message: Any, *, include_metadata: bool, excerpt_terms: list[str] | None = None) -> dict[str, Any]:
    content = _content(message)
    payload = {
        "message_id": str(_field(message, "id")),
        "session_id": str(_field(message, "session_id")),
        "role": str(_field(message, "role", "")),
        "content": content[:MAX_CHAT_CONTENT_CHARS],
        "excerpt": _excerpt(content, excerpt_terms or []),
        "sender_type": _field(message, "sender_type"),
        "sender_display_name": str(_field(message, "sender_display_name", "") or "")[:200],
        "created_at": _iso(_field(message, "created_at")),
        "updated_at": _iso(_field(message, "updated_at")),
    }
    if include_metadata:
        safe_metadata = public_model_message_metadata(_field(message, "message_metadata", {}) or {})
        payload["metadata"] = safe_metadata if isinstance(safe_metadata, Mapping) else {}
    return payload


def _count_value(result: Any) -> int:
    scalar_one = getattr(result, "scalar_one", None)
    if callable(scalar_one):
        return int(scalar_one() or 0)
    scalar = getattr(result, "scalar", None)
    if callable(scalar):
        return int(scalar() or 0)
    return 0


async def _scalars(session: Any, statement: Any, *, limit: int | None = None) -> list[Any]:
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return []
    try:
        result = await execute(statement)
        scalar_result = result.scalars() if callable(getattr(result, "scalars", None)) else result
        values = scalar_result.all() if callable(getattr(scalar_result, "all", None)) else list(scalar_result or [])
        values = list(values)
        return values[:limit] if limit is not None else values
    except Exception:
        return []


async def _project_readable(session: Any, project_id: uuid.UUID, actor: Any) -> bool:
    actor_text = _actor_id(actor)
    actor_uuid: uuid.UUID | None
    try:
        actor_uuid = _uuid(actor_text, "actor_id")
    except ProjectChatSearchError:
        return False
    try:
        from ..memory.project_repository import ProjectRepository

        return bool(await ProjectRepository.has_permission(session, project_id, actor_uuid, "read"))
    except Exception:
        project = None
        try:
            result = await session.execute(select(Project).where(Project.id == project_id, Project.deleted_at.is_(None)))
            project = result.scalar_one_or_none()
        except Exception:
            project = None
        if project is None:
            return False
        if str(_field(project, "owner_id")) == actor_text or _actor_role(actor) == "admin":
            return True
        for member in _field(project, "members", ()) or ():
            if str(_field(member, "user_id")) == actor_text:
                permissions = _field(member, "permissions", {})
                return isinstance(permissions, Mapping) and permissions.get("read") is True
        return False


@dataclass(frozen=True, slots=True)
class ProjectScopedChatSearch:
    """A search/read object whose Project binding cannot be model-controlled."""

    session: Any
    project_id: uuid.UUID | str
    actor: Any
    max_results: int = MAX_CHAT_RESULT_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _uuid(self.project_id, "project_id"))
        object.__setattr__(self, "max_results", min(max(int(self.max_results), 1), MAX_CHAT_RESULT_LIMIT))

    async def _authorized_session(self, session_id: Any) -> Any | None:
        try:
            requested = _uuid(session_id, "session_id")
        except ProjectChatSearchError:
            return None
        actor_text = _actor_id(self.actor)
        if not await _project_readable(self.session, self.project_id, self.actor):
            raise ProjectChatAccessDenied("Project read access denied")
        # The project and actor predicates are deliberately repeated in the
        # point read; a guessed UUID must not reveal whether a foreign row
        # exists.
        participant_exists = select(ConversationParticipant.session_id).where(
            ConversationParticipant.session_id == ConversationSession.id,
            ConversationParticipant.participant_type == "user",
            ConversationParticipant.participant_id == actor_text,
            ConversationParticipant.status == "joined",
        ).exists()
        rows = await _scalars(
            self.session,
            select(ConversationSession).where(
                ConversationSession.id == requested,
                ConversationSession.project_id == self.project_id,
                ConversationSession.deleted_at.is_(None),
                or_(ConversationSession.user_id == actor_text, participant_exists),
            ).limit(1),
        )
        if not rows:
            return None
        return rows[0]

    async def search_project_chats(
        self,
        query: str,
        *,
        limit: int = 20,
        session_id: str | uuid.UUID | None = None,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        terms = _terms(query)
        if not terms:
            return {"success": False, "error": "query is empty", "results": [], "count": 0}
        try:
            if not await _project_readable(self.session, self.project_id, self.actor):
                raise ProjectChatAccessDenied("Project read access denied")
            session_filter = None
            if session_id is not None:
                session_filter = _uuid(session_id, "session_id")
            actor_text = _actor_id(self.actor)
            participant_exists = select(ConversationParticipant.session_id).where(
                ConversationParticipant.session_id == ConversationSession.id,
                ConversationParticipant.participant_type == "user",
                ConversationParticipant.participant_id == actor_text,
                ConversationParticipant.status == "joined",
            ).exists()
            session_conditions = [
                ConversationSession.project_id == self.project_id,
                ConversationSession.deleted_at.is_(None),
                or_(ConversationSession.user_id == actor_text, participant_exists),
            ]
            if session_filter is not None:
                session_conditions.append(ConversationSession.id == session_filter)
            sessions = await _scalars(
                self.session,
                select(ConversationSession).where(and_(*session_conditions)).order_by(ConversationSession.last_activity.desc()).limit(MAX_CHAT_RESULT_LIMIT),
            )
            if not sessions:
                return {"success": True, "project_id": str(self.project_id), "results": [], "count": 0}
            messages = await _scalars(
                self.session,
                select(ConversationMessage).where(
                    ConversationMessage.session_id.in_([_field(row, "id") for row in sessions]),
                    ConversationMessage.deleted_at.is_(None),
                    ConversationMessage.is_active_branch.is_(True),
                ).order_by(ConversationMessage.created_at.desc()).limit(MAX_CHAT_PAGE_SIZE),
            )
        except ProjectChatAccessDenied:
            return {"success": False, "error": "Project chat access denied", "results": [], "count": 0}
        except Exception:
            return {"success": False, "error": "Project chat search unavailable", "results": [], "count": 0}

        session_by_id = {str(_field(row, "id")): row for row in sessions}
        results: list[dict[str, Any]] = []
        for message in messages:
            if is_privacy_masking_source(message):
                continue
            content = _content(message)
            folded = content.casefold()
            if not all(term in folded for term in terms):
                continue
            session = session_by_id.get(str(_field(message, "session_id")))
            if session is None:
                continue
            results.append(
                {
                    "project_id": str(self.project_id),
                    "session": _session_payload(session),
                    "message": _message_payload(message, include_metadata=include_metadata, excerpt_terms=terms),
                }
            )
            if len(results) >= min(max(int(limit), 1), self.max_results):
                break
        return {
            "success": True,
            "project_id": str(self.project_id),
            "results": results,
            "count": len(results),
            "truncated": len(results) >= min(max(int(limit), 1), self.max_results),
        }

    async def read_project_chat_session(
        self,
        session_id: str | uuid.UUID,
        *,
        limit: int = 200,
        offset: int = 0,
        order: str = "asc",
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        try:
            session = await self._authorized_session(session_id)
            if session is None:
                return {"success": False, "error": "Project chat session not found", "messages": []}
            filters = (
                ConversationMessage.session_id == _field(session, "id"),
                ConversationMessage.deleted_at.is_(None),
                ConversationMessage.is_active_branch.is_(True),
            )
            safe_limit = min(max(int(limit), 1), MAX_CHAT_PAGE_SIZE)
            safe_offset = min(max(int(offset), 0), 1_000_000)
            order_name = "desc" if str(order or "asc").casefold() == "desc" else "asc"
            created_order = (
                ConversationMessage.created_at.desc()
                if order_name == "desc"
                else ConversationMessage.created_at.asc()
            )
            id_order = (
                ConversationMessage.id.desc()
                if order_name == "desc"
                else ConversationMessage.id.asc()
            )
            count_result = await self.session.execute(
                select(func.count())
                .select_from(ConversationMessage)
                .where(*filters)
            )
            total = _count_value(count_result)
            rows = await _scalars(
                self.session,
                select(ConversationMessage)
                .where(*filters)
                .order_by(created_order, id_order)
                .offset(safe_offset)
                .limit(safe_limit + 1),
            )
        except ProjectChatAccessDenied:
            return {"success": False, "error": "Project chat access denied", "messages": []}
        except Exception:
            return {"success": False, "error": "Project chat session unavailable", "messages": []}
        has_more = len(rows) > safe_limit
        window = [message for message in rows[:safe_limit] if not is_privacy_masking_source(message)]
        return {
            "success": True,
            "project_id": str(self.project_id),
            "session": _session_payload(session),
            "messages": [_message_payload(message, include_metadata=include_metadata) for message in window],
            "total_message_count": total,
            "returned_message_count": len(window),
            "offset": safe_offset,
            "order": order_name,
            "has_more": has_more,
        }


async def search_project_chats(
    session: Any,
    project_id: str | uuid.UUID,
    actor: Any,
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return await ProjectScopedChatSearch(session, project_id, actor).search_project_chats(query, **kwargs)


async def read_project_chat_session(
    session: Any,
    project_id: str | uuid.UUID,
    actor: Any,
    session_id: str | uuid.UUID,
    **kwargs: Any,
) -> dict[str, Any]:
    return await ProjectScopedChatSearch(session, project_id, actor).read_project_chat_session(session_id, **kwargs)


StrictProjectChatSearch = ProjectScopedChatSearch


__all__ = [
    "MAX_CHAT_CONTENT_CHARS",
    "MAX_CHAT_EXCERPT_CHARS",
    "ProjectChatAccessDenied",
    "ProjectChatSearchError",
    "ProjectScopedChatSearch",
    "StrictProjectChatSearch",
    "read_project_chat_session",
    "search_project_chats",
]
