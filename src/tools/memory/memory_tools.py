"""Memory search tools for function calling."""

from typing import Any, Dict, Optional

from ..core import tool
from src.memory.config import MemoryConfig
from src.memory.manager import ConversationMemoryManager


_memory_manager: Optional[ConversationMemoryManager] = None


def get_memory_manager() -> ConversationMemoryManager:
    """Get the global memory manager instance."""
    global _memory_manager

    if _memory_manager is None:
        config = MemoryConfig()
        _memory_manager = ConversationMemoryManager(config)

    return _memory_manager


def _resolve_current_user_id(explicit_user_id: Optional[str]) -> str:
    try:
        from src.tools.os_operations.tools import get_current_user_context

        context = get_current_user_context()
        user_id = context.get("user_id")
        if user_id:
            return str(user_id)
    except Exception:
        pass

    if explicit_user_id:
        return explicit_user_id

    return "default_user"


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
        }

    try:
        memory_manager = get_memory_manager()
        resolved_user_id = _resolve_current_user_id(user_id)
        resolved_character_name = character_name or "aoi"

        results = await memory_manager.search_memory(
            user_id=resolved_user_id,
            character_name=resolved_character_name,
            query=query,
            time_range=time_range,
            max_results=max_results,
        )

        if not results:
            return {
                "success": True,
                "message": "関連する過去会話は見つかりませんでした",
                "results": [],
            }

        formatted_results = []
        for result in results:
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

            formatted_results.append(formatted_result)

        return {
            "success": True,
            "message": f"{len(results)}件の関連する過去会話が見つかりました",
            "results": formatted_results,
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"検索中にエラーが発生しました: {str(e)}",
            "results": [],
        }


# ToolDefinition created by @tool decorator
