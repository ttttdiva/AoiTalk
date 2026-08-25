"""Memory search helpers for the past-chat search tool."""

import re
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from src.memory.config import MemoryConfig
from src.memory.manager import ConversationMemoryManager
from src.services.context_memory_service import _keywords


_memory_manager: Optional[ConversationMemoryManager] = None
_current_character_name: ContextVar[Optional[str]] = ContextVar(
    "aoi_current_memory_character_name", default=None
)


def set_current_memory_character_name(character_name: Optional[str]):
    """Bind the session character for memory tools during one generation turn."""
    return _current_character_name.set(character_name or None)


def reset_current_memory_character_name(token) -> None:
    """Restore the previous session character binding."""
    _current_character_name.reset(token)


def get_memory_manager() -> ConversationMemoryManager:
    """Get the global memory manager instance."""
    global _memory_manager

    if _memory_manager is None:
        config = MemoryConfig()
        _memory_manager = ConversationMemoryManager(config)

    return _memory_manager


def _resolve_current_user_id(explicit_user_id: Optional[str]) -> Tuple[str, str]:
    try:
        from src.services.turn_context import get_turn_context

        user_id = get_turn_context().user_id
        if user_id:
            return str(user_id), "turn_context"
    except Exception:
        pass

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
    from src.services.scoped_memory_service import ScopedMemoryService
    from src.services.turn_context import get_turn_context, is_project_context_enabled

    context = get_turn_context()
    scoped_project_id = (
        context.project_id if is_project_context_enabled(context) else None
    )
    memories: list[dict[str, Any]] = []
    try:
        compatibility_rows = await dreaming_memory_service.list_memories(user_id)
    except Exception:
        compatibility_rows = []
    memories.extend(compatibility_rows)
    if scoped_project_id or context.session_id or not compatibility_rows:
        try:
            scoped_rows = await ScopedMemoryService().search(
                actor_id=str(user_id),
                query=query,
                project_id=scoped_project_id,
                session_id=context.session_id,
                limit=max_results,
            )
        except Exception:
            scoped_rows = []
        known_ids = {str(item.get("id")) for item in memories}
        memories.extend(
            item for item in scoped_rows if str(item.get("id")) not in known_ids
        )
    terms = _query_terms(query)

    results: List[Dict[str, Any]] = []
    for memory in memories:
        memory_user_id = memory.get("user_id")
        if (
            memory.get("scope_type") in (None, "global", "user")
            and memory_user_id is not None
            and str(memory_user_id) != str(user_id)
        ):
            continue
        if memory.get("retrieval_score") is not None:
            score = float(memory["retrieval_score"])
        else:
            # Durable facts get a modest trust prior, while still competing in
            # the same numeric score space as conversation fragments.
            score = min(1.0, _score_dreaming_memory(memory, terms) + 0.2)
        if score <= 0:
            continue
        result = {
                "type": "dreaming_memory",
                "id": memory.get("id"),
                "title": memory.get("title"),
                "content": memory.get("content") or "",
                "memory_type": memory.get("memory_type"),
                "relevance_score": round(score, 3),
                "timestamp": memory.get("updated_at") or memory.get("created_at"),
                "source_type": memory.get("source_type"),
            }
        if memory.get("scope_type") is not None:
            result["scope"] = memory.get("scope_type")
        if memory.get("selection_reason") is not None:
            result["selection_reason"] = memory.get("selection_reason")
        results.append(result)
    return results


def _format_conversation_result(result: Dict[str, Any]) -> Dict[str, Any]:
    formatted_result = {
        "type": result["type"],
        "content": result["content"],
        "relevance_score": round(result["relevance_score"], 3),
        "timestamp": result.get("timestamp"),
    }

    # session_id を落とすと read_chat_session でその会話を開けなくなる。
    session_id = result.get("session_id")
    if session_id:
        formatted_result["session_id"] = str(session_id)

    if result["type"] == "archived_summary":
        formatted_result["message_count"] = result.get("message_count", 0)
    elif result["type"] == "active_message":
        formatted_result["role"] = result.get("role")

    return formatted_result


async def semantic_memory_search(
    query: str,
    time_range: str = "all",
    max_results: int = 10,
    user_id: Optional[str] = None,
    character_name: Optional[str] = None,
) -> Dict[str, Any]:
    """過去会話メモリを意味検索する読み取り専用の実装。

    `search_past_chats(mode="semantic")` の実体。dreaming memory と
    conversation memory を横断して関連する断片を返す。各ヒットには元の
    会話の `session_id` が付くので、断片で足りない場合は `read_chat_session`
    に渡して会話本文を開ける。

    Args:
        query: 検索クエリ（探したい話題・人物・決定事項などを簡潔に）。
        time_range: 対象期間。"all"（既定）/"today"/"week"/"month" 等。
        max_results: 返す件数の上限（既定 10）。
    """
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
        resolved_character_name = character_name or _current_character_name.get()
        if not resolved_character_name:
            try:
                from src.tools.keyword.character_manager import get_character_manager

                resolved_character_name = get_character_manager().get_current_character()
            except Exception:
                resolved_character_name = None
        if not resolved_character_name:
            resolved_character_name = "aoi"
        if not character_name:
            try:
                from src.services.character_service import get_character_for_prompt

                character = await get_character_for_prompt(resolved_character_name)
                resolved_character_name = str(
                    (character or {}).get("slug") or resolved_character_name
                ).strip()
            except Exception:
                pass
        # Keep the public source label stable while the implementation behind
        # it is the Scoped Memory adapter.
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
            from src.services.turn_context import (
                get_turn_context,
                is_project_context_enabled,
            )

            context = get_turn_context()
            current_project_id = (
                context.project_id
                if is_project_context_enabled(context)
                else None
            )
            raw_conversation_results = await memory_manager.search_conversation_memory(
                user_id=resolved_user_id,
                character_name=resolved_character_name,
                query=query,
                time_range=time_range,
                max_results=max_results,
                project_id=current_project_id,
            )
            formatted_conversation_results = [
                _format_conversation_result(result)
                for result in raw_conversation_results
            ]
        except Exception as e:
            source_errors["conversation_memory"] = str(e)
            formatted_conversation_results = []
        combined_results = sorted(
            [*dreaming_results, *formatted_conversation_results],
            key=lambda item: (
                float(item.get("relevance_score") or 0.0),
                str(item.get("timestamp") or ""),
            ),
            reverse=True,
        )[:max_results]
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



