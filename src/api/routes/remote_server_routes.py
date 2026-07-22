"""外部AoiTalkサーバー接続プロファイルの管理ルート。

接続プロファイル（URL + 暗号化トークン）の CRUD と接続テストを提供する。
``remote_server_view`` feature が無効なプロファイル（会社版など）では外向きの
接続機能自体を無効化し、404 ではなく 403 を返す。
"""

import logging
import ipaddress
import os
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..router_helpers import cookie_auth_dependency
from ..uuid_http import parse_uuid_or_400

try:
    from ...memory.remote_server_repository import RemoteServerRepository
except ImportError:
    RemoteServerRepository = None

try:
    from ...memory.remote_connector import (
        RemoteServerConnector,
        RemoteConnectorError,
    )
except ImportError:
    RemoteServerConnector = None
    RemoteConnectorError = Exception

try:
    from ...features import Features
except ImportError:
    Features = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


class CreateRemoteProfilePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    base_url: str = Field(..., min_length=1, max_length=500)
    auth_token: Optional[str] = Field(default=None, max_length=4096)
    display_color: Optional[str] = Field(default=None, max_length=32)
    enabled: bool = Field(default=True)


class UpdateRemoteProfilePayload(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=500)
    auth_token: Optional[str] = Field(default=None, max_length=4096)
    display_color: Optional[str] = Field(default=None, max_length=32)
    enabled: Optional[bool] = Field(default=None)


def register_remote_server_routes(app: FastAPI, server: "WebChatServer") -> None:
    """外部AoiTalkサーバー接続プロファイルの CRUD・接続テストを登録する。"""
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
        if RemoteServerRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

    def _ensure_feature_enabled() -> None:
        # 会社版など remote_server_view が無効な環境では外向き接続を禁止する。
        if Features is not None and not Features.is_enabled("remote_server_view"):
            raise HTTPException(
                status_code=403,
                detail="Remote server view is disabled on this server",
            )

    def _validate_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise HTTPException(status_code=422, detail="base_url must not contain credentials")
        if os.getenv("AOITALK_ALLOW_PRIVATE_REMOTE_SERVERS", "").strip().lower() not in {"1", "true", "yes", "on"}:
            hostname = parsed.hostname.lower().rstrip(".")
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
                raise HTTPException(status_code=422, detail="private remote server hosts are disabled")
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                address = None
            if address is not None and (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
                or not address.is_global
            ):
                raise HTTPException(status_code=422, detail="private remote server hosts are disabled")
        return normalized

    @app.get("/api/remote-servers")
    async def list_remote_servers(
        request: Request, _: None = Depends(require_auth)
    ):
        """接続プロファイル一覧を返す（トークン平文は含めない）。"""
        _ensure_feature_enabled()
        _ensure_available()
        user_id = await _require_user_id(request)
        session = await server._db_manager.get_session()
        try:
            profiles = await RemoteServerRepository.list_profiles(session, user_id)
            return JSONResponse(
                {"success": True, "profiles": [p.to_dict() for p in profiles]}
            )
        finally:
            await session.close()

    @app.post("/api/remote-servers")
    async def create_remote_server(
        payload: CreateRemoteProfilePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """接続プロファイルを新規作成する。"""
        _ensure_feature_enabled()
        _ensure_available()
        user_id = await _require_user_id(request)
        session = await server._db_manager.get_session()
        try:
            record = await RemoteServerRepository.create_profile(
                session,
                user_id=user_id,
                name=payload.name.strip(),
                base_url=_validate_base_url(payload.base_url),
                auth_token=payload.auth_token,
                display_color=payload.display_color,
                enabled=payload.enabled,
            )
            return JSONResponse({"success": True, "profile": record.to_dict()})
        finally:
            await session.close()

    @app.patch("/api/remote-servers/{profile_id}")
    async def update_remote_server(
        profile_id: str,
        payload: UpdateRemoteProfilePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """接続プロファイルを部分更新する。"""
        _ensure_feature_enabled()
        _ensure_available()
        user_id = await _require_user_id(request)
        profile_uuid = parse_uuid_or_400(profile_id, "profile id")
        updates = payload.model_dump(exclude_unset=True)
        if "base_url" in updates and updates["base_url"] is not None:
            updates["base_url"] = _validate_base_url(updates["base_url"])
        session = await server._db_manager.get_session()
        try:
            record = await RemoteServerRepository.update_profile(
                session, user_id, profile_uuid, updates
            )
            if record is None:
                raise HTTPException(status_code=404, detail="Profile not found")
            return JSONResponse({"success": True, "profile": record.to_dict()})
        finally:
            await session.close()

    @app.delete("/api/remote-servers/{profile_id}")
    async def delete_remote_server(
        profile_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """接続プロファイルを削除する。"""
        _ensure_feature_enabled()
        _ensure_available()
        user_id = await _require_user_id(request)
        profile_uuid = parse_uuid_or_400(profile_id, "profile id")
        session = await server._db_manager.get_session()
        try:
            deleted = await RemoteServerRepository.delete_profile(
                session, user_id, profile_uuid
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="Profile not found")
            return JSONResponse({"success": True})
        finally:
            await session.close()

    @app.post("/api/remote-servers/{profile_id}/test")
    async def test_remote_server(
        profile_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """接続テストを行い、capabilities を取得して結果を保存する。"""
        _ensure_feature_enabled()
        _ensure_available()
        if RemoteServerConnector is None:
            raise HTTPException(status_code=503, detail="Connector is not available")
        user_id = await _require_user_id(request)
        profile_uuid = parse_uuid_or_400(profile_id, "profile id")
        session = await server._db_manager.get_session()
        try:
            record = await RemoteServerRepository.get_profile(
                session, user_id, profile_uuid
            )
            if record is None:
                raise HTTPException(status_code=404, detail="Profile not found")
            connector = RemoteServerConnector(
                base_url=record.base_url,
                auth_token=record.get_auth_token(),
            )
            try:
                capabilities = await connector.test_connection()
            except RemoteConnectorError as exc:
                await RemoteServerRepository.record_check_result(
                    session, user_id, profile_uuid, status="error"
                )
                return JSONResponse(
                    {"success": False, "status": "error", "error": str(exc)},
                    status_code=502,
                )
            await RemoteServerRepository.record_check_result(
                session,
                user_id,
                profile_uuid,
                status="ok",
                capabilities=capabilities,
            )
            return JSONResponse(
                {"success": True, "status": "ok", "capabilities": capabilities}
            )
        finally:
            await session.close()


