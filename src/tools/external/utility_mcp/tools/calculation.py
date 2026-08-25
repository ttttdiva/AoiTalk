"""計算ツール"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.tools.basic.calculation_tools import calculate_impl

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """計算ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def calculate(expression: str) -> str:
        """数式を計算します。基本的な四則演算、数学関数、定数が使用できます。

        使用可能な演算子: +, -, *, /, //, %, **, ()
        使用可能な関数: sin, cos, tan, log, log10, sqrt, abs, round, pow, etc.
        使用可能な定数: pi, e

        Args:
            expression: 計算したい数式（例: "2 + 3 * 4", "sin(pi/2)", "sqrt(16)"）
        """
        return calculate_impl(expression)
