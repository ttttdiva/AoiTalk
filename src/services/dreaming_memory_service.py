"""Dreaming-style long-term memory backed by scoped context memories."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete as sa_delete, or_, select, text

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
_TRANSIENT_EXTERNAL_TOPIC_RE = re.compile(
    r"(?:"
    r"weather|forecast|news|headline|price|stock|exchange rate|traffic|score|"
    r"天気|予報|ニュース|速報|価格|株価|為替|交通情報|試合結果"
    r")",
    re.IGNORECASE,
)
_TRANSIENT_TIME_RE = re.compile(
    r"(?:today|tonight|currently|current|latest|now|"
    r"今日|今夜|現在|最新|いま|今の)",
    re.IGNORECASE,
)
_DURABLE_EXTERNAL_CONTEXT_RE = re.compile(
    r"(?:"
    r"project|work(?:s|ing)?\s+(?:on|with)|develop(?:s|ing)?|build(?:s|ing)?|"
    r"プロジェクト|案件|開発して|構築して|仕事で"
    r")",
    re.IGNORECASE,
)
_TRANSIENT_TURN_SCOPE_RE = re.compile(
    r"(?:"
    r"\btoday\b|\bthis time\b|\bfor now\b|\bright now\b|\bon this answer\b|"
    r"今日は|今回(?:だけ|は)?|今だけ|今の(?:返答|回答)|この(?:返答|回答)では|"
    r"ひとまず|とりあえず"
    r")",
    re.IGNORECASE,
)
_DURABLE_USER_SCOPE_RE = re.compile(
    r"(?:"
    r"\balways\b|\bfrom now on\b|\bgoing forward\b|\bacross (?:all )?projects\b|"
    r"いつも|今後(?:は|も)?|これから(?:は|も)?|普段から|どの案件でも|"
    r"すべてのプロジェクトで|全プロジェクトで"
    r")",
    re.IGNORECASE,
)
_PROJECT_SPECIFIC_RE = re.compile(
    r"(?:"
    r"\bthis (?:project|repository|repo|client|incident|case)\b|"
    r"\bthe (?:project|repository|repo|client|incident)\b|"
    r"この(?:案件|プロジェクト|リポジトリ|レポジトリ|顧客|クライアント|障害)|"
    r"当該(?:案件|プロジェクト|障害)|[A-Za-z0-9_-]+案件"
    r")",
    re.IGNORECASE,
)
_PERSONAL_SELF_DISCLOSURE_RE = re.compile(
    r"(?:"
    r"\bi am\b|\bi['’]?m\b|\bmy\b|私は|わたしは|僕は|ぼくは|俺は|"
    r"自分は|私の|わたしの|僕の|俺の|アレルギー|誕生日|出身"
    r")",
    re.IGNORECASE,
)


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
    lowered = re.sub(
        r"\bthe user\b|\buser\b|ユーザー|依頼者",
        "",
        lowered,
    )
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


def _looks_like_transient_external_fact(
    *,
    content: str,
    evidence_span: str,
    memory_type: str,
) -> bool:
    """Reject ephemeral observations that are not durable user memories."""
    if memory_type not in {"fact", "project"}:
        return False
    combined = f"{content}\n{evidence_span}"
    if not _TRANSIENT_EXTERNAL_TOPIC_RE.search(combined):
        return False
    if _DURABLE_EXTERNAL_CONTEXT_RE.search(combined):
        return False
    # Time markers strengthen the signal, but prices/news/weather observations
    # are ephemeral by nature even when the user omits "today" or "latest".
    return True


def _replacement_preserves_enough(old_content: str, new_content: str) -> bool:
    old_text = str(old_content or "").strip()
    new_text = str(new_content or "").strip()
    if not new_text:
        return False
    minimum_chars = max(8, round(len(old_text) * 0.45))
    if len(new_text) < minimum_chars:
        return False
    if not old_text:
        return True
    old_key = _memory_key(old_text)
    new_key = _memory_key(new_text)
    if not old_key or not new_key:
        return False
    similarity_floor = 0.5 if len(old_key) < 24 else 0.25
    return (
        SequenceMatcher(None, old_key, new_key).ratio()
        >= similarity_floor
    )


def _normalize_candidate(
    item: Any,
    *,
    user_input: Optional[str] = None,
    source_type: str = DREAMING_AUTO_SOURCE,
    project_id: Optional[str] = None,
    routed_scope: str = DREAMING_SCOPE_TYPE,
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

    normalized_scope = str(routed_scope or DREAMING_SCOPE_TYPE).strip().casefold()
    if normalized_scope not in {"user", "project"}:
        return None
    if normalized_scope == "project" and not project_id:
        return None

    if source_type != DREAMING_MANUAL_SOURCE:
        # The legacy bulk path remains user-only.  A project candidate is
        # accepted only after the deterministic router explicitly selected the
        # project scope; it is never inferred from memory_type alone.
        if normalized_scope == DREAMING_SCOPE_TYPE and memory_type == "project":
            return None
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
        if (
            normalized_scope == DREAMING_SCOPE_TYPE
            and action in {"upsert", "update"}
            and not _is_user_scoped_content(content)
        ):
            return None
        if action in {"upsert", "update"} and normalized_scope == DREAMING_SCOPE_TYPE:
            combined_scope_text = f"{content}\n{evidence_span}"
            if (
                _TRANSIENT_TURN_SCOPE_RE.search(combined_scope_text)
                and not _DURABLE_USER_SCOPE_RE.search(combined_scope_text)
            ):
                return None
            if _PROJECT_SPECIFIC_RE.search(combined_scope_text):
                return None
            if (
                project_id
                and memory_type in {"workflow", "constraint", "instruction"}
                and not _DURABLE_USER_SCOPE_RE.search(combined_scope_text)
            ):
                return None
            if (
                project_id
                and memory_type == "fact"
                and not _DURABLE_USER_SCOPE_RE.search(combined_scope_text)
                and not _PERSONAL_SELF_DISCLOSURE_RE.search(evidence_span)
            ):
                return None
        if action in {"upsert", "update"} and _looks_like_transient_external_fact(
            content=content,
            evidence_span=evidence_span,
            memory_type=memory_type,
        ):
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
        "scope_intent": str(item.get("scope_intent") or "user").strip().lower(),
        "explicit_evidence": item.get("explicit_evidence") is True,
    }


def _to_dict(memory: ContextMemory) -> Dict[str, Any]:
    data = memory.to_dict()
    data["is_active"] = data.get("status") == "active"
    return data


async def list_memories(user_id: str) -> List[Dict[str, Any]]:
    """List user-scoped Dreaming memories."""
    from .scoped_memory_service import ScopedMemoryService

    return await ScopedMemoryService().list_memories(
        actor_id=str(user_id),
        scope_type=DREAMING_SCOPE_TYPE,
        status="active",
    )


async def get_memory(
    memory_id: str,
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if user_id is None:
        return None
    from .scoped_memory_service import ScopedMemoryError, ScopedMemoryService

    try:
        memory = await ScopedMemoryService().get_memory(memory_id, actor_id=str(user_id))
    except ScopedMemoryError:
        return None
    return memory if memory.get("scope_type") == DREAMING_SCOPE_TYPE else None


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
    from .scoped_memory_service import ScopedMemoryService

    result = await ScopedMemoryService().upsert_memory(
        actor_id=str(user_id),
        content=content,
        scope_type=DREAMING_SCOPE_TYPE,
        scope_id=str(user_id),
        memory_type=_normalize_memory_type(memory_type),
        title=title,
        structured_data=metadata or {},
        source_type=source_type,
        confidence=_coerce_confidence(confidence, default=1.0),
        importance=_coerce_importance(importance, default=7),
        status="active",
        is_pinned=source_type == DREAMING_MANUAL_SOURCE,
    )
    return result["memory"]


async def update_memory(
    memory_id: str,
    data: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if user_id is None:
        return None
    from .scoped_memory_service import ScopedMemoryError, ScopedMemoryService

    service = ScopedMemoryService()
    try:
        current = await service.get_memory(memory_id, actor_id=str(user_id))
        result = await service.update_memory(
            memory_id,
            actor_id=str(user_id),
            changes=data,
            expected_version=int(data.get("version") or current.get("version") or 1),
        )
        return result["memory"]
    except ScopedMemoryError:
        return None


async def delete_memory(
    memory_id: str,
    user_id: Optional[str] = None,
) -> bool:
    if user_id is None:
        return False
    from .scoped_memory_service import ScopedMemoryError, ScopedMemoryService

    try:
        await ScopedMemoryService().forget_memory(memory_id, actor_id=str(user_id))
        return True
    except ScopedMemoryError:
        return False


async def delete_all_memories(user_id: str) -> int:
    from .scoped_memory_service import ScopedMemoryService

    service = ScopedMemoryService()
    rows = await service.list_memories(
        actor_id=str(user_id), scope_type=DREAMING_SCOPE_TYPE
    )
    count = 0
    for row in rows:
        await service.forget_memory(
            row["id"],
            actor_id=str(user_id),
            expected_version=int(row.get("version") or 1),
            reason="explicit_forget_all",
        )
        count += 1
    return count


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
                project_id=str((metadata or {}).get("project_id") or "") or None,
            )
            for m in memories
        )
        if item
    ]
    if not normalized:
        return []
    if any(item.get("action") == "delete_all" for item in normalized):
        # An explicit user request to forget everything is exclusive.  A model
        # must not recreate memories from sibling candidates in the same output.
        normalized = [
            next(item for item in normalized if item.get("action") == "delete_all")
        ]

    from .scoped_memory_service import ScopedMemoryError, ScopedMemoryService

    service = ScopedMemoryService()
    changed: list[dict[str, Any]] = []
    existing = await service.list_memories(
        actor_id=str(user_id),
        scope_type=DREAMING_SCOPE_TYPE,
    )
    for item in normalized:
        action = item.get("action") or "upsert"
        if action == "delete_all":
            for memory in existing:
                try:
                    result = await service.forget_memory(
                        memory["id"],
                        actor_id=str(user_id),
                        expected_version=int(memory.get("version") or 1),
                        reason="dreaming_explicit_forget_all",
                    )
                    changed.append(result.get("memory") or memory)
                except ScopedMemoryError:
                    continue
            break

        content = item.get("content") or ""
        target = next(
            (
                memory
                for memory in existing
                if (
                    item.get("memory_id") == memory.get("id")
                    or (content and _is_similar_memory(memory.get("content") or "", content))
                )
            ),
            None,
        )
        if action == "delete":
            if target:
                result = await service.forget_memory(
                    target["id"],
                    actor_id=str(user_id),
                    expected_version=int(target.get("version") or 1),
                    reason="dreaming_explicit_forget",
                )
                changed.append(result.get("memory") or target)
            continue
        structured_data = dict(item.get("structured_data") or {})
        if metadata:
            structured_data["source_metadata"] = dict(metadata)
        session_id = str((metadata or {}).get("session_id") or "") or None
        result = await service.upsert_memory(
            actor_id=str(user_id),
            content=content,
            scope_type=DREAMING_SCOPE_TYPE,
            scope_id=str(user_id),
            memory_type=item.get("memory_type") or DREAMING_DEFAULT_TYPE,
            title=item.get("title"),
            structured_data=structured_data,
            source_type=source_type,
            source_ref=f"conversation_session:{session_id}" if session_id else None,
            confidence=item.get("confidence", 0.7),
            importance=item.get("importance", 5),
            evidence_refs=[
                {
                    "type": "conversation",
                    "session_id": session_id,
                    "project_id": (metadata or {}).get("project_id"),
                }
            ],
            status="candidate",
            idempotency_key=(
                f"{session_id}:{_memory_key(content)}" if session_id else None
            ),
        )
        changed.append(result["memory"])
        existing.append(result["memory"])

    if changed:
        logger.info("[DreamingMemory] %d memories changed for user=%s", len(changed), user_id)
    return changed
