"""Memory search tools for function calling."""

import re
from typing import Any, Dict, List, Optional, Tuple

from ..core import tool
from src.memory.config import MemoryConfig
from src.memory.manager import ConversationMemoryManager
from src.services.context_memory_service import _keywords


_memory_manager: Optional[ConversationMemoryManager] = None


def get_memory_manager() -> ConversationMemoryManager:
    """Get the global memory manager instance."""
    global _memory_manager

    if _memory_manager is None:
        config = MemoryConfig()
        _memory_manager = ConversationMemoryManager(config)

    return _memory_manager


def _resolve_current_user_id(explicit_user_id: Optional[str]) -> Tuple[str, str]:
    try:
        from src.tools.os_operations.tools import get_current_user_context

        context = get_current_user_context()
        user_id = context.get("user_id")
        if user_id:
            return str(user_id), "current_user_context"
    except Exception:
        pass

    if explicit_user_id:
        return explicit_user_id, "tool_argument"

    return "default_user", "default"


_SYNONYM_GROUPS = (
    (
        (
            "好き",
            "好み",
            "お気に入り",
            "推し",
            "prefer",
            "preference",
            "favorite",
            "favourite",
            "like",
        ),
        (
            "like",
            "likes",
            "prefer",
            "prefers",
            "preference",
            "favorite",
            "favourite",
        ),
    ),
    (("パズル", "puzzle"), ("puzzle", "puzzles")),
    (("ゲーム", "game"), ("game", "games")),
    (("音楽", "music"), ("music", "song", "songs")),
    (("映画", "movie", "film"), ("movie", "movies", "film", "films")),
    (("作業", "workflow"), ("workflow", "workflows", "work")),
)


def _compact_text(text: str) -> str:
    return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", str(text or "").casefold())


def _query_terms(query: str) -> set[str]:
    terms = set(_keywords(query))
    folded = str(query or "").casefold()
    compacted = _compact_text(folded)

    for triggers, expansions in _SYNONYM_GROUPS:
        if any(
            trigger.casefold() in folded or _compact_text(trigger) in compacted
            for trigger in triggers
        ):
            terms.update(expansion.casefold() for expansion in expansions)

    return {term for term in terms if term}


def _score_dreaming_memory(memory: Dict[str, Any], terms: set[str]) -> float:
    haystack = "\n".join(
        str(memory.get(key) or "")
        for key in ("title", "content", "memory_type")
    ).casefold()
    compacted_haystack = _compact_text(haystack)

    matches = {
        term
        for term in terms
        if term.casefold() in haystack or _compact_text(term) in compacted_haystack
    }
    if not matches:
        return 0.0

    denominator = max(1, min(len(terms), 8))
    keyword_score = min(1.0, len(matches) / denominator)
    importance = max(0, min(int(memory.get("importance") or 0), 10)) / 10
    pinned_bonus = 0.05 if memory.get("is_pinned") else 0.0
    confidence = max(0.0, min(float(memory.get("confidence") or 0.0), 1.0))
    return min(
        1.0,
        0.45
        + keyword_score * 0.35
        + importance * 0.12
        + confidence * 0.03
        + pinned_bonus,
    )


async def _search_dreaming_memories(
    *,
    user_id: str,
    query: str,
    max_results: int,
) -> List[Dict[str, Any]]:
    from src.services import dreaming_memory_service

    terms = _query_terms(query)
    memories = await dreaming_memory_service.list_memories(user_id)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for memory in memories:
        memory_user_id = memory.get("user_id")
        if memory_user_id is not None and str(memory_user_id) != str(user_id):
            continue
        score = _score_dreaming_memory(memory, terms)
        if score <= 0:
            continue
        scored.append((score, memory))

    scored.sort(
        key=lambda item: (
            item[0],
            1 if item[1].get("is_pinned") else 0,
            int(item[1].get("importance") or 0),
            str(item[1].get("updated_at") or item[1].get("created_at") or ""),
        ),
        reverse=True,
    )

    results: List[Dict[str, Any]] = []
    for score, memory in scored[:max_results]:
        results.append(
            {
                "type": "dreaming_memory",
                "id": memory.get("id"),
                "title": memory.get("title"),
                "content": memory.get("content") or "",
                "memory_type": memory.get("memory_type"),
                "relevance_score": round(score, 3),
                "timestamp": memory.get("updated_at") or memory.get("created_at"),
                "source_type": memory.get("source_type"),
            }
        )
    return results


