"""Memory search helpers for the past-chat search tool."""

from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from src.memory.config import MemoryConfig
from src.memory.manager import ConversationMemoryManager
from src.memory.search_window import conversation_search_window


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


async def _search_dreaming_memories(
    *,
    user_id: str,
    query: str,
    max_results: int,
) -> List[Dict[str, Any]]:
    from src.services.scoped_memory_service import ScopedMemoryService
    from src.services.turn_context import get_turn_context, is_project_context_enabled

    context = get_turn_context()
    scoped_project_id = (
        context.project_id if is_project_context_enabled(context) else None
    )
    memories = await ScopedMemoryService().search(
        actor_id=str(user_id), query=query, project_id=scoped_project_id,
        session_id=context.session_id, limit=max_results,
    )

    results: List[Dict[str, Any]] = []
    for memory in memories:
        memory_user_id = memory.get("user_id")
        if (
            memory.get("scope_type") in (None, "global", "user")
            and memory_user_id is not None
            and str(memory_user_id) != str(user_id)
        ):
            continue
        score = float(memory.get("retrieval_score") or 0.0)
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
    max_results = max(1, min(int(max_results) if max_results is not None else 5, 100))

    if not query or not query.strip():
        return {
            "success": False,
            "error": "検索クエリが空です",
            "results": [],
            "searched_sources": [],
            "resolved_user_id": None,
            "result_count_by_source": {},
        }

    try:
        window = conversation_search_window(time_range)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "results": [], "searched_sources": []}

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
        searched_sources = (["conversation_memory"] if window.bounded
                            else ["dreaming_memory", "conversation_memory"])
        source_errors: Dict[str, str] = {}

        try:
            dreaming_results = [] if window.bounded else await _search_dreaming_memories(
                user_id=resolved_user_id,
                query=query,
                max_results=max_results,
            )
        except Exception as e:
            source_errors["dreaming_memory"] = "unavailable"
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
                search_window=window,
                raise_on_unavailable=True,
            )
            formatted_conversation_results = [
                _format_conversation_result(result)
                for result in raw_conversation_results
            ]
        except Exception as e:
            from src.memory.search_errors import MemorySearchUnavailable
            source_errors["conversation_memory"] = (
                e.code if isinstance(e, MemorySearchUnavailable) else "unavailable"
            )
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
            payload["search_window"] = window.metadata()
            if window.bounded:
                payload["excluded_sources"] = {"dreaming_memory": "durable_facts_have_no_conversation_event_timestamp"}
            if source_errors:
                payload["source_errors"] = source_errors
                payload["partial"] = True
                payload["success"] = len(source_errors) < len(searched_sources)
                payload["message"] = (
                    "一部の記憶ソースを検索できませんでした" if payload["success"]
                    else "記憶ソースを検索できませんでした"
                )
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
        payload["search_window"] = window.metadata()
        if window.bounded:
            payload["excluded_sources"] = {"dreaming_memory": "durable_facts_have_no_conversation_event_timestamp"}
        if source_errors:
            payload["source_errors"] = source_errors
            payload["partial"] = True
        return payload

    except Exception as e:
        return {
            "success": False,
            "error": "記憶検索を完了できませんでした",
            "results": [],
            "searched_sources": ["dreaming_memory", "conversation_memory"],
            "resolved_user_id": resolved_user_id,
            "resolved_user_id_source": user_id_source,
            "result_count_by_source": {},
        }



