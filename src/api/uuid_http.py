"""HTTP レイヤ向け UUID パースヘルパー。

``task_routes.py`` / ``sync_routes.py`` / ``routes/remote_server_routes.py`` の
``_parse_uuid`` を集約する。変換失敗時は ``HTTPException(400, "Invalid {field_name}")``
を送出する。
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException


def parse_uuid_or_400(value: Optional[str], field_name: str) -> Optional[UUID]:
    """値を UUID に変換する。None・空文字は None、変換失敗は HTTPException(400)。"""
    if value in (None, ""):
        return None
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc
