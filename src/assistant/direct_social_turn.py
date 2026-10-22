"""挨拶だけの発話。ツールも検証モデルも付けずに直接返す。"""

from __future__ import annotations

import re

_TRAILING_PUNCTUATION = "。.!！?？〜~…"

# 完全一致だけを挨拶扱いにする。依頼が混ざった文はエージェント作業へ残す。
_DIRECT_SOCIAL_TURNS = frozenset(
    {
        "こんにちは",
        "こんばんは",
        "おはよう",
        "おはようございます",
        "ありがとう",
        "ありがとうございます",
        "さようなら",
        "おやすみ",
        "おやすみなさい",
        "お疲れ",
        "お疲れ様",
        "hello",
        "hi",
        "hey",
        "good morning",
        "good evening",
        "good night",
        "thanks",
        "thank you",
        "bye",
    }
)


def is_direct_social_turn(text: str | None) -> bool:
    """短い社交発話なら True。作業依頼を含む文は False。"""

    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    compact = compact.strip(_TRAILING_PUNCTUATION).strip()
    if not compact:
        return False
    return compact.casefold() in _DIRECT_SOCIAL_TURNS
