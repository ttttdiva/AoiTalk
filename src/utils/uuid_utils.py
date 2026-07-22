"""UUID パースの共有ユーティリティ。

`_parse_uuid` / `_parse_uuid_strict` の重複定義を 1 箇所に集約する。

- ``parse_uuid``: 寛容型。None・空文字・変換失敗はすべて ``None`` を返す。
- ``parse_uuid_strict``: 変換失敗時に呼び出し元が指定した例外を送出する。

HTTP レイヤ（``HTTPException`` を送出する版）は ``src/api/uuid_http.py`` に分離する。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Optional
from uuid import UUID


def parse_uuid(value: Any) -> Optional[UUID]:
    """値を UUID に変換する。None・空文字・変換失敗はすべて None を返す。

    - すでに ``UUID`` の場合はそのまま返す。
    - falsy（None・空文字など）は None。
    - それ以外は ``uuid.UUID(str(value).strip())`` を試み、失敗時は None。
    """
    if isinstance(value, UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError, AttributeError):
        return None


def parse_uuid_strict(
    value: Any,
    exc_factory: Callable[[Any], BaseException],
) -> UUID:
    """値を UUID に変換する。失敗時は ``exc_factory(value)`` を送出する。

    ``exc_factory`` は元の値を受け取り、送出すべき例外インスタンスを返す
    （例: ``lambda v: CharacterError(f"無効なUUID形式です: {v}")``）。
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise exc_factory(value)
