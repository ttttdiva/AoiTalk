"""Roleplay 画像生成トリガー判定。"""

from __future__ import annotations

import re
from typing import Iterable

_EMOTION_KEYWORDS = frozenset(
    {
        "happy",
        "sad",
        "angry",
        "surprised",
        "fear",
        "disgust",
        "smile",
        "smiling",
        "crying",
        "tears",
        "blush",
        "embarrassed",
        "serious",
        "worried",
        "excited",
        "shy",
        "laughing",
        "frown",
        "pout",
        "neutral",
        "calm",
        "nervous",
        "confident",
        "relaxed",
        "standing",
        "sitting",
        "lying",
        "kneeling",
        "running",
        "walking",
    }
)


def normalize_scene_text(scene: str) -> str:
    """比較用にシーン描写を正規化する。"""
    value = re.sub(r"\s+", " ", str(scene or "").strip().lower())
    return value


def tokenize_scene(scene: str) -> set[str]:
    normalized = normalize_scene_text(scene)
    if not normalized:
        return set()
    return {token for token in re.split(r"[\s,]+", normalized) if token}


def extract_emotion_tokens(scene: str) -> set[str]:
    tokens = tokenize_scene(scene)
    found = tokens & _EMOTION_KEYWORDS
    lowered = normalize_scene_text(scene)
    for keyword in _EMOTION_KEYWORDS:
        if keyword in lowered:
            found.add(keyword)
    return found


def scenes_visually_changed(previous_scene: str | None, current_scene: str) -> bool:
    """背景・構図・状況が視覚的に変わったかを判定する。"""
    current = normalize_scene_text(current_scene)
    if not current:
        return False
    previous = normalize_scene_text(previous_scene or "")
    if not previous:
        return True

    prev_tokens = tokenize_scene(previous)
    curr_tokens = tokenize_scene(current)
    if not prev_tokens or not curr_tokens:
        return previous != current

    union = prev_tokens | curr_tokens
    overlap = len(prev_tokens & curr_tokens) / max(len(union), 1)
    return overlap < 0.7


def emotions_visually_changed(previous_scene: str | None, current_scene: str) -> bool:
    """表情・姿勢・感情の変化をヒューリスティックに判定する。"""
    current = normalize_scene_text(current_scene)
    if not current:
        return False
    previous = normalize_scene_text(previous_scene or "")
    if not previous:
        return True

    prev_emotions = extract_emotion_tokens(previous)
    curr_emotions = extract_emotion_tokens(current)
    if prev_emotions or curr_emotions:
        return prev_emotions != curr_emotions
    return scenes_visually_changed(previous, current)


def should_generate_roleplay_image(
    *,
    trigger: str,
    interval: int,
    scene_description: str,
    previous_scene: str | None,
    turns_since_last_success: int,
) -> bool:
    """サーバ正本のトリガー判定。"""
    scene = str(scene_description or "").strip()
    if not scene:
        return False

    every_n_interval = max(1, int(interval or 1))
    normalized_trigger = str(trigger or "scene_change").strip().lower()

    if normalized_trigger == "every_n":
        if previous_scene is None:
            return True
        return turns_since_last_success >= every_n_interval
    if normalized_trigger == "emotion_change":
        return emotions_visually_changed(previous_scene, scene)
    return scenes_visually_changed(previous_scene, scene)


def coerce_trigger(value: str | None) -> str:
    trigger = str(value or "scene_change").strip().lower()
    if trigger in {"scene_change", "every_n", "emotion_change"}:
        return trigger
    return "scene_change"
