"""StoryCharacter の aliases / keywords 正規化と破損修復。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


class StoryCharacterFieldError(ValueError):
    """aliases / keywords の入力または永続化形状が不正。"""


def is_corrupted_char_split_array(values: Any) -> bool:
    """JSON 配列文字列が1文字ずつ JSONB 化された破損形状か判定する。"""
    if not isinstance(values, list) or not values:
        return False
    if not all(isinstance(item, str) and len(item) <= 1 for item in values):
        return False
    joined = "".join(values)
    try:
        parsed = json.loads(joined)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)


def repair_char_split_array(values: Any) -> list[str] | None:
    """破損形状を list[str] に復元。対象外なら None。"""
    if not is_corrupted_char_split_array(values):
        return None
    parsed = json.loads("".join(values))
    return [str(item) for item in parsed]


def normalize_string_list_field(values: Any, *, field_name: str, strict: bool = False) -> list[str]:
    """文字列配列フィールドを正規化。破損形状は復元、strict 時は復元不能を拒否。"""
    if values is None:
        return []
    if not isinstance(values, list):
        if strict:
            raise StoryCharacterFieldError(f"{field_name} は配列である必要があります")
        return []

    repaired = repair_char_split_array(values)
    if repaired is not None:
        return repaired

    if not all(isinstance(item, str) for item in values):
        if strict:
            raise StoryCharacterFieldError(f"{field_name} は文字列の配列である必要があります")
        return [str(item) for item in values if item is not None]

    return list(values)


def dedupe_keywords(name: str, aliases: list[str], keywords: list[str]) -> list[str]:
    """name / alias と casefold 完全一致する keywords だけ冗長削除する。"""
    identity = {name.casefold()}
    for alias in aliases:
        identity.add(alias.casefold())
    return [keyword for keyword in keywords if keyword.casefold() not in identity]


def normalize_character_aliases_keywords(
    name: str,
    aliases: Any,
    keywords: Any,
    *,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    normalized_aliases = normalize_string_list_field(
        aliases, field_name="aliases", strict=strict
    )
    normalized_keywords = normalize_string_list_field(
        keywords, field_name="keywords", strict=strict
    )
    return normalized_aliases, dedupe_keywords(name, normalized_aliases, normalized_keywords)


def normalize_character_dict(character: dict[str, Any]) -> dict[str, Any]:
    """GET 応答用に aliases / keywords を読み取り正規化する。"""
    name = str(character.get("name") or "")
    aliases, keywords = normalize_character_aliases_keywords(
        name,
        character.get("aliases"),
        character.get("keywords"),
        strict=False,
    )
    return {**character, "aliases": aliases, "keywords": keywords}


def apply_character_field_normalization(
    name: str,
    aliases: Any,
    keywords: Any,
    *,
    strict: bool = True,
) -> tuple[list[str], list[str]]:
    try:
        return normalize_character_aliases_keywords(
            name, aliases, keywords, strict=strict
        )
    except StoryCharacterFieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = [
    "StoryCharacterFieldError",
    "apply_character_field_normalization",
    "dedupe_keywords",
    "is_corrupted_char_split_array",
    "normalize_character_aliases_keywords",
    "normalize_character_dict",
    "normalize_string_list_field",
    "repair_char_split_array",
]
