"""Explicit mounted storage API. Legacy explorer namespaces stay unchanged."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from ...services import storage_files as files
from ...services.storage_io import StorageError, StorageUnavailable, storage_io
from ...services.storage_roots import StorageRoot, read_registry, save_root
from ..router_helpers import cookie_auth_dependency
from .storage_stream import storage_download_response


class RootConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr = Field(min_length=1, max_length=120)
    root_path: StrictStr = Field(min_length=1, max_length=4096)
    read_only: StrictBool = False
    enabled: StrictBool = True
    external: StrictBool = True
    shared: StrictBool = False
    project_ids: list[UUID] = Field(default_factory=list, max_length=256)
    user_ids: list[UUID] = Field(default_factory=list, max_length=256)


class SaveRootRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    root: RootConfiguration


class FilePathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr = Field(min_length=1, max_length=4096)


class SaveTextRequest(FilePathRequest):
    content: StrictStr = Field(max_length=files.TEXT_BYTES)
    etag: StrictStr | None = Field(default=None, max_length=256)


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: StrictStr = Field(min_length=1, max_length=4096)
    destination: StrictStr = Field(min_length=1, max_length=4096)


class UploadRequest(FilePathRequest):
    size: StrictInt = Field(ge=0)
    etag: StrictStr | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class Principal:
    id: str
    admin: bool


def storage_http_error(error: StorageError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail())


async def run_storage_operation(root: StorageRoot, operation: Callable, *, write: bool = False):
    def work():
        root.require_online(write=write)
        try:
            return operation()
        except FileNotFoundError as exc:
            root.require_online(write=write)
            raise StorageError("file_not_found", "ファイルが見つかりません", 404) from exc
        except PermissionError as exc:
            root.require_online(write=write)
            raise StorageError("storage_permission_denied", "OS側のファイルアクセス権限がありません", 403) from exc
        except OSError as exc:
            raise StorageUnavailable("ストレージの入出力に失敗しました。接続と空き容量を確認してください") from exc
    try:
        return await storage_io.run(root.io_key, work, mutation=write)
    except StorageError as exc:
        raise storage_http_error(exc) from exc


def register_storage_root_routes(app: FastAPI, server: Any) -> None:
    """Register independent additional roots; no DB volume or global-root mutation."""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def principal(request: Request) -> Principal:
        if getattr(server, "auth_enabled", None) is False:
            return Principal("development", True)
        user = await server._get_user_info_from_request(request)
        try:
            uid = str(UUID(str((user or {}).get("id"))))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(401, "Not authenticated") from exc
        return Principal(uid, bool(await server._is_admin_user(request)))

    async def permitted(root: StorageRoot, actor: Principal, *, write: bool = False) -> bool:
        if actor.admin or root.shared or actor.id in root.user_ids:
            return True
        if not root.project_ids:
            return False
        manager = getattr(server, "_db_manager", None)
        if manager is None:
            raise HTTPException(503, "Project permission service is unavailable")
        from ...memory.project_repository import ProjectRepository
        session = await manager.get_session()
        try:
            for project_id in root.project_ids:
                if await ProjectRepository.has_permission(
                    session, project_id=UUID(project_id), user_id=UUID(actor.id),
                    permission="write" if write else "read",
                ):
                    return True
        finally:
            await session.close()
        return False

    async def load_root(root_id: str, request: Request, *, write: bool = False):
        actor = await principal(request)
        try:
            root = read_registry().get(root_id)
            if not await permitted(root, actor, write=write):
                raise StorageError("storage_not_found", "ストレージが見つかりません", 404)
            # Do not touch the remote filesystem on the event loop.
            if not root.enabled:
                raise StorageUnavailable("ストレージは無効化されています")
            if write and root.read_only:
                raise StorageError("storage_read_only", "このストレージは読み取り専用です", 403)
            return root, actor
        except StorageError as exc:
            raise storage_http_error(exc) from exc

    @app.get("/api/storage/roots", tags=["storage-roots"])
    async def roots(request: Request, _: None = Depends(require_auth)):
        actor = await principal(request)
        result: dict[str, Any] = {
            "success": True, "is_admin": actor.admin,
            "default_storage": {"id": "default", "name": "AoiTalk Storage", "enabled": True,
                                "managed_contexts": ["personal", "project"]},
            "roots": [], "revision": None,
        }
        try:
            snapshot = read_registry()
            result["revision"] = snapshot.revision if actor.admin else None
            for root in snapshot.roots:
                if await permitted(root, actor):
                    value = root.public(admin=actor.admin)
                    value["can_write"] = not root.read_only and await permitted(root, actor, write=True)
                    result["roots"].append(value)
        except StorageError as exc:
            # Never make the existing local Files UI wait for/fail on an extra root.
            result["configuration_error"] = exc.detail()
        return result

    @app.put("/api/storage/roots", tags=["storage-roots"])
    async def put_root(body: SaveRootRequest, request: Request, _: None = Depends(require_auth)):
        actor = await principal(request)
        if not actor.admin:
            raise HTTPException(403, "管理者権限が必要です")
        try:
            snapshot = await storage_io.run("local-storage-configuration", lambda: save_root(
                body.root.model_dump(mode="json"), expected_revision=body.revision), mutation=True)
            return {"success": True, "revision": snapshot.revision,
                    "root": snapshot.get(body.root.id).public(admin=True)}
        except StorageError as exc:
            raise storage_http_error(exc) from exc
        except OSError as exc:
            raise storage_http_error(StorageUnavailable("ローカルのストレージ設定を保存できません")) from exc

    @app.post("/api/storage/roots/{root_id}/enroll", tags=["storage-roots"])
    async def enroll(root_id: str, request: Request, _: None = Depends(require_auth)):
        actor = await principal(request)
        if not actor.admin:
            raise HTTPException(403, "管理者権限が必要です")
        try:
            root = read_registry().get(root_id)
            await storage_io.run(root.io_key, root.enroll, timeout=5, mutation=True)
            return {"success": True, "message": "識別ファイルを確認しました"}
        except StorageError as exc:
            raise storage_http_error(exc) from exc

    @app.get("/api/storage/roots/{root_id}/status", tags=["storage-roots"])
    async def status(root_id: str, request: Request, _: None = Depends(require_auth)):
        # Disabled roots must remain inspectable without probing the mount.
        actor = await principal(request)
        try:
            root = read_registry().get(root_id)
            if not await permitted(root, actor):
                raise StorageError("storage_not_found", "ストレージが見つかりません", 404)
        except StorageError as exc:
            raise storage_http_error(exc) from exc
        value = root.public(admin=actor.admin)
        value["can_write"] = not root.read_only and await permitted(root, actor, write=True)
        if not root.enabled:
            return {**value, "online": False, "status": "disabled"}
        try:
            await storage_io.run(root.io_key, root.require_online, timeout=3)
            return {**value, "online": True, "status": "online"}
        except StorageError as exc:
            return {**value, "online": False, "status": "offline", "error": exc.detail()}

    @app.get("/api/storage/roots/{root_id}/files", tags=["storage-roots"])
    async def list_files(root_id: str, request: Request, path: str = Query("", max_length=4096), _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request)
        return await run_storage_operation(root, lambda: files.list_files(root, path))

    @app.get("/api/storage/roots/{root_id}/text", tags=["storage-roots"])
    async def get_text(root_id: str, request: Request, path: str = Query(..., max_length=4096), _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request)
        return await run_storage_operation(root, lambda: files.read_text(root, path))

    @app.put("/api/storage/roots/{root_id}/text", tags=["storage-roots"])
    async def put_text(root_id: str, body: SaveTextRequest, request: Request, _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.save_text(root, body.path, body.content, body.etag), write=True)

    @app.post("/api/storage/roots/{root_id}/directories", tags=["storage-roots"])
    async def mkdir(root_id: str, body: FilePathRequest, request: Request, _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.mkdir(root, body.path), write=True)

    @app.post("/api/storage/roots/{root_id}/move", tags=["storage-roots"])
    async def move(root_id: str, body: MoveRequest, request: Request, _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.move(root, body.source, body.destination), write=True)

    @app.post("/api/storage/roots/{root_id}/trash", tags=["storage-roots"])
    async def trash(root_id: str, body: FilePathRequest, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.trash(root, body.path, actor.id), write=True)

    @app.post("/api/storage/roots/{root_id}/trash/{trash_id}/restore", tags=["storage-roots"])
    async def restore(root_id: str, trash_id: str, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.restore(root, trash_id, actor.id, actor.admin), write=True)

    @app.post("/api/storage/roots/{root_id}/uploads", tags=["storage-roots"])
    async def upload_start(root_id: str, body: UploadRequest, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.start_upload(root, body.path, body.size, actor.id, body.etag), write=True)

    @app.get("/api/storage/roots/{root_id}/uploads/{upload_id}", tags=["storage-roots"])
    async def upload_status(root_id: str, upload_id: str, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.upload_status(root, upload_id, actor.id))

    @app.put("/api/storage/roots/{root_id}/uploads/{upload_id}", tags=["storage-roots"])
    async def upload_chunk(root_id: str, upload_id: str, request: Request,
                           offset: int = Query(..., ge=0), _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > files.CHUNK_BYTES:
                raise HTTPException(413, "4MiBを超えるchunkは受け付けません")
            content.extend(chunk)
        return await run_storage_operation(root, lambda: files.upload_chunk(root, upload_id, actor.id, offset, bytes(content)), write=True)

    @app.post("/api/storage/roots/{root_id}/uploads/{upload_id}/complete", tags=["storage-roots"])
    async def upload_complete(root_id: str, upload_id: str, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.finish_upload(root, upload_id, actor.id), write=True)

    @app.delete("/api/storage/roots/{root_id}/uploads/{upload_id}", tags=["storage-roots"])
    async def upload_cancel(root_id: str, upload_id: str, request: Request, _: None = Depends(require_auth)):
        root, actor = await load_root(root_id, request, write=True)
        return await run_storage_operation(root, lambda: files.cancel_upload(root, upload_id, actor.id), write=True)

    @app.get("/api/storage/roots/{root_id}/download", tags=["storage-roots"])
    async def download(root_id: str, request: Request,
                       path: str = Query(..., min_length=1, max_length=4096), _: None = Depends(require_auth)):
        root, _actor = await load_root(root_id, request)
        try:
            return await storage_download_response(root, path, request)
        except StorageError as exc:
            raise storage_http_error(exc) from exc
