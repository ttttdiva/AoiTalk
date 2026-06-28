"""Dreaming-style long-term memory backed by scoped context memories."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete as sa_delete, or_, select

from ..memory.database import get_db_session
from ..memory.models import ContextMemory

logger = logging.getLogger(__name__)

DREAMING_SCOPE_TYPE = "user"
DREAMING_DEFAULT_TYPE = "fact"
DREAMING_MANUAL_SOURCE = "manual"
DREAMING_AUTO_SOURCE = "dreaming_auto"
MIN_AUTO_CONFIDENCE = 0.8
MIN_AUTO_IMPORTANCE = 6

_ALLOWED_TYPES = {
    "fact",
    "preference",
    "constraint",
    "project",
    "workflow",
    "relationship",
    "instruction",
}

_ALLOWED_ACTIONS = {"upsert", "update", "delete", "delete_all"}


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


def _text_contains(haystack: str, needle: str) -> bool:
    haystack_text = str(haystack or "").casefold()
    needle_text = str(needle or "").casefold()
    if not haystack_text or not needle_text:
        return False
    if needle_text in haystack_text:
        return True

    def compact(text: str) -> str:
        return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", text.casefold())

    compact_haystack = compact(haystack_text)
    compact_needle = compact(needle_text)
    return bool(compact_needle and compact_needle in compact_haystack)


def _memory_key(content: str) -> str:
    lowered = content.casefold()
    lowered = re.sub(r"\bthe user\b|\buser\b", "", lowered)
    lowered = re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", lowered)
    return lowered


def _is_similar_memory(left: str, right: str) -> bool:
    left_key = _memory_key(left)
    right_key = _memory_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return SequenceMatcher(None, left_key, right_key).ratio() >= 0.86


def _is_user_scoped_content(content: str) -> bool:
    lowered = content.casefold().strip()
    return (
        lowered.startswith("the user ")
        or lowered.startswith("user ")
        or "ユーザー" in content
        or "依頼者" in content
    )


def _looks_like_delete_all_memory_request(text: str) -> bool:
    normalized = str(text or "").casefold()
    return any(
        term in normalized
        for term in (
            "forget everything",
            "delete all memories",
            "clear all memories",
            "forget all memories",
            "全部忘れ",
            "全て忘れ",
            "すべて忘れ",
            "メモリ全部",
            "記憶全部",
            "全メモリ",
            "すべてのメモリ",
        )
    )


def _normalize_candidate(
    item: Any,
    *,
    user_input: Optional[str] = None,
    source_type: str = DREAMING_AUTO_SOURCE,
) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    action = str(item.get("action") or "upsert").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        action = "upsert"
    if source_type == DREAMING_MANUAL_SOURCE:
        action = "upsert"

    content = str(item.get("content") or "").strip()
    if action in {"upsert", "update"} and not content:
        return None

    memory_type = _normalize_memory_type(item.get("memory_type"))
    confidence = _coerce_confidence(
        item.get("confidence"),
        default=1.0 if action in {"delete", "delete_all"} else 0.75,
    )
    importance = _coerce_importance(
        item.get("importance"),
        default=10 if action in {"delete", "delete_all"} else 5,
    )
    sensitivity = str(item.get("sensitivity") or "normal").strip().lower()
    evidence_span = str(item.get("evidence_span") or "").strip()
    expires_at = _parse_datetime(item.get("expires_at"))
    memory_id = str(item.get("memory_id") or "").strip() or None

    if source_type != DREAMING_MANUAL_SOURCE:
        if confidence < MIN_AUTO_CONFIDENCE or importance < MIN_AUTO_IMPORTANCE:
            return None
        if sensitivity != "normal":
            return None
        if expires_at is not None:
            return None
        if not evidence_span:
            return None
        if user_input is None or not _text_contains(user_input, evidence_span):
            return None
        if action == "delete_all" and not _looks_like_delete_all_memory_request(user_input):
            return None
        if action == "delete" and not (memory_id or content):
            return None
        if action in {"upsert", "update"} and not _is_user_scoped_content(content):
            return None

    structured_data = {
        "reason": item.get("reason"),
        "sensitivity": sensitivity,
        "evidence_span": evidence_span or None,
        "evidence_source": "user_input" if evidence_span else None,
        "operation": action,
    }
    structured_data.update(
        {
            str(key): value
            for key, value in (item.get("structured_data") or {}).items()
            if isinstance(key, str)
        }
    )

    return {
        "action": action,
        "memory_id": memory_id,
        "content": content,
        "memory_type": memory_type,
        "title": str(item.get("title") or "").strip() or None,
        "confidence": confidence,
        "importance": importance,
        "expires_at": expires_at,
        "structured_data": structured_data,
    }


def _to_dict(memory: ContextMemory) -> Dict[str, Any]:
    data = memory.to_dict()
    data["is_active"] = data.get("status") == "active"
    return data


async def list_memories(user_id: str) -> List[Dict[str, Any]]:
    """List user-scoped Dreaming memories."""
    async with await get_db_session() as session:
        stmt = (
            select(ContextMemory)
            .where(ContextMemory.user_id == str(user_id))
            .where(ContextMemory.scope_type == DREAMING_SCOPE_TYPE)
            .where(ContextMemory.status == "active")
            .where(
                or_(
                    ContextMemory.expires_at.is_(None),
                    ContextMemory.expires_at > datetime.utcnow(),
                )
            )
            .order_by(
                ContextMemory.is_pinned.desc(),
                ContextMemory.importance.desc(),
                ContextMemory.updated_at.desc(),
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
    user_input: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Apply extracted Dreaming memory operations after strict validation."""
    normalized = [
        item
        for item in (
            _normalize_candidate(
                m,
                user_input=user_input,
                source_type=source_type,
            )
            for m in memories
        )
        if item
    ]
    if not normalized:
        return []

    async with await get_db_session() as session:
        changed: list[ContextMemory] = []
        existing_result = await session.execute(
            select(ContextMemory).where(
                ContextMemory.user_id == str(user_id),
                ContextMemory.scope_type == DREAMING_SCOPE_TYPE,
                ContextMemory.status == "active",
            )
        )
        existing_memories = list(existing_result.scalars().all())
        for item in normalized:
            action = item.get("action") or "upsert"

            if action == "delete_all":
                for memory in existing_memories:
                    if memory.status == "active":
                        memory.status = "archived"
                        memory.updated_at = datetime.utcnow()
                        changed.append(memory)
                continue

            content = item.get("content") or ""

            structured_data = dict(item.get("structured_data") or {})
            if metadata:
                structured_data["source_metadata"] = metadata

            target = None
            memory_id = item.get("memory_id")
            if memory_id:
                target_uuid = None
                try:
                    target_uuid = _coerce_uuid(memory_id)
                except (TypeError, ValueError):
                    target_uuid = None
                if target_uuid is not None:
                    target = next(
                        (
                            memory
                            for memory in existing_memories
                            if memory.id == target_uuid
                        ),
                        None,
                    )

            similar = target or next(
                (
                    memory
                    for memory in existing_memories
                    if content and _is_similar_memory(memory.content or "", content)
                ),
                None,
            )

            if action == "delete":
                if similar and similar.status == "active":
                    similar.status = "archived"
                    similar.structured_data = {
                        **(similar.structured_data or {}),
                        "deleted_by": DREAMING_AUTO_SOURCE,
                        "delete_metadata": structured_data,
                    }
                    similar.updated_at = datetime.utcnow()
                    changed.append(similar)
                continue

            if similar:
                should_update = (
                    action == "update"
                    or item.get("importance", 0) > similar.importance
                    or item.get("confidence", 0.0) > similar.confidence
                    or len(content) > len(similar.content or "")
                )
                if should_update:
                    similar.content = content
                    similar.memory_type = item.get("memory_type") or similar.memory_type
                    similar.title = item.get("title") or similar.title
                    similar.structured_data = structured_data
                    similar.confidence = max(similar.confidence, item.get("confidence", 0.0))
                    similar.importance = max(similar.importance, item.get("importance", 1))
                    similar.expires_at = item.get("expires_at")
                    similar.updated_at = datetime.utcnow()
                    changed.append(similar)
                continue

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
            changed.append(memory)
            existing_memories.append(memory)

        if changed:
            await session.commit()
            for memory in changed:
                await session.refresh(memory)
            logger.info("[DreamingMemory] %d memories changed for user=%s", len(changed), user_id)

        return [_to_dict(memory) for memory in changed]
