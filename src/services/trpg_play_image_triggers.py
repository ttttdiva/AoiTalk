"""TRPG Play 専用の画像生成トリガー判定（Roleplay / Story 挿絵とは別）。"""

from __future__ import annotations

import re
from typing import Any, Mapping

PLAY_IMAGE_TRIGGERS = frozenset(
    {
        "manual",
        "scene_shift",
        "movement",
        "npc_enter",
        "combat",
        "situation",
    }
)

_MOVEMENT_PATTERNS = (
    r"移動",
    r"歩",
    r"走",
    r"進む",
    r"入る",
    r"出る",
    r"登る",
    r"降り",
    r"乗",
    r"向か",
    r"移す",
    r"move",
    r"walk",
    r"run",
    r"enter",
    r"leave",
)
_COMBAT_PATTERNS = (
    r"攻撃",
    r"戦闘",
    r"殴",
    r"斬",
    r"射",
    r"撃",
    r"防御",
    r"回避",
    r"ダメージ",
    r"attack",
    r"fight",
    r"battle",
    r"strike",
    r"combat",
)
_NPC_ENTER_PATTERNS = (
    r"現れ",
    r"姿を見せ",
    r"登場",
    r"声が",
    r"誰か",
    r"appears",
    r"enters",
    r"emerges",
)
_SITUATION_PATTERNS = (
    r"天候",
    r"雨",
    r"雪",
    r"嵐",
    r"暗く",
    r"明る",
    r"増え",
    r"減",
    r"危険",
    r"騒",
    r"静",
    r"雰囲気",
    r"状況",
)
_LOCATION_PATTERNS = (
    r"場所",
    r"部屋",
    r"街",
    r"森",
    r"城",
    r"洞窟",
    r"広場",
    r"駅",
    r"店",
    r"room",
    r"street",
    r"forest",
    r"castle",
    r"cave",
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def snapshot_scene_key(snapshot: Mapping[str, Any] | None) -> str:
    if not isinstance(snapshot, Mapping):
        return ""
    scene = str(snapshot.get("scene") or snapshot.get("location") or "").strip()
    location = str(snapshot.get("location") or "").strip()
    if scene and location and scene != location:
        return f"{scene}|{location}"
    return scene or location


def scene_shift_detected(
    previous_snapshot: Mapping[str, Any] | None,
    current_snapshot: Mapping[str, Any] | None,
    narration_text: str = "",
) -> bool:
    prev_key = snapshot_scene_key(previous_snapshot)
    curr_key = snapshot_scene_key(current_snapshot)
    if prev_key and curr_key and prev_key != curr_key:
        return True
    if _matches_any(narration_text, _LOCATION_PATTERNS):
        if not prev_key:
            return True
        normalized_narration = _normalize_text(narration_text)
        if prev_key.lower() not in normalized_narration:
            return True
    return False


def detect_play_image_trigger(
    *,
    trigger_hint: str | None = None,
    action_text: str = "",
    narration_text: str = "",
    previous_snapshot: Mapping[str, Any] | None = None,
    current_snapshot: Mapping[str, Any] | None = None,
) -> str | None:
    """1 回の判定で最大 1 トリガーを返す。優先度は scene_shift > combat > npc_enter > movement > situation。"""
    hint = str(trigger_hint or "").strip().lower()
    if hint == "manual":
        return "manual"

    combined = f"{action_text}\n{narration_text}".strip()
    if not combined and not scene_shift_detected(previous_snapshot, current_snapshot, narration_text):
        return None

    if scene_shift_detected(previous_snapshot, current_snapshot, narration_text):
        return "scene_shift"
    if _matches_any(combined, _COMBAT_PATTERNS):
        return "combat"
    if _matches_any(combined, _NPC_ENTER_PATTERNS):
        return "npc_enter"
    if _matches_any(combined, _MOVEMENT_PATTERNS):
        return "movement"
    if _matches_any(combined, _SITUATION_PATTERNS):
        return "situation"
    return None


def prompts_are_similar(previous: str | None, current: str) -> bool:
    prev = _normalize_text(previous or "")
    curr = _normalize_text(current)
    if not curr:
        return True
    if not prev:
        return False
    if prev == curr:
        return True
    shorter, longer = (prev, curr) if len(prev) <= len(curr) else (curr, prev)
    return shorter in longer and len(shorter) / max(len(longer), 1) >= 0.85


__all__ = [
    "PLAY_IMAGE_TRIGGERS",
    "detect_play_image_trigger",
    "prompts_are_similar",
    "scene_shift_detected",
    "snapshot_scene_key",
]
