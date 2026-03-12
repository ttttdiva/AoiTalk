"""時刻取得ツール"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """時刻ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def get_current_time() -> str:
        """現在の日時を取得します。ユーザーが現在の時刻や日付を尋ねた場合に使用します。"""
        now = datetime.datetime.now()
        return now.strftime("%Y年%m月%d日 %H時%M分")
