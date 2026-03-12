"""会話メモリ検索ツール"""

from __future__ import annotations

import json
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """メモリ検索ツールを MCP サーバーに登録する。"""

    # 遅延初期化
    _memory_manager = None

    def _get_memory_manager():
        nonlocal _memory_manager
        if _memory_manager is None:
            from src.memory.manager import ConversationMemoryManager
            from src.memory.config import MemoryConfig
            config = MemoryConfig()
            _memory_manager = ConversationMemoryManager(config)
        return _memory_manager

    @mcp.tool()
    async def search_memory(query: str, time_range: str = "all", max_results: int = 10) -> str:
        """過去の会話履歴や記憶から関連する内容を検索する

        Args:
            query: 検索クエリ（例：「前に話した投資の件」「私の好みについて」）
            time_range: 検索対象期間 ("recent", "this_week", "this_month", "all")
            max_results: 最大検索結果数
        """
        max_results = int(max_results) if max_results is not None else 5

        if not query or not query.strip():
            return json.dumps({"success": False, "error": "検索クエリが空です", "results": []}, ensure_ascii=False)

        try:
            memory_manager = _get_memory_manager()
            user_id = "default_user"
            character_name = "ずんだもん"

            results = await memory_manager.search_memory(
                user_id=user_id,
                character_name=character_name,
                query=query,
                time_range=time_range,
                max_results=max_results
            )

            if not results:
                return json.dumps({
                    "success": True,
                    "message": "関連する過去の会話が見つかりませんでした",
                    "results": []
                }, ensure_ascii=False)

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

            return json.dumps({
                "success": True,
                "message": f"{len(results)}件の関連する会話が見つかりました",
                "results": formatted_results
            }, ensure_ascii=False, default=str)

        except Exception as e:
            return json.dumps({
                "success": False,
                "error": f"検索中にエラーが発生しました: {str(e)}",
                "results": []
            }, ensure_ascii=False)
