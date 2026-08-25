"""Memory search MCP tool."""

from __future__ import annotations

import json
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """Register memory search tool on an MCP server."""

    @mcp.tool()
    async def semantic_memory_search(
        query: str,
        time_range: str = "all",
        max_results: int = 10,
        user_id: Optional[str] = None,
        character_name: Optional[str] = None,
    ) -> str:
        """Search relevant past conversation memory."""
        try:
            from src.tools.memory.memory_tools import semantic_memory_search

            result = await semantic_memory_search(
                query=query,
                time_range=time_range,
                max_results=max_results,
                user_id=user_id,
                character_name=character_name,
            )
            return json.dumps(result, ensure_ascii=False, default=str)

        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"検索中にエラーが発生しました: {str(e)}",
                    "results": [],
                    "searched_sources": ["dreaming_memory", "conversation_memory"],
                    "resolved_user_id": user_id,
                    "result_count_by_source": {},
                },
                ensure_ascii=False,
            )
