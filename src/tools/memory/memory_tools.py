"""
Memory search tools for function calling
"""

from typing import Dict, Any, List, Optional
from ..core import tool as function_tool
from src.memory.manager import ConversationMemoryManager
from src.memory.config import MemoryConfig


# Global memory manager
_memory_manager: Optional[ConversationMemoryManager] = None


def get_memory_manager() -> ConversationMemoryManager:
    """Get global memory manager instance"""
    global _memory_manager
    
    if _memory_manager is None:
        config = MemoryConfig()
        _memory_manager = ConversationMemoryManager(config)
    
    return _memory_manager


@function_tool
async def search_memory(query: str, time_range: str = "all", max_results: int = 10) -> Dict[str, Any]:
    """過去の会話履歴や記憶から関連する内容を検索する
    
    Args:
        query: 検索クエリ（例：「前に話した投資の件」「私の好みについて」「何を覚えているか」）
        time_range: 検索対象期間 ("recent", "this_week", "this_month", "all")
        max_results: 最大検索結果数
        
    Returns:
        Dict[str, Any]: 検索結果
    """
    # Convert max_results to int (Gemini may pass float)
    max_results = int(max_results) if max_results is not None else 5
    
    if not query or not query.strip():
        return {
            "success": False,
            "error": "検索クエリが空です",
            "results": []
        }
    
    try:
        memory_manager = get_memory_manager()
        
        # For now, use default user and character
        # In a real implementation, these would come from the current session context
        user_id = "default_user"
        character_name = "ずんだもん"  # Fixed to match actual character name
        
        # Search memory
        results = await memory_manager.search_memory(
            user_id=user_id,
            character_name=character_name,
            query=query,
            time_range=time_range,
            max_results=max_results
        )
        
        if not results:
            return {
                "success": True,
                "message": "関連する過去の会話が見つかりませんでした",
                "results": []
            }
        
        # Format results for display
        formatted_results = []
        for result in results:
            formatted_result = {
                "type": result["type"],
                "content": result["content"],
                "relevance_score": round(result["relevance_score"], 3),
                "timestamp": result.get("timestamp")
            }
            
            if result["type"] == "archived_summary":
                formatted_result["message_count"] = result.get("message_count", 0)
            elif result["type"] == "active_message":
                formatted_result["role"] = result.get("role")
            
            formatted_results.append(formatted_result)
        
        return {
            "success": True,
            "message": f"{len(results)}件の関連する会話が見つかりました",
            "results": formatted_results
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"検索中にエラーが発生しました: {str(e)}",
            "results": []
        }


# FunctionTool created by @function_tool decorator