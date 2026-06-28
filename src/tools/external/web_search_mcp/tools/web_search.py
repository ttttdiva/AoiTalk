"""Web検索ツール（OpenAI Responses API Web Search使用）"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """Web検索ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def web_search(query: str) -> str:
        """Web検索を実行します（OpenAI proxy実装）

        Args:
            query: 検索クエリ
        """
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            return "Web検索を使用するにはOPENAI_API_KEYが必要です。"

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
            response = await client.responses.create(
                model="gpt-4o-mini",
                tools=[{"type": "web_search_preview"}],
                input=(
                    "あなたはWeb検索アシスタントです。"
                    "与えられたクエリについて最新の情報を検索し、"
                    "簡潔で正確な回答を日本語で提供してください。\n\n"
                    f"検索クエリ: {query}"
                ),
            )

            if response and hasattr(response, 'output_text'):
                return response.output_text
            elif response:
                return str(response)
            else:
                return "検索結果を取得できませんでした。"

        except Exception as e:
            return f"Web検索エラー: {str(e)}"
