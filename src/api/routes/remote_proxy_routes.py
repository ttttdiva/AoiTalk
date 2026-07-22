"""外部AoiTalkサーバーのタスク/カレンダーを中継するプロキシルート。

登録済み接続プロファイルを使い、リモートAoiTalkの公開API（/api/tasks,
/api/task-occurrences 等）へ読み取り・軽量操作を中継する。取得したリモート
データ本体は永続化しない。書き込み系は事前に capabilities を確認する。

``remote_server_view`` feature が無効な環境（会社版など）では外向き接続を
禁止し 403 を返す。
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..router_helpers import cookie_auth_dependency

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


class RemoteTaskPatchPayload(BaseModel):
    """タスクの軽量更新（status / 日付など）。"""

    status: Optional[str] = Field(default=None, max_length=64)
    start_at: Optional[str] = Field(default=None, max_length=64)
    end_at: Optional[str] = Field(default=None, max_length=64)
    priority: Optional[str] = Field(default=None, max_length=32)


class RemoteCommentPayload(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


def register_remote_proxy_routes(app: FastAPI, server: "WebChatServer") -> None:
    """リモートのタスク/カレンダー中継ルートを登録する。"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def _require_user_id(request: Request) -> UUID:
        user_info = await server._get_user_info_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            return UUID(user_info["id"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _ensure_ready() -> None:
        if Features is not None and not Features.is_enabled("remote_server_view"):
            raise HTTPException(
                status_code=403,
                detail="Remote server view is disabled on this server",
            )
        if RemoteServerRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        if RemoteServerConnector is None:
            raise HTTPException(status_code=503, detail="Connector is not available")

    async def _connector_for(user_id: UUID, profile_id: str):
        """プロファイルを読み込み、有効ならコネクタを返す。"""
        try:
            profile_uuid = UUID(profile_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid profile id")
        session = await server._db_manager.get_session()
        try:
            record = await RemoteServerRepository.get_profile(
                session, user_id, profile_uuid
            )
        finally:
            await session.close()
        if record is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        if not record.enabled:
            raise HTTPException(status_code=409, detail="Profile is disabled")
        return RemoteServerConnector(
            base_url=record.base_url, auth_token=record.get_auth_token()
        )

    async def _proxy_get(
        user_id: UUID, profile_id: str, path: str, params: Dict[str, Any]
    ):
        connector = await _connector_for(user_id, profile_id)
        try:
            data = await connector.get(path, params=params or None)
        except RemoteConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return JSONResponse({"success": True, "data": data})

    async def _proxy_write(
        user_id: UUID,
        profile_id: str,
        method: str,
        path: str,
        json_body: Dict[str, Any],
        required_feature: Optional[str] = None,
    ):
        connector = await _connector_for(user_id, profile_id)
        try:
            data = await connector.write(
                method,
                path,
                json_body=json_body,
                required_feature=required_feature,
            )
        except RemoteConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return JSONResponse({"success": True, "data": data})

    @app.get("/api/remote-servers/{profile_id}/tasks")
    async def proxy_list_tasks(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """リモートのタスク一覧を中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, "/api/tasks", dict(request.query_params)
        )

    @app.get("/api/remote-servers/{profile_id}/tasks/{task_id}")
    async def proxy_get_task(
        profile_id: str,
        task_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """リモートのタスク1件を中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, f"/api/tasks/{task_id}", {}
        )

    @app.get("/api/remote-servers/{profile_id}/task-occurrences")
    async def proxy_list_occurrences(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """リモートのタスク発生（カレンダー）一覧を中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id,
            profile_id,
            "/api/task-occurrences",
            dict(request.query_params),
        )

    @app.get("/api/remote-servers/{profile_id}/spaces")
    async def proxy_list_spaces(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """リモートのスペース一覧を中継する（プロジェクト文脈の表示用）。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, "/api/spaces", dict(request.query_params)
        )

    @app.get("/api/remote-servers/{profile_id}/capabilities")
    async def proxy_capabilities(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """接続先のCapabilitiesを明示的に取得する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(user_id, profile_id, "/api/capabilities", {})

    @app.get("/api/remote-servers/{profile_id}/projects")
    async def proxy_list_projects(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """リモート側の認証ユーザーが閲覧可能なProjectだけを中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, "/api/projects", dict(request.query_params)
        )

    @app.get("/api/remote-servers/{profile_id}/reports/time")
    async def proxy_time_report(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        """リモートDBへ直接接続せず、Enterpriseの認証済み集計APIを使う。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, "/api/reports/time", dict(request.query_params)
        )

    @app.get("/api/remote-servers/{profile_id}/time-entries")
    async def proxy_time_entries(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(
            user_id, profile_id, "/api/time-entries", dict(request.query_params)
        )

    def _project_scope(request: Request) -> tuple[str, Dict[str, Any]]:
        project_id = str(request.query_params.get("project_id") or "").strip()
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required")
        try:
            UUID(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid project_id") from exc
        params = dict(request.query_params)
        params.pop("project_id", None)
        return project_id, params

    @app.get("/api/remote-servers/{profile_id}/workspace/files")
    async def proxy_workspace_files(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        return await _proxy_get(
            user_id,
            profile_id,
            f"/api/projects/{project_id}/files",
            params,
        )

    @app.get("/api/remote-servers/{profile_id}/workspace/info")
    async def proxy_workspace_info(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        return await _proxy_get(
            user_id,
            profile_id,
            f"/api/projects/{project_id}/files/info",
            params,
        )

    @app.get("/api/remote-servers/{profile_id}/workspace/preview")
    async def proxy_workspace_preview(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        return await _proxy_get(
            user_id,
            profile_id,
            f"/api/projects/{project_id}/files/preview",
            params,
        )

    @app.get("/api/remote-servers/{profile_id}/workspace/content")
    async def proxy_workspace_content(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        return await _proxy_get(
            user_id,
            profile_id,
            f"/api/projects/{project_id}/files/content",
            params,
        )

    @app.get("/api/remote-servers/{profile_id}/workspace/search")
    async def proxy_workspace_search(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        return await _proxy_get(
            user_id,
            profile_id,
            f"/api/projects/{project_id}/files/search",
            params,
        )

    @app.get("/api/remote-servers/{profile_id}/workspace/download")
    async def proxy_workspace_download(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        if not str(params.get("path") or "").strip():
            raise HTTPException(status_code=400, detail="path is required")
        connector = await _connector_for(user_id, profile_id)
        try:
            raw = await connector.request_raw(
                "GET", f"/api/projects/{project_id}/files/download", params=params
            )
        except RemoteConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        headers = {
            key: value
            for key, value in raw.headers.items()
            if key in {"content-type", "content-disposition", "content-length", "accept-ranges", "content-range"}
        }
        return Response(content=raw.content, status_code=raw.status_code, headers=headers)

    @app.get("/api/remote-servers/{profile_id}/docs/tree")
    async def proxy_docs_tree(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(user_id, profile_id, "/api/docs/tree", dict(request.query_params))

    @app.get("/api/remote-servers/{profile_id}/docs/search")
    async def proxy_docs_search(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(user_id, profile_id, "/api/docs/search", dict(request.query_params))

    @app.get("/api/remote-servers/{profile_id}/docs/nodes/{node_id}")
    async def proxy_doc_node(
        profile_id: str,
        node_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_get(user_id, profile_id, f"/api/docs/nodes/{node_id}", {})

    @app.patch("/api/remote-servers/{profile_id}/tasks/{task_id}")
    async def proxy_patch_task(
        profile_id: str,
        task_id: str,
        payload: RemoteTaskPatchPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """リモートタスクの軽量更新（status/日付）を中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        body = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not body:
            raise HTTPException(status_code=400, detail="No fields to update")
        return await _proxy_write(
            user_id,
            profile_id,
            "PATCH",
            f"/api/tasks/{task_id}",
            body,
            required_feature="remote_task_patch",
        )

    @app.post("/api/remote-servers/{profile_id}/tasks/{task_id}/comments")
    async def proxy_add_comment(
        profile_id: str,
        task_id: str,
        payload: RemoteCommentPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """リモートタスクへコメント追加を中継する。"""
        _ensure_ready()
        user_id = await _require_user_id(request)
        return await _proxy_write(
            user_id,
            profile_id,
            "POST",
            f"/api/tasks/{task_id}/comments",
            {"content": payload.content},
            required_feature="remote_task_comments",
        )