def _format_conversation_result(result: Dict[str, Any]) -> Dict[str, Any]:
    formatted_result = {
        "type": result["type"],
        "content": result["content"],
        "relevance_score": round(result["relevance_score"], 3),
        "timestamp": result.get("timestamp"),
    }

    if result["type"] == "archived_summary":
        formatted_result["message_count"] = result.get("message_count", 0)
    elif result["type"] == "active_message":
        formatted_result["role"] = result.get("role")

    return formatted_result


@tool
async def search_memory(
    query: str,
    time_range: str = "all",
    max_results: int = 10,
    user_id: Optional[str] = None,
    character_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Search relevant past conversation memory."""
    max_results = int(max_results) if max_results is not None else 5

    if not query or not query.strip():
        return {
            "success": False,
            "error": "検索クエリが空です",
            "results": [],
            "searched_sources": [],
            "resolved_user_id": None,
            "result_count_by_source": {},
        }

    resolved_user_id, user_id_source = _resolve_current_user_id(user_id)

    try:
        memory_manager = get_memory_manager()
        resolved_character_name = character_name or "aoi"
        searched_sources = ["dreaming_memory", "conversation_memory"]
        source_errors: Dict[str, str] = {}

        try:
            dreaming_results = await _search_dreaming_memories(
                user_id=resolved_user_id,
                query=query,
                max_results=max_results,
            )
        except Exception as e:
            source_errors["dreaming_memory"] = str(e)
            dreaming_results = []

        try:
            raw_conversation_results = await memory_manager.search_memory(
                user_id=resolved_user_id,
                character_name=resolved_character_name,
                query=query,
                time_range=time_range,
                max_results=max_results,
            )
            formatted_conversation_results = [
                _format_conversation_result(result)
                for result in raw_conversation_results
            ]
        except Exception as e:
            source_errors["conversation_memory"] = str(e)
            formatted_conversation_results = []
        combined_results = [
            *dreaming_results,
            *formatted_conversation_results,
        ][:max_results]
        result_count_by_source = {
            "dreaming_memory": sum(
                1
                for result in combined_results
                if result.get("type") == "dreaming_memory"
            ),
            "conversation_memory": sum(
                1
                for result in combined_results
                if result.get("type") != "dreaming_memory"
            ),
        }

        if not combined_results:
            payload = {
                "success": True,
                "message": "関連する記憶や過去会話は見つかりませんでした",
                "results": [],
                "searched_sources": searched_sources,
                "resolved_user_id": resolved_user_id,
                "resolved_user_id_source": user_id_source,
                "result_count_by_source": result_count_by_source,
            }
            if source_errors:
                payload["source_errors"] = source_errors
            return payload

        payload = {
            "success": True,
            "message": f"{len(combined_results)}件の関連する記憶や過去会話が見つかりました",
            "results": combined_results,
            "searched_sources": searched_sources,
            "resolved_user_id": resolved_user_id,
            "resolved_user_id_source": user_id_source,
            "result_count_by_source": result_count_by_source,
        }
        if source_errors:
            payload["source_errors"] = source_errors
        return payload

    except Exception as e:
        return {
            "success": False,
            "error": f"検索中にエラーが発生しました: {str(e)}",
            "results": [],
            "searched_sources": ["dreaming_memory", "conversation_memory"],
            "resolved_user_id": resolved_user_id,
            "resolved_user_id_source": user_id_source,
            "result_count_by_source": {},
        }


# ToolDefinition created by @tool decorator
