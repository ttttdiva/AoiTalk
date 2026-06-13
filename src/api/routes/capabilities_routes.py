"""Capabilities API。

サーバーの version / profile / feature flags を返す。外部AoiTalkサーバーへ接続する
クライアントは、これを見て機能の出し分けと書き込み可否の判断を行う。会社版は
スナップショットで仕様乖離が起きうるため、クライアントはこの情報を前提に動く。

認証必須。接続テスト時のトークン検証も兼ねる。
"""

import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency

try:
    from ...features import Features
except ImportError:
    Features = None

try:
    from ... import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "unknown"

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_capabilities_routes(app: FastAPI, server: "WebChatServer") -> None:
    """Capabilities ルートを登録する。"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/api/capabilities")
    async def get_capabilities(request: Request, _: None = Depends(require_auth)):
        """サーバーの version / profile / features と認証ユーザーを返す。"""
        profile = os.getenv("AOITALK_PROFILE", "").lower() or "personal"
        features = Features.get_all() if Features is not None else {}

        user_summary = None
        try:
            user_info = await server._get_user_info_from_request(request)
            if user_info:
                user_summary = {
                    "id": user_info.get("id"),
                    "username": user_info.get("username"),
                    "role": user_info.get("role"),
                }
        except Exception as exc:
            logger.warning(f"Failed to resolve user for capabilities: {exc}")

        return JSONResponse(
            {
                "version": APP_VERSION,
                "profile": profile,
                "features": features,
                "server_time": datetime.utcnow().isoformat() + "Z",
                "user": user_summary,
            }
        )
