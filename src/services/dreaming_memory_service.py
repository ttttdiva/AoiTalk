"""Dreaming-style long-term memory backed by scoped context memories."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete as sa_delete, or_, select

from ..memory.database import get_db_session
from ..memory.models import ContextMemory

logger = logging.getLogger(__name__)

DREAMING_SCOPE_TYPE = "user"
DREAMING_DEFAULT_TYPE = "fact"
DREAMING_MANUAL_SOURCE = "manual"
DREAMING_AUTO_SOURCE = "dreaming_auto"

_ALLOWED_TYPES = {
    "fact",
    "preference",
    "constraint",
    "project",
    "workflow",
    "relationship",
    "instruction",
}


def _coerce_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _normalize_memory_type(value: Any) -> str:
    memory_type = str(value or DREAMING_DEFAULT_TYPE).strip().lower()
    return memory_type if memory_type in _ALLOWED_TYPES else DREAMING_DEFAULT_TYPE


def _coerce_confidence(value: Any, default: float = 0.75) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return min(1.0, max(0.0, confidence))


def _coerce_importance(value: Any, default: int = 5) -> int:
    try:
        importance = int(value)
    except (TypeError, ValueError):
        importance = default
    return min(10, max(1, importance))


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _normalize_candidate(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return {
            "content": content,
            "memory_type": DREAMING_DEFAULT_TYPE,
            "confidence": 0.7,
            "importance": 5,
            "structured_data": {},
        }

    if not isinstance(item, dict):
        return None

    content = str(item.get("content") or item.get("value") or "").strip()
    if not content:
        return None

    structured_data = {
        "reason": item.get("reason"),
        "sensitivity": item.get("sensitivity") or "normal",
    }
    structured_data.update(
        {
            str(key): value
            for key, value in (item.get("structured_data") or {}).items()
            if isinstance(key, str)
        }
    )

    return {
        "content": content,
        "memory_type": _normalize_memory_type(item.get("memory_type") or item.get("key")),
        "title": str(item.get("title") or "").strip() or None,
        "confidence": _coerce_confidence(item.get("confidence")),
        "importance": _coerce_importance(item.get("importance")),
        "expires_at": _parse_datetime(item.get("expires_at")),
        "structured_data": structured_data,
    }


def _to_dict(memory: ContextMemory) -> Dict[str, Any]:
    data = memory.to_dict()
    data["is_active"] = data.get("status") == "active"
    return data


async def list_memories(
    user_id: str,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    """List user-scoped Dreaming memories."""
    async with await get_db_session() as session:
        stmt = (
            select(ContextMemory)
            .where(ContextMemory.user_id == str(user_id))
            .where(ContextMemory.scope_type == DREAMING_SCOPE_TYPE)
            .order_by(
                ContextMemory.is_pinned.desc(),
                ContextMemory.importance.desc(),
                ContextMemory.updated_at.desc(),
            )
        )
        if active_only:
            stmt = stmt.where(ContextMemory.status == "active").where(
                or_(
                    ContextMemory.expires_at.is_(None),
                    ContextMemory.expires_at > datetime.utcnow(),
                )
            )
        result = await session.execute(stmt)
        return [_to_dict(memory) for memory in result.scalars().all()]


async def get_memory(
    memory_id: str,
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    async with await get_db_session() as session:
        memory = await session.get(ContextMemory, _coerce_uuid(memory_id))
        if memory is None or memory.scope_type != DREAMING_SCOPE_TYPE:
            return None
        if user_id is not None and memory.user_id != str(user_id):
            return None
        return _to_dict(memory)


async def create_memory(
    user_id: str,
    content: str,
    source_type: str = DREAMING_MANUAL_SOURCE,
    memory_type: str = DREAMING_DEFAULT_TYPE,
    metadata: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    confidence: float = 1.0,
    importance: int = 7,
) -> Dict[str, Any]:
    async with await get_db_session() as session:
        memory = ContextMemory(
            id=uuid.uuid4(),
            user_id=str(user_id),
            scope_type=DREAMING_SCOPE_TYPE,
            scope_id=str(user_id),
            memory_type=_normalize_memory_type(memory_type),
            title=title,
            content=content.strip(),
            structured_data=metadata or {},
            source_type=source_type,
            source_ref=None,
            confidence=_coerce_confidence(confidence, default=1.0),
            importance=_coerce_importance(importance, default=7),
            status="active",
            is_pinned=source_type == DREAMING_MANUAL_SOURCE,
        )
        session.add(memory)
        await session.commit()
        await session.refresh(memory)
        return _to_dict(memory)


async def update_memory(
    memory_id: str,
    data: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    allowed = {
        "title",
        "content",
        "memory_type",
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
        if memory is None or memory.scope_type != DREAMING_SCOPE_TYPE:
            return None
        if user_id is not None and memory.user_id != str(user_id):
            return None

        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "content":
                value = str(value or "").strip()
                if not value:
                    continue
            elif key == "memory_type":
                value = _normalize_memory_type(value)
            elif key == "confidence":
                value = _coerce_confidence(value)
            elif key == "importance":
                value = _coerce_importance(value)
            elif key == "expires_at":
                value = _parse_datetime(value)
            elif key == "status":
                value = "active" if str(value) == "active" else "archived"
            setattr(memory, key, value)

        memory.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(memory)
        return _to_dict(memory)


async def delete_memory(
    memory_id: str,
    user_id: Optional[str] = None,
) -> bool:
    async with await get_db_session() as session:
        stmt = sa_delete(ContextMemory).where(
            ContextMemory.id == _coerce_uuid(memory_id),
            ContextMemory.scope_type == DREAMING_SCOPE_TYPE,
        )
        if user_id is not None:
            stmt = stmt.where(ContextMemory.user_id == str(user_id))
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0


async def toggle_memory(
    memory_id: str,
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    async with await get_db_session() as session:
        memory = await session.get(ContextMemory, _coerce_uuid(memory_id))
        if memory is None or memory.scope_type != DREAMING_SCOPE_TYPE:
            return None
        if user_id is not None and memory.user_id != str(user_id):
            return None

        memory.status = "archived" if memory.status == "active" else "active"
        memory.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(memory)
        return _to_dict(memory)


async def delete_all_memories(user_id: str) -> int:
    async with await get_db_session() as session:
        result = await session.execute(
            sa_delete(ContextMemory).where(
                ContextMemory.user_id == str(user_id),
                ContextMemory.scope_type == DREAMING_SCOPE_TYPE,
            )
        )
        await session.commit()
        return result.rowcount


async def bulk_create_memories(
    user_id: str,
    memories: Iterable[Any],
    source_type: str = DREAMING_AUTO_SOURCE,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Create extracted Dreaming memories with exact-content deduplication."""
    normalized = [item for item in (_normalize_candidate(m) for m in memories) if item]
    if not normalized:
        return []

    async with await get_db_session() as session:
        created: list[ContextMemory] = []
        existing_result = await session.execute(
            select(ContextMemory).where(
                ContextMemory.user_id == str(user_id),
                ContextMemory.scope_type == DREAMING_SCOPE_TYPE,
                ContextMemory.status == "active",
            )
        )
        existing_contents = {
            memory.content
            for memory in existing_result.scalars().all()
            if memory.content
        }
        for item in normalized:
            content = item["content"]
            if content in existing_contents:
                continue

            structured_data = dict(item.get("structured_data") or {})
            if metadata:
                structured_data["source_metadata"] = metadata

            session_id = (metadata or {}).get("session_id")
            memory = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(user_id),
                scope_type=DREAMING_SCOPE_TYPE,
                scope_id=str(user_id),
                memory_type=item.get("memory_type") or DREAMING_DEFAULT_TYPE,
                title=item.get("title"),
                content=content,
                structured_data=structured_data,
                source_type=source_type,
                source_ref=f"conversation_session:{session_id}" if session_id else None,
                confidence=item.get("confidence", 0.7),
                importance=item.get("importance", 5),
                status="active",
                is_pinned=False,
                expires_at=item.get("expires_at"),
            )
            session.add(memory)
            created.append(memory)
            existing_contents.add(content)

        if created:
            await session.commit()
            for memory in created:
                await session.refresh(memory)
            logger.info("[DreamingMemory] %d memories created for user=%s", len(created), user_id)

        return [_to_dict(memory) for memory in created]
