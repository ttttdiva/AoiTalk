"""ECC ルーター群で共有するヘルパー。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Request

logger = logging.getLogger("src.api.ecc")


def make_get_user_id(app_instance: Any) -> Callable[[Request], Any]:
    """リクエストから user_id を取得する非同期ヘルパーを生成する。

    認証済みの場合は DB のユーザーID、それ以外は default_user を返す。
    """

    async def _get_user_id(request: Request) -> str:
        try:
            user_info = await app_instance._get_user_info_from_request(request)
            if user_info and user_info.get("id"):
                return str(user_info["id"])
        except Exception as e:
            logger.debug("ユーザーID取得失敗（default_userにフォールバック）: %s", e)
        return "default_user"

    return _get_user_id
