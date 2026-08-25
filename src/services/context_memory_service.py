"""Scoped context memory CRUD and prompt selection helpers."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import and_, or_, select

from ..memory.database import get_db_session
from ..memory.models import ContextMemory


def _coerce_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{3,}")
_CJK_RUN_RE = re.compile(r"[ぁ-んァ-ン一-龥]{2,}")
_ALWAYS_ON_MEMORY_TYPES = {"preference", "constraint", "instruction"}
_CJK_STOP_BIGRAMS = {
    "して",
    "する",
    "した",
    "いる",
    "ある",
    "ます",
    "です",
    "こと",
    "ため",
    "よう",
    "から",
    "ので",
    "これ",
    "それ",
}


def _keywords(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    raw = str(text)
    terms = {part.casefold() for part in _ASCII_WORD_RE.findall(raw)}
    for run in _CJK_RUN_RE.findall(raw):
        clipped = run[:64]
        terms.add(clipped.casefold())
        for size in (2, 3, 4):
            if len(clipped) < size:
                continue
            for index in range(0, len(clipped) - size + 1):
                term = clipped[index : index + size].casefold()
                if size == 2 and term in _CJK_STOP_BIGRAMS:
                    continue
                terms.add(term)
                if len(terms) >= 120:
                    return terms
    return terms


def _memory_selection(
    item: Dict[str, Any],
    *,
    terms: set[str],
    project_id: Optional[str],
    task_id: Optional[str],
    session_id: Optional[str],
) -> tuple[bool, str, int]:
    """Return whether a scoped memory is useful for this specific turn.

    Broad scope eligibility and turn relevance are intentionally separate:
    being owned by the user does not mean a detailed fact belongs in every
    prompt.
    """
    haystack = f"{item.get('title') or ''}\n{item.get('content') or ''}".casefold()
    keyword_score = sum(1 for term in terms if term in haystack)
    if item.get("is_pinned"):
        return True, "pinned", keyword_score
    if keyword_score:
        return True, "current_message_keyword_match", keyword_score

    memory_type = str(item.get("memory_type") or "").casefold()
    importance = int(item.get("importance") or 0)
    if memory_type in _ALWAYS_ON_MEMORY_TYPES and importance >= 8:
        return True, "high_importance_user_guidance", 0
    if (
        importance >= 7
        and (
            (session_id and item.get("session_id") == session_id)
            or (task_id and item.get("task_id") == task_id)
        )
    ):
        return True, "active_session_or_task_scope", 0
    return False, "not_relevant_to_current_turn", 0


class ContextMemoryService:
    """Service for project/task/session-scoped memories."""

    async def create_memory(
        self,
        *,
        content: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        scope_type: str = "user",
        scope_id: Optional[str] = None,
        memory_type: str = "fact",
        title: Optional[str] = None,
        structured_data: Optional[Dict[str, Any]] = None,
        source_type: str = "manual",
        source_ref: Optional[str] = None,
        confidence: float = 1.0,
        importance: int = 5,
        status: str = "active",
        is_pinned: bool = False,
        expires_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        async with await get_db_session() as session:
            memory = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(user_id) if user_id else None,
                project_id=_coerce_uuid(project_id),
                task_id=_coerce_uuid(task_id),
                session_id=_coerce_uuid(session_id),
                scope_type=scope_type,
                scope_id=scope_id,
                memory_type=memory_type,
                title=title,
                content=content.strip(),
                structured_data=structured_data or {},
                source_type=source_type,
                source_ref=source_ref,
                confidence=confidence,
                importance=importance,
                status=status,
                is_pinned=is_pinned,
                expires_at=expires_at,
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            return memory.to_dict()

    async def list_memories(
        self,
        *,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        scope_type: Optional[str] = None,
        memory_type: Optional[str] = None,
        active_only: bool = True,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        async with await get_db_session() as session:
            stmt = select(ContextMemory)
            if active_only:
                stmt = stmt.where(ContextMemory.status == "active")
                stmt = stmt.where(
                    or_(
                        ContextMemory.expires_at.is_(None),
                        ContextMemory.expires_at > datetime.utcnow(),
                    )
                )
            if user_id is not None:
                stmt = stmt.where(ContextMemory.user_id == str(user_id))
            if project_id:
                stmt = stmt.where(ContextMemory.project_id == _coerce_uuid(project_id))
            if task_id:
                stmt = stmt.where(ContextMemory.task_id == _coerce_uuid(task_id))
            if session_id:
                stmt = stmt.where(ContextMemory.session_id == _coerce_uuid(session_id))
            if scope_type:
                stmt = stmt.where(ContextMemory.scope_type == scope_type)
            if memory_type:
                stmt = stmt.where(ContextMemory.memory_type == memory_type)

            stmt = stmt.order_by(
                ContextMemory.is_pinned.desc(),
                ContextMemory.importance.desc(),
                ContextMemory.updated_at.desc(),
            )
            if limit:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return [item.to_dict() for item in result.scalars().all()]

    async def update_memory(
        self, memory_id: str, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "scope_type",
            "scope_id",
            "memory_type",
            "title",
            "content",
            "structured_data",
            "source_type",
            "source_ref",
            "confidence",
            "importance",
            "status",
            "is_pinned",
            "expires_at",
        }
        async with await get_db_session() as session:
            memory = await session.get(ContextMemory, _coerce_uuid(memory_id))
            if memory is None:
                return None

            for key, value in data.items():
                if key not in allowed:
                    continue
                if key == "content" and value:
                    value = str(value).strip()
                setattr(memory, key, value)
            memory.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(memory)
            return memory.to_dict()

    async def archive_memory(self, memory_id: str) -> bool:
        updated = await self.update_memory(memory_id, {"status": "archived"})
        return updated is not None

    async def delete_memory(self, memory_id: str) -> bool:
        async with await get_db_session() as session:
            result = await session.execute(
                sa_delete(ContextMemory).where(ContextMemory.id == _coerce_uuid(memory_id))
            )
            await session.commit()
            return result.rowcount > 0

    async def get_memories_for_context(
        self,
        *,
        user_id: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        project_uuid = _coerce_uuid(project_id)
        task_uuid = _coerce_uuid(task_id)
        session_uuid = _coerce_uuid(session_id)
        terms = _keywords(message)

        async with await get_db_session() as session:
            conditions = [
                and_(
                    ContextMemory.scope_type == "global",
                    or_(
                        ContextMemory.user_id.is_(None),
                        ContextMemory.user_id == str(user_id),
                    ),
                ),
                and_(
                    ContextMemory.scope_type == "user",
                    ContextMemory.user_id == str(user_id),
                ),
            ]
            if project_uuid:
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "project",
                        ContextMemory.project_id == project_uuid,
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "project",
                        ContextMemory.scope_id == str(project_id),
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )
            if task_uuid:
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "task",
                        ContextMemory.task_id == task_uuid,
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "task",
                        ContextMemory.scope_id == str(task_id),
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )
            if session_uuid:
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "session",
                        ContextMemory.session_id == session_uuid,
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )
                conditions.append(
                    and_(
                        ContextMemory.scope_type == "session",
                        ContextMemory.scope_id == str(session_id),
                        or_(
                            ContextMemory.user_id.is_(None),
                            ContextMemory.user_id == str(user_id),
                        ),
                    )
                )

            stmt = (
                select(ContextMemory)
                .where(ContextMemory.status == "active")
                .where(
                    or_(
                        ContextMemory.expires_at.is_(None),
                        ContextMemory.expires_at > datetime.utcnow(),
                    )
                )
                .where(or_(*conditions))
                .order_by(
                    ContextMemory.is_pinned.desc(),
                    ContextMemory.importance.desc(),
                    ContextMemory.updated_at.desc(),
                )
                # Keep the always-on path bounded.  Older/detail memories remain
                # available through the explicit search_past_chats semantic path.
                .limit(max(limit * 25, 200))
            )
            result = await session.execute(stmt)
            rows = [item.to_dict() for item in result.scalars().all()]

        selected: list[Dict[str, Any]] = []
        for item in rows:
            include, reason, keyword_score = _memory_selection(
                item,
                terms=terms,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
            )
            if not include:
                continue
            item = dict(item)
            item["selection_reason"] = reason
            item["_keyword_score"] = keyword_score
            selected.append(item)

        def score(item: Dict[str, Any]) -> tuple[int, int, int]:
            keyword_score = int(item.get("_keyword_score") or 0)
            scope_score = 0
            if item.get("session_id") == session_id:
                scope_score += 3
            if item.get("task_id") == task_id:
                scope_score += 2
            if item.get("project_id") == project_id:
                scope_score += 1
            return (
                1 if item.get("is_pinned") else 0,
                scope_score + keyword_score,
                int(item.get("importance") or 0),
            )

        selected.sort(key=score, reverse=True)
        for item in selected:
            item.pop("_keyword_score", None)
        return selected[:limit]

    @staticmethod
    def render_memories_for_prompt(memories: List[Dict[str, Any]]) -> str:
        if not memories:
            return ""
        lines = ["## Dreaming Memory"]
        for memory in memories:
            prefix = memory.get("memory_type") or "memory"
            title = memory.get("title")
            content = (memory.get("content") or "").strip()
            if not content:
                continue
            label = f"{prefix}: {title}" if title else prefix
            lines.append(f"- [{label}] {content}")
        return "\n".join(lines)


_service = ContextMemoryService()


async def create_memory(**kwargs) -> Dict[str, Any]:
    return await _service.create_memory(**kwargs)


async def list_memories(**kwargs) -> List[Dict[str, Any]]:
    return await _service.list_memories(**kwargs)


async def update_memory(memory_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return await _service.update_memory(memory_id, data)


async def archive_memory(memory_id: str) -> bool:
    return await _service.archive_memory(memory_id)


async def delete_memory(memory_id: str) -> bool:
    return await _service.delete_memory(memory_id)


async def get_memories_for_context(**kwargs) -> List[Dict[str, Any]]:
    return await _service.get_memories_for_context(**kwargs)
