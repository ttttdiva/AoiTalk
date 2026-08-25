"""外部AoiTalkサーバーのタスク/カレンダーを中継するプロキシルート。

登録済み接続プロファイルを使い、リモートAoiTalkの公開API（/api/tasks,
/api/task-occurrences 等）へ読み取り・軽量操作を中継する。取得したリモート
データ本体は永続化しない。書き込み系は事前に capabilities を確認する。

``remote_server_view`` feature が無効な環境（会社版など）では外向き接続を
禁止し 403 を返す。
"""

import inspect
import json
import logging
from urllib.parse import parse_qs
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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

_MAX_SELECTED_DOWNLOAD_PAYLOAD_BYTES = 256 * 1024
_MAX_SELECTED_DOWNLOAD_PATHS = 100
_MAX_SELECTED_DOWNLOAD_PATH_LENGTH = 4096


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

    async def _close_raw_stream(handle: Any) -> None:
        """Close a connector stream and any legacy response/client handles."""

        close = getattr(handle, "aclose", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("remote download stream close failed", exc_info=True)

        # Keep compatibility with early connector test doubles that expose the
        # response and HTTP client separately.  The current connector keeps
        # these resources private and closes them from handle.aclose(); calling
        # an exposed close method again is safe for httpx and protects older
        # test doubles that do not compose their lifecycle methods.
        for resource in (
            getattr(handle, "response", None),
            getattr(handle, "client", None),
        ):
            if resource is None or resource is handle:
                continue
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("remote download resource close failed", exc_info=True)

    def _stream_headers(raw_headers: Any) -> dict[str, str]:
        """Forward safe file headers while dropping hop-by-hop metadata."""

        allowed = {
            "content-type",
            "content-disposition",
            "content-length",
            "accept-ranges",
            "content-range",
            "etag",
            "last-modified",
            "cache-control",
            "expires",
            "x-content-type-options",
        }
        headers: dict[str, str] = {}
        for key, value in (raw_headers or {}).items():
            lowered = str(key).lower()
            if lowered in allowed:
                headers[lowered] = str(value)

        # A decoded response must not advertise byte ranges or an encoded
        # content length.  The connector normally strips these; keep the
        # proxy boundary defensive for test doubles and older connectors.
        content_encoding = ""
        for key, value in (raw_headers or {}).items():
            if str(key).lower() == "content-encoding":
                content_encoding = str(value)
                break
        if content_encoding.strip():
            headers.pop("content-length", None)
            headers.pop("accept-ranges", None)
            headers.pop("content-range", None)
        return headers

    async def _open_remote_download(
        user_id: UUID,
        profile_id: str,
        method: str,
        project_id: str,
        params: Dict[str, Any],
        *,
        json_body: Optional[Dict[str, Any]] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ):
        connector = await _connector_for(user_id, profile_id)
        try:
            open_stream = getattr(connector, "open_raw_stream")
            return await open_stream(
                method,
                f"/api/projects/{project_id}/files/download",
                params=params or None,
                json_body=json_body,
                request_headers=request_headers,
            )
        except RemoteConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except AttributeError as exc:
            raise HTTPException(status_code=503, detail="Connector streaming is not available") from exc

    async def _stream_download_response(handle: Any, *, head_only: bool = False):
        status_code = int(getattr(handle, "status_code", 502) or 502)
        if status_code >= 400:
            await _close_raw_stream(handle)
            detail = "Remote download failed"
            # Preserve upstream client errors so callers can distinguish an
            # inaccessible project/file from a connector failure.
            if 400 <= status_code < 600:
                raise HTTPException(status_code=status_code, detail=detail)
            raise HTTPException(status_code=502, detail=detail)

        headers = _stream_headers(getattr(handle, "headers", None))

        async def body():
            try:
                if head_only:
                    return
                iterator = getattr(handle, "aiter_bytes", None)
                if iterator is None:
                    iterator = getattr(handle, "iter_bytes", None)
                if not callable(iterator):
                    raise RemoteConnectorError("remote stream iterator is not available")
                async for chunk in iterator():
                    if chunk:
                        yield chunk
            finally:
                await _close_raw_stream(handle)

        return StreamingResponse(
            body(),
            status_code=status_code,
            headers=headers,
            media_type=None,
        )

    async def _bounded_request_body(
        request: Request, *, max_bytes: int
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="Download payload is too large")
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)

    async def _selected_download_paths(request: Request) -> list[str]:
        """Read the selected-path contract from JSON or URL-encoded forms."""

        content_length = request.headers.get("content-length")
        try:
            if content_length is not None and int(content_length) > _MAX_SELECTED_DOWNLOAD_PAYLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Download payload is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length") from exc

        content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        payload: Any
        if content_type == "application/x-www-form-urlencoded":
            raw_body = await _bounded_request_body(
                request, max_bytes=_MAX_SELECTED_DOWNLOAD_PAYLOAD_BYTES
            )
            try:
                decoded_body = raw_body.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="Invalid download payload") from exc
            fields = parse_qs(decoded_body, keep_blank_values=True)
            values = fields.get("paths", [])
            if len(values) == 1:
                value = values[0]
                try:
                    payload = {"paths": json.loads(value)} if value.lstrip().startswith("[") else {"paths": values}
                except json.JSONDecodeError:
                    payload = {"paths": values}
            else:
                payload = {"paths": values}
        else:
            try:
                raw_body = await _bounded_request_body(
                    request, max_bytes=_MAX_SELECTED_DOWNLOAD_PAYLOAD_BYTES
                )
                payload = json.loads(raw_body.decode("utf-8", errors="strict"))
            except HTTPException:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid download payload") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="paths must be an array")
        paths = payload.get("paths")
        if isinstance(paths, str):
            try:
                decoded = json.loads(paths)
            except json.JSONDecodeError:
                decoded = [paths]
            paths = decoded
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > _MAX_SELECTED_DOWNLOAD_PATHS
            or any(not isinstance(path, str) for path in paths)
        ):
            raise HTTPException(status_code=400, detail="paths must be a non-empty array")
        cleaned = [path.strip() for path in paths]
        if any(not path or len(path) > _MAX_SELECTED_DOWNLOAD_PATH_LENGTH for path in cleaned):
            raise HTTPException(status_code=400, detail="paths must be a non-empty array")
        return cleaned

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
        handle = await _open_remote_download(
            user_id,
            profile_id,
            request.method,
            project_id,
            params,
            request_headers={
                key: value
                for key in ("range", "if-range", "if-none-match", "if-modified-since")
                if (value := request.headers.get(key))
            },
        )
        return await _stream_download_response(
            handle,
            head_only=False,
        )

    @app.head(
        "/api/remote-servers/{profile_id}/workspace/download",
        include_in_schema=False,
    )
    async def proxy_workspace_download_head(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        if not str(params.get("path") or "").strip():
            raise HTTPException(status_code=400, detail="path is required")
        handle = await _open_remote_download(
            user_id,
            profile_id,
            "HEAD",
            project_id,
            params,
            request_headers={
                key: value
                for key in ("range", "if-range", "if-none-match", "if-modified-since")
                if (value := request.headers.get(key))
            },
        )
        return await _stream_download_response(handle, head_only=True)

    @app.post("/api/remote-servers/{profile_id}/workspace/download")
    async def proxy_workspace_download_selected(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        _ensure_ready()
        user_id = await _require_user_id(request)
        project_id, params = _project_scope(request)
        paths = await _selected_download_paths(request)
        handle = await _open_remote_download(
            user_id,
            profile_id,
            "POST",
            project_id,
            params,
            json_body={"paths": paths},
        )
        return await _stream_download_response(handle)

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
