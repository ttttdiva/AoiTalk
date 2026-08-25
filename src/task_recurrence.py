"""繰り返し予定のスキップモードに関する共通定義。"""

from __future__ import annotations

from typing import Optional


SKIP_MODE_SHIFT_FORWARD = "shift_forward"
# 既存データの読み取り専用互換値。新しい設定としては保存しない。
SKIP_MODE_SHIFT_BACKWARD = "shift_backward"
SKIP_MODE_OMIT = "omit"
VALID_SKIP_MODES = (SKIP_MODE_SHIFT_FORWARD, SKIP_MODE_OMIT)
DEFAULT_SKIP_MODE = SKIP_MODE_SHIFT_FORWARD


def normalize_skip_mode(value: Optional[str]) -> str:
    """skip_mode を新しい二択へ正規化する。"""
    if value == SKIP_MODE_SHIFT_BACKWARD:
        return SKIP_MODE_SHIFT_FORWARD
    if value in VALID_SKIP_MODES:
        return value
    return DEFAULT_SKIP_MODE
