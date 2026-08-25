"""長期APIトークン管理ルート（発行・一覧・失効）。

サーバー間アクセス用の長期トークンをユーザー単位で管理する。平文トークンは
発行レスポンスでのみ返し、以降は接頭辞のみ参照できる。
"""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..router_helpers import cookie_auth_dependency

try:
    from ...memory.api_token_repository import ApiTokenRepository
except ImportError:
    ApiTokenRepository = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


class CreateApiTokenPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


def register_api_token_routes(app: FastAPI, server: "WebChatServer") -> None:
    """長期APIトークンの CRUD ルートを登録する。"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def _require_user_id(request: Request) -> UUID:
        user_info = await server._get_user_info_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            return UUID(user_info["id"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _ensure_available() -> None:
        if ApiTokenRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

    @app.get("/api/auth/long-lived-tokens")
    async def list_long_lived_tokens(
        request: Request, _: None = Depends(require_auth)
    ):
        """ユーザーの長期トークン一覧を返す（平文は含めない）。"""
        _ensure_available()
        user_id = await _require_user_id(request)
        session = await server._db_manager.get_session()
        try:
            tokens = await ApiTokenRepository.list_tokens(session, user_id)
            return JSONResponse(
                {"success": True, "tokens": [t.to_dict() for t in tokens]}
            )
        finally:
            await session.close()

    @app.post("/api/auth/long-lived-tokens")
    async def create_long_lived_token(
        payload: CreateApiTokenPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """長期トークンを発行する。平文トークンはこのレスポンスでのみ返す。"""
        _ensure_available()
        user_id = await _require_user_id(request)
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Unauthorized")
        expires_at = None
        if payload.expires_in_days is not None:
            expires_at = datetime.utcnow() + timedelta(days=payload.expires_in_days)
        session = await server._db_manager.get_session()
        try:
            record, plaintext = await ApiTokenRepository.create_token(
                session,
                user_id=user_id,
                name=payload.name.strip(),
                expires_at=expires_at,
                session_version=int(user_info.get("session_version") or 1),
            )
            data = record.to_dict()
            data["token"] = plaintext  # 発行時のみ返却
            return JSONResponse({"success": True, "token_record": data})
        finally:
            await session.close()

    @app.delete("/api/auth/long-lived-tokens/{token_id}")
    async def revoke_long_lived_token(
        token_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """長期トークンを失効させる。"""
        _ensure_available()
        user_id = await _require_user_id(request)
        try:
            token_uuid = UUID(token_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid token id")
        session = await server._db_manager.get_session()
        try:
            revoked = await ApiTokenRepository.revoke_token(
                session, user_id, token_uuid
            )
            if not revoked:
                raise HTTPException(status_code=404, detail="Token not found")
            return JSONResponse({"success": True})
        finally:
            await session.close()
