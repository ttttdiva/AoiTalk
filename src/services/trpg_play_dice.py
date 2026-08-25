"""TRPG Play 用の安全なダイス式パーサ。"""

from __future__ import annotations

from typing import Any, Dict

from .trpg_coc_system import roll_coc_dice_expression


def roll_play_dice(expression: str) -> Dict[str, Any]:
    """NdN+M 形式の式を評価する。既存 CoC パーサを再利用する。"""

    expr = str(expression or "").strip()
    if not expr:
        raise ValueError("ダイス式が空です")
    result = roll_coc_dice_expression(expr)
    return {
        "expression": expr,
        "total": int(result.get("total") or 0),
        "rolls": result.get("rolls") or [],
        "detail": result,
    }


__all__ = ["roll_play_dice"]
