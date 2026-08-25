"""ファイラー・ファイルエクスプローラー系ルート (server.py から移設)"""

import asyncio
import logging
import math
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from ..router_helpers import (
    await_task_completion_before_cancellation,
    cookie_auth_dependency,
)
from ...features import Features

# Import absolute filer path support (server.py と同じフォールバック付き)
try:
    from ...tools.absolute_filer_paths import (
        get_filer_config,
        list_folder_contents,
        get_file_path,
        get_filer_mime_type,
        get_video_thumbnail_path,
    )

    ABSOLUTE_FILER_PATHS_AVAILABLE = True
except ImportError:
    ABSOLUTE_FILER_PATHS_AVAILABLE = False
    get_filer_config = None
    list_folder_contents = None
    get_file_path = None
    get_filer_mime_type = None

# Import file explorer service (server.py と同じフォールバック付き)
try:
    from ...tools.file_explorer import (
        list_directory as explorer_list_directory,
        create_directory as explorer_create_directory,
        upload_file_stream as explorer_upload_file_stream,
        download_file as explorer_download_file,
        download_items as explorer_download_items,
        rename_item as explorer_rename_item,
        move_item as explorer_move_item,
        copy_item as explorer_copy_item,
        archive_items as explorer_archive_items,
        extract_archives as explorer_extract_archives,
        delete_item as explorer_delete_item,
        get_file_info as explorer_get_file_info,
        get_preview as explorer_get_preview,
        get_directory_tree as explorer_get_directory_tree,
        # Editor functions
        save_file as explorer_save_file,
        get_full_content as explorer_get_full_content,
        search_workspace_entries as explorer_search_workspace_entries,
        resolve_file_path as explorer_resolve_file_path,
        get_root_dir as explorer_get_root_dir,
        # Folder thumbnail
        set_folder_thumbnail as explorer_set_folder_thumbnail,
        clear_folder_thumbnail as explorer_clear_folder_thumbnail,
    )
    from ...tools.file_explorer.download_stream import (
        prepare_download_file,
        prepare_download_items,
        remove_temp_download,
    )

    FILE_EXPLORER_AVAILABLE = True
except ImportError:
    FILE_EXPLORER_AVAILABLE = False
    explorer_list_directory = None
    explorer_create_directory = None
    explorer_upload_file_stream = None
    explorer_download_file = None
    explorer_download_items = None
    explorer_rename_item = None
    explorer_move_item = None
    explorer_copy_item = None
    explorer_archive_items = None
    explorer_extract_archives = None
    explorer_delete_item = None
    explorer_get_file_info = None
    explorer_get_preview = None
    explorer_get_directory_tree = None
    explorer_search_workspace_entries = None
    explorer_resolve_file_path = None
    explorer_get_root_dir = None
    prepare_download_file = None
    prepare_download_items = None
    remove_temp_download = None


class _TemporaryFileResponse(FileResponse):
    """Delete a generated download even when response streaming is interrupted."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # FileResponse closes its file before returning or propagating a send error,
            # so synchronous unlink is safe here on Windows as well.
            remove_temp_download(Path(self.path))


def _download_response(download) -> FileResponse:
    response_type = _TemporaryFileResponse if download.temporary else FileResponse
    return response_type(
        path=download.path,
        media_type=download.mime_type,
        filename=download.filename,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _join_explorer_upload_path(base: str, child: str) -> str:
    """Join an upload validation path without changing absolute semantics."""

    base_text = str(base or "")
    child_text = str(child or "").lstrip("/\\")
    if not base_text:
        return child_text
    if not child_text:
        return base_text
    if base_text.endswith(("/", "\\")):
        return f"{base_text}{child_text}"
    return f"{base_text}/{child_text}"


def _is_external_admin_absolute_path(path: str, *, is_admin: bool) -> bool:
    """Return whether an explorer path is an admin-only external target.

    ``authorize_explorer_paths`` intentionally keeps its historical
    ``(paths, is_admin)`` return shape, so the delete endpoint derives this
    small bit of context from the original request value.  External absolute
    targets retain the service's explicit physical-delete semantics; relative
    and workspace-contained paths remain fail-closed and require trash.
    """

    if not is_admin:
        return False
    raw = str(path or "").strip()
    candidate = raw.replace("\\", "/")
    if not (
        Path(raw).is_absolute()
        or candidate.startswith("/")
        or bool(re.match(r"^[A-Za-z]:[\\/]", raw))
    ):
        return False
    try:
        workspace_root = explorer_get_root_dir()
        Path(raw).resolve(strict=False).relative_to(
            Path(workspace_root).resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return True
    return False


async def _prepare_download_in_threadpool(function, *args):
    """Finish and clean a temp ZIP if request cancellation wins the race."""
    worker = asyncio.create_task(run_in_threadpool(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        current_task = asyncio.current_task()
        # ``Task.uncancel`` was added in Python 3.11.  The project still
        # supports 3.10, where catching the cancellation is sufficient to
        # continue draining the shielded worker; use the API only when it is
        # available so repeated client disconnects do not raise AttributeError.
        uncancel = getattr(current_task, "uncancel", None) if current_task is not None else None
        if callable(uncancel):
            uncancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A timeout and server shutdown may cancel the same request more
                # than once. Keep collecting cancellation requests until the
                # disk-backed result can be reclaimed safely.
                if callable(uncancel):
                    uncancel()
        try:
            download = worker.result()
        except Exception:
            download = None
        if download is not None and download.temporary:
            remove_temp_download(download.path)
        raise cancellation

# ゴミ箱（削除の取り消し）用。パッケージ __init__ には出していないので直接取り込む
try:
    from ...tools.file_explorer.file_explorer_service import (
        restore_from_trash as explorer_restore_from_trash,
    )
except ImportError:
    explorer_restore_from_trash = None

# Import storage context (memo 保存先解決用)
try:
    from ...tools.file_explorer.storage_context import ensure_user_storage
except ImportError:
    ensure_user_storage = None

# Import bookmark repository (server.py と同じフォールバック付き)
try:
    from ...memory.file_explorer_bookmark_repository import (
        BOOKMARK_FOLDER_PATH_PREFIX,
        FileExplorerBookmarkRepository,
    )
except ImportError:
    FileExplorerBookmarkRepository = None

try:
    from ...memory.file_explorer_launcher_repository import (
        FileExplorerLauncherRepository,
    )
except ImportError:
    FileExplorerLauncherRepository = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_file_explorer_routes(app: FastAPI, server: "WebChatServer") -> None:
    """filer / explorer / bookmark / editor 系ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    video_thumbnail_slots = asyncio.Semaphore(3)
    image_thumbnail_slots = asyncio.Semaphore(4)
    video_thumbnail_flights: dict[str, asyncio.Task[Path | None]] = {}
    video_thumbnail_flights_lock = asyncio.Lock()

    async def generate_video_thumbnail(path: str) -> Path | None:
        key = os.path.normcase(str(Path(path).resolve(strict=False)))

        async def run_and_release() -> Path | None:
            try:
                async with video_thumbnail_slots:
                    return await run_in_threadpool(get_video_thumbnail_path, path)
            finally:
                current_task = asyncio.current_task()
                async with video_thumbnail_flights_lock:
                    if video_thumbnail_flights.get(key) is current_task:
                        video_thumbnail_flights.pop(key, None)

        async with video_thumbnail_flights_lock:
            task = video_thumbnail_flights.get(key)
            if task is None:
                task = asyncio.create_task(run_and_release())
                video_thumbnail_flights[key] = task
        return await asyncio.shield(task)

    async def require_admin_filer_access(request: Request) -> None:
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="管理者権限が必要です")

    _PROJECT_SEGMENT = re.compile(r"^project_([0-9a-f]{8}-[0-9a-f-]{27})$", re.IGNORECASE)
    _USER_SEGMENT = re.compile(r"^user_([0-9a-f]{8}-[0-9a-f-]{27})$", re.IGNORECASE)

    async def authorize_explorer_paths(
        request: Request,
        paths: list[str],
        *,
        write: bool = False,
    ) -> tuple[list[str], bool]:
        """Authorize generic explorer namespaces before resolving filesystem paths.

        Project workspaces have project-specific APIs which perform the quota
        admission/row-lock protocol.  The legacy generic explorer therefore
        permits project reads only and rejects every project write, including
        administrators, so an alternate path cannot bypass that protocol.
        User storage remains available only to its owner for non-admin users.
        """

        is_admin = await server._is_admin_user(request)
        user_id: UUID | None = None
        auth_disabled = getattr(server, "auth_enabled", None) is False
        if not auth_disabled:
            user_info = await server._get_user_info_from_request(request)
            if not user_info or not user_info.get("id"):
                raise HTTPException(status_code=401, detail="Not authenticated")
            try:
                user_id = UUID(str(user_info["id"]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=401, detail="Not authenticated")

        # Personal development mode historically exposed the workspace to the
        # default_user principal.  Keep that compatibility only when the
        # server explicitly disables authentication; Enterprise always has
        # auth_enabled=True and therefore remains ACL-scoped.
        if auth_disabled:
            is_admin = True
        workspace_root = (
            explorer_get_root_dir() if explorer_get_root_dir is not None else None
        )
        normalized: list[str] = []
        project_checks: list[UUID] = []

        for raw_value in paths:
            raw = str(raw_value or "")
            if raw == "__drives__":
                if not is_admin:
                    raise HTTPException(status_code=403, detail="管理者権限が必要です")
                normalized.append(raw)
                continue

            # The old explorer treated an empty path as the entire workspace.
            # Scope that legacy root to the caller's own user namespace.
            if not raw.strip():
                if is_admin:
                    normalized.append(raw)
                else:
                    normalized.append(f"_users/user_{user_id}")
                continue

            candidate = raw.replace("\\", "/").strip()
            candidate_path = Path(candidate)
            is_absolute = (
                candidate_path.is_absolute()
                or candidate.startswith("/")
                or bool(re.match(r"^[A-Za-z]:[\\/]", candidate))
            )
            absolute_external = False
            if is_absolute:
                if not is_admin:
                    raise HTTPException(status_code=403, detail="絶対パスは許可されません")
                # Admin absolute paths are still subject to the project write
                # rule when they point back into this workspace namespace.
                if workspace_root is not None:
                    try:
                        relative = candidate_path.resolve(strict=False).relative_to(
                            workspace_root.resolve()
                        )
                    except (OSError, ValueError):
                        relative = None
                    if relative is not None:
                        candidate = relative.as_posix()
                    else:
                        absolute_external = True
                else:
                    absolute_external = True

            parts = [part for part in candidate.split("/") if part]
            folded = [part.casefold() for part in parts]
            if any(part in {"..", "."} for part in parts) or any(
                part in {".git", ".trash"} for part in folded
            ):
                raise HTTPException(status_code=403, detail="無効なパスです")

            # Keep absolute external destinations opaque to namespace ACLs,
            # but only after the lexical traversal/hidden-directory checks
            # above.  This keeps the authorization path identical to the
            # filesystem target without dropping a leading slash.
            if absolute_external:
                normalized.append(raw)
                continue

            if folded and folded[0] == "_projects":
                if len(parts) < 2:
                    # 管理者はプロジェクト領域の親コンテナも閲覧できる。
                    # list_directory が返す parent_path をそのまま辿れるようにし、
                    # ここからワークスペース全体・絶対パス閲覧へ進める。
                    if not is_admin or write:
                        raise HTTPException(
                            status_code=403, detail="プロジェクトパスが必要です"
                        )
                else:
                    match = _PROJECT_SEGMENT.fullmatch(parts[1])
                    if match is None:
                        raise HTTPException(
                            status_code=403, detail="無効なプロジェクトパスです"
                        )
                    if write:
                        raise HTTPException(
                            status_code=403,
                            detail="プロジェクトファイルはプロジェクトAPIから操作してください",
                        )
                    if not is_admin:
                        project_checks.append(UUID(match.group(1)))
            elif folded and folded[0] == "_users":
                if len(parts) < 2:
                    # _users も管理者の上位移動で通過する読み取り専用の
                    # 名前空間コンテナ。書き込み先としては認めない。
                    if not is_admin or write:
                        raise HTTPException(
                            status_code=403, detail="ユーザーパスが必要です"
                        )
                else:
                    match = _USER_SEGMENT.fullmatch(parts[1])
                    if match is None:
                        raise HTTPException(
                            status_code=403, detail="無効なユーザーパスです"
                        )
                    if not is_admin and UUID(match.group(1)) != user_id:
                        raise HTTPException(
                            status_code=403,
                            detail="他ユーザーのファイルにはアクセスできません",
                        )
            elif not is_admin:
                raise HTTPException(status_code=403, detail="この名前空間にはアクセスできません")

            normalized.append(candidate if is_absolute else raw)

        if project_checks:
            if getattr(server, "_db_manager", None) is None:
                raise HTTPException(status_code=503, detail="Database is not available")
            from ...memory.project_repository import ProjectRepository

            session = await server._db_manager.get_session()
            try:
                for project_id in dict.fromkeys(project_checks):
                    allowed = await ProjectRepository.has_permission(
                        session,
                        project_id=project_id,
                        user_id=user_id,
                        permission="read",
                    )
                    if not allowed:
                        raise HTTPException(status_code=403, detail="Permission denied")
            finally:
                await session.close()

        return normalized, is_admin

    @app.get("/api/filer/config")
    async def get_absolute_filer_path_config(
        request: Request, _: None = Depends(require_auth)
    ):
        """Get filer path configuration"""
        if not ABSOLUTE_FILER_PATHS_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Absolute filer path support is not available",
            )
        await require_admin_filer_access(request)

        try:
            result = get_filer_config()
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Failed to get filer config: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/filer/browse")
    async def browse_filer_folder(
        request: Request, path: str = "", _: None = Depends(require_auth)
    ):
        """List contents of a directory (both images and videos)"""
        if not ABSOLUTE_FILER_PATHS_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Absolute filer path support is not available",
            )
        await require_admin_filer_access(request)

        try:
            result = list_folder_contents(path)
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to browse folder"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to browse filer folder: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/filer/file")
    async def serve_filer_file(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Serve a file by absolute path"""
        if not ABSOLUTE_FILER_PATHS_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Absolute filer path support is not available",
            )
        await require_admin_filer_access(request)

        file_path = get_file_path(path)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        mime_type = get_filer_mime_type(file_path)
        return FileResponse(
            path=str(file_path),
            media_type=mime_type,
            filename=file_path.name,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/api/filer/video-thumbnail")
    async def serve_video_thumbnail(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Serve a video thumbnail (generated via FFmpeg)"""
        if not ABSOLUTE_FILER_PATHS_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="Absolute filer path support is not available",
            )
        await require_admin_filer_access(request)

        thumbnail_path = await generate_video_thumbnail(path)
        if thumbnail_path is None:
            raise HTTPException(
                status_code=404, detail="Video thumbnail not available"
            )

        return FileResponse(
            path=str(thumbnail_path),
            media_type="image/jpeg",
            filename=thumbnail_path.name,
        )

    # ── File Explorer API Endpoints ────────────────────────────────────
    # Replaces old user-files API with full file explorer functionality

    class ExplorerMkdirPayload(BaseModel):
        path: str = ""  # Parent directory path
        name: str  # New directory name

    class ExplorerRenamePayload(BaseModel):
        path: str
        new_name: str

    class ExplorerMovePayload(BaseModel):
        src: str
        dest: str

    class ExplorerCopyPayload(BaseModel):
        src: str
        dest: str

    class ExplorerArchivePayload(BaseModel):
        paths: list[str]
        dest: str = ""

    class ExplorerDownloadPayload(BaseModel):
        paths: list[StrictStr] = Field(min_length=1, max_length=100)

        @field_validator("paths")
        @classmethod
        def validate_path_lengths(cls, paths: list[str]) -> list[str]:
            if any(len(path) > 4096 for path in paths):
                raise ValueError("path is too long")
            return paths

    @app.get("/api/explorer/tree")
    async def get_explorer_tree(
        request: Request, root: str = "", _: None = Depends(require_auth)
    ):
        """Get directory tree structure"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [root], _ = await authorize_explorer_paths(request, [root])
            result = explorer_get_directory_tree(root_path=root)
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get directory tree: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Memo API ─────────────────────────────────────────────────────

    @app.get("/api/explorer/memo")
    async def get_memo(request: Request, _: None = Depends(require_auth)):
        """ユーザーの memo.md を読み込む"""
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            from uuid import UUID as _UUID

            user_id = _UUID(user_info["id"])
            user_dir = ensure_user_storage(user_id)
            memo_path = user_dir / "memo.md"
            content = (
                memo_path.read_text(encoding="utf-8") if memo_path.exists() else ""
            )
            return JSONResponse({"success": True, "content": content})
        except Exception as e:
            logger.error(f"Failed to read memo: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.put("/api/explorer/memo")
    async def save_memo(request: Request, _: None = Depends(require_auth)):
        """ユーザーの memo.md を保存する"""
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            from uuid import UUID as _UUID

            body = await request.json()
            content = body.get("content", "")
            user_id = _UUID(user_info["id"])
            user_dir = ensure_user_storage(user_id)
            memo_path = user_dir / "memo.md"
            memo_path.write_text(content, encoding="utf-8")
            return JSONResponse({"success": True})
        except Exception as e:
            logger.error(f"Failed to save memo: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/explorer/list")
    async def explorer_list(
        request: Request, path: str = "", _: None = Depends(require_auth)
    ):
        """List directory contents"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            # Check if user is admin to allow browsing outside user_files
            [path], is_admin = await authorize_explorer_paths(request, [path])

            result = explorer_list_directory(path, is_admin=is_admin)
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to list directory"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list directory: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/mkdir")
    async def explorer_mkdir(
        payload: ExplorerMkdirPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Create a new directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [authorized_path], is_admin = await authorize_explorer_paths(
                request, [payload.path], write=True
            )
            result = explorer_create_directory(
                authorized_path, payload.name, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to create directory"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to create directory: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/upload")
    async def explorer_upload(
        request: Request,
        file: UploadFile = File(...),
        relative_path: str = Form(""),
        path: str = "",
        _: None = Depends(require_auth),
    ):
        """Upload a file to the specified directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [authorized_path], is_admin = await authorize_explorer_paths(
                request, [path], write=True
            )
            # Validate the browser-supplied relative filename as part of the
            # same namespace decision, but keep the service's directory
            # argument separate from the filename.
            upload_target = _join_explorer_upload_path(
                authorized_path,
                relative_path,
            )
            await authorize_explorer_paths(request, [upload_target], write=True)
            filename = relative_path or file.filename or "unnamed_file"
            await file.seek(0)
            result = await await_task_completion_before_cancellation(
                asyncio.to_thread(
                    explorer_upload_file_stream,
                    authorized_path,
                    filename,
                    file.file,
                    is_admin,
                    allow_external=is_admin,
                )
            )

            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to upload file"),
                )

            logger.info(
                f"Uploaded file: {filename} to {path or 'root'} ({result.get('size_bytes', 0)} bytes)"
            )
            return JSONResponse(result)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to upload file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/explorer/download")
    async def explorer_download(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Download a file or folder"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        [path], is_admin = await authorize_explorer_paths(request, [path])
        download = await _prepare_download_in_threadpool(
            prepare_download_file, path, is_admin
        )
        if download is None:
            raise HTTPException(status_code=404, detail="File not found")

        return _download_response(download)

    @app.post("/api/explorer/download")
    async def explorer_download_selected(
        payload: ExplorerDownloadPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Download selected files or folders as a single payload"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        if not payload.paths:
            raise HTTPException(status_code=400, detail="No files selected")

        paths, is_admin = await authorize_explorer_paths(request, payload.paths)
        download = await _prepare_download_in_threadpool(
            prepare_download_items, paths, is_admin
        )
        if download is None:
            raise HTTPException(status_code=404, detail="File not found")

        return _download_response(download)

    @app.get("/api/explorer/serve")
    async def explorer_serve(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Serve a workspace file inline (for thumbnails / image display).

        .lnk shortcuts are transparently resolved by resolve_file_path.
        """
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        [path], is_admin = await authorize_explorer_paths(request, [path])
        file_path = explorer_resolve_file_path(path, is_admin=is_admin)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        mime_type = (
            get_filer_mime_type(file_path)
            if ABSOLUTE_FILER_PATHS_AVAILABLE
            else "application/octet-stream"
        )
        return FileResponse(
            path=str(file_path),
            media_type=mime_type,
            filename=file_path.name,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/api/explorer/video-thumbnail")
    async def explorer_video_thumbnail(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Serve a video thumbnail for a workspace-relative path (.lnk supported)."""
        if not FILE_EXPLORER_AVAILABLE or not ABSOLUTE_FILER_PATHS_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail="File explorer or absolute filer path support is not available",
            )

        [path], is_admin = await authorize_explorer_paths(request, [path])
        file_path = explorer_resolve_file_path(path, is_admin=is_admin)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        thumbnail_path = await generate_video_thumbnail(str(file_path))
        if thumbnail_path is None:
            raise HTTPException(
                status_code=404, detail="Video thumbnail not available"
            )

        return FileResponse(
            path=str(thumbnail_path),
            media_type="image/jpeg",
            filename=thumbnail_path.name,
        )

    def _build_image_thumbnail(file_path: Path, size: int) -> Response:
        """画像を指定サイズで縮小したサムネイルを返す。
        Pillow 未導入時は原本を FileResponse で返却する。"""
        size = max(32, min(1024, size))
        try:
            from PIL import Image, ImageOps  # type: ignore
            import io as _io

            with Image.open(file_path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                im.thumbnail((size, size), Image.LANCZOS)
                buf = _io.BytesIO()
                # RGBA は PNG、それ以外は JPEG
                fmt = "PNG" if im.mode == "RGBA" else "JPEG"
                save_kwargs: dict = {}
                if fmt == "JPEG":
                    save_kwargs = {"quality": 82, "optimize": True}
                im.save(buf, format=fmt, **save_kwargs)
                data = buf.getvalue()
                mt = "image/png" if fmt == "PNG" else "image/jpeg"
                return Response(
                    content=data,
                    media_type=mt,
                    headers={"Cache-Control": "public, max-age=3600"},
                )
        except Exception as e:
            logger.warning(
                f"image thumbnail generation failed for {file_path}: {e}"
            )
            mime_type = (
                get_filer_mime_type(file_path)
                if ABSOLUTE_FILER_PATHS_AVAILABLE
                else "application/octet-stream"
            )
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                filename=file_path.name,
            )

    async def _serve_image_thumbnail(file_path: Path, size: int) -> Response:
        # Pillowのdecode/resize/encodeは同期CPU処理なのでASGI event loop外で実行する。
        async with image_thumbnail_slots:
            return await run_in_threadpool(_build_image_thumbnail, file_path, size)

    @app.get("/api/explorer/image-thumbnail")
    async def explorer_image_thumbnail(
        request: Request,
        path: str,
        size: int = 320,
        _: None = Depends(require_auth),
    ):
        """ワークスペース相対パスから画像サムネイルを生成して返す。"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        [path], is_admin = await authorize_explorer_paths(request, [path])
        file_path = explorer_resolve_file_path(path, is_admin=is_admin)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")
        return await _serve_image_thumbnail(file_path, size)

    @app.get("/api/filer/image-thumbnail")
    async def filer_image_thumbnail(
        request: Request,
        path: str,
        size: int = 320,
        _: None = Depends(require_auth),
    ):
        """絶対パスから画像サムネイルを生成して返す（管理者向け絶対パス閲覧対応）。"""
        await require_admin_filer_access(request)
        try:
            file_path = Path(path)
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            return await _serve_image_thumbnail(file_path, size)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"filer image thumbnail failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    class FolderThumbnailPayload(BaseModel):
        folder_path: str
        target_path: str

    @app.post("/api/explorer/folder-thumbnail")
    async def explorer_set_folder_thumb(
        payload: FolderThumbnailPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """フォルダに代表サムネを明示設定する（`.folder-thumb` 書き込み）。"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        paths, is_admin = await authorize_explorer_paths(
            request, [payload.folder_path, payload.target_path], write=True
        )
        result = explorer_set_folder_thumbnail(
            paths[0], paths[1], is_admin=is_admin
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "failed")
            )
        return JSONResponse(result)

    @app.delete("/api/explorer/folder-thumbnail")
    async def explorer_clear_folder_thumb(
        folder_path: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """フォルダの明示サムネ設定を解除する（`.folder-thumb` 削除）。"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        [folder_path], is_admin = await authorize_explorer_paths(
            request, [folder_path], write=True
        )
        result = explorer_clear_folder_thumbnail(folder_path, is_admin=is_admin)
        if not result.get("success"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "failed")
            )
        return JSONResponse(result)

    @app.get("/api/explorer/info")
    async def explorer_info(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Get file/directory info"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [path], is_admin = await authorize_explorer_paths(request, [path])
            result = explorer_get_file_info(path, is_admin=is_admin)
            if not result.get("success"):
                raise HTTPException(
                    status_code=404, detail=result.get("error", "Not found")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get file info: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/explorer/preview")
    async def explorer_preview(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Get file preview (text content, image data, etc.)"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [path], is_admin = await authorize_explorer_paths(request, [path])
            result = explorer_get_preview(path, is_admin=is_admin)
            if not result.get("success"):
                raise HTTPException(
                    status_code=404, detail=result.get("error", "Not found")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get preview: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/rename")
    async def explorer_rename(
        payload: ExplorerRenamePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Rename a file or directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [authorized_path], is_admin = await authorize_explorer_paths(
                request, [payload.path], write=True
            )
            result = explorer_rename_item(
                authorized_path, payload.new_name, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to rename")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to rename: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/move")
    async def explorer_move(
        payload: ExplorerMovePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Move a file or directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [src], is_admin = await authorize_explorer_paths(
                request, [payload.src], write=True
            )
            [dest], _ = await authorize_explorer_paths(
                request, [payload.dest], write=True
            )
            result = explorer_move_item(
                src, dest, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to move")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to move: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/copy")
    async def explorer_copy(
        payload: ExplorerCopyPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Copy a file or directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [src], is_admin = await authorize_explorer_paths(request, [payload.src])
            [dest], _ = await authorize_explorer_paths(
                request, [payload.dest], write=True
            )
            result = explorer_copy_item(
                src, dest, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to copy")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to copy: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/archive")
    async def explorer_archive(
        payload: ExplorerArchivePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Create a zip archive from selected files or directories"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            paths, is_admin = await authorize_explorer_paths(request, payload.paths)
            [dest], _ = await authorize_explorer_paths(
                request, [payload.dest], write=True
            )
            result = explorer_archive_items(
                paths, dest, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to archive")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to archive: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/extract")
    async def explorer_extract(
        payload: ExplorerArchivePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Extract selected zip archives"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            paths, is_admin = await authorize_explorer_paths(request, payload.paths)
            [dest], _ = await authorize_explorer_paths(
                request, [payload.dest], write=True
            )
            result = explorer_extract_archives(
                paths, dest, is_admin=is_admin
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to extract")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to extract: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/explorer/delete")
    async def explorer_delete(
        request: Request, path: str, _: None = Depends(require_auth)
    ):
        """Delete a file or directory"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            [path], is_admin = await authorize_explorer_paths(
                request, [path], write=True
            )
            # First-party workspace deletes are reversible by contract.  The
            # service's fail-soft default remains available to explicit
            # external/admin absolute paths, but this route must not fall back
            # to an irreversible physical delete when trashing fails.
            result = explorer_delete_item(
                path,
                is_admin=is_admin,
                require_trash=not _is_external_admin_absolute_path(
                    path,
                    is_admin=is_admin,
                ),
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Failed to delete")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    class ExplorerRestorePayload(BaseModel):
        token: str

    @app.post("/api/explorer/restore")
    async def explorer_restore(
        payload: ExplorerRestorePayload,
        _: None = Depends(require_auth),
    ):
        """ゴミ箱に退避したファイル/ディレクトリを元の場所へ復元する"""
        # The token-only legacy endpoint has no project ACL or row-lock/quota
        # context.  Disable it in Enterprise so a project deletion can never
        # be restored outside the project API's accounting boundary.
        if Features.is_enterprise():
            raise HTTPException(
                status_code=403,
                detail="Enterpriseでは汎用ゴミ箱復元を無効化しています。プロジェクトAPIを使用してください",
            )
        if not FILE_EXPLORER_AVAILABLE or explorer_restore_from_trash is None:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            result = explorer_restore_from_trash(payload.token)
            if not result.get("success"):
                status_code = 404 if result.get("code") == "not_found" else 400
                raise HTTPException(
                    status_code=status_code,
                    detail=result.get("error", "Failed to restore"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to restore: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── File Explorer Bookmark Endpoints ────────────────────────────────

    async def get_bookmark_user_id(request: Request):
        user_info = await server._get_user_info_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        from uuid import UUID as _UUID

        try:
            return _UUID(user_info["id"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Unauthorized")

    async def _resolve_sidebar_scope(
        request: Request,
        session,
        *,
        scope: str | None,
        space_id: UUID | None,
        write: bool = False,
    ) -> tuple[str, UUID | None, UUID, dict]:
        """Resolve and authorize the Files sidebar collection scope.

        ``scope`` was added after the original user-scoped API.  Requests from
        older clients which omit it deliberately retain the personal behavior;
        a supplied ``space_id`` without ``scope=shared`` is rejected rather than
        silently changing the ownership boundary.
        """

        user_id = await get_bookmark_user_id(request)
        user_info = await server._get_user_info_from_request(request) or {}
        normalized_scope = str(scope or "personal").strip().lower()
        if normalized_scope not in {"personal", "shared"}:
            raise HTTPException(
                status_code=422,
                detail="scopeは personal または shared を指定してください",
            )
        if normalized_scope == "personal":
            if space_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="personal scopeではspace_idを指定できません",
                )
            return normalized_scope, None, user_id, user_info
        # The legacy collection URL is intentionally personal-only.  Shared
        # collections use the canonical /api/spaces/{space_id}/explorer/* URL
        # below, so an item UUID from a Space can never be reached by merely
        # adding query parameters to the old endpoint.
        if request.url.path.startswith("/api/explorer/"):
            raise HTTPException(
                status_code=404,
                detail="共有サイドバーはSpaceスコープのAPIを使用してください",
            )
        if space_id is None:
            raise HTTPException(
                status_code=422,
                detail="shared scopeにはspace_idが必要です",
            )

        # Keep the Files routes on the same policy used by Task/Space APIs.
        # Import lazily so legacy installations which do not yet have the
        # extracted service can still use personal bookmarks.
        try:
            from ...services.space_access import (
                can_write_space,
                get_readable_space,
                load_space,
            )
        except ImportError as exc:  # pragma: no cover - compatibility fallback
            raise HTTPException(
                status_code=503,
                detail="Space access policy is not available",
            ) from exc

        if write:
            allowed, _space = await can_write_space(
                session,
                space_id=str(space_id),
                user_id=user_id,
                user_info=user_info,
            )
            if _space is None:
                raise HTTPException(status_code=404, detail="Spaceが見つかりません")
        else:
            _space = await get_readable_space(
                session,
                space_id=str(space_id),
                user_id=user_id,
                user_info=user_info,
            )
            allowed = _space is not None
            if _space is None:
                # The read helper intentionally returns None for both a
                # missing Space and an unreadable Space.  Match spaces_routes
                # and conceal both cases behind 404; a readable collection is
                # the only situation in which its existence is disclosed.
                loaded_space = await load_space(session, str(space_id))
                if loaded_space is None or not allowed:
                    raise HTTPException(status_code=404, detail="Spaceが見つかりません")
        if not allowed:
            # Do not disclose whether another Space or its item exists.
            raise HTTPException(status_code=403, detail="Spaceへのアクセス権がありません")
        return normalized_scope, space_id, user_id, user_info

    def _sidebar_item_to_dict(item, *, space_id: UUID | None) -> dict:
        """Serialize a sidebar item while exposing its effective Space."""

        data = dict(item.to_dict())
        effective_space_id = getattr(item, "space_id", None) or space_id
        data["space_id"] = (
            str(effective_space_id) if effective_space_id is not None else None
        )
        return data

    _RECORD_TABLE_PATH = re.compile(
        r"^aoitalk-record-table:"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        re.IGNORECASE,
    )

    def _is_hf_virtual_folder(path: str) -> bool:
        """Recognize the HF virtual folder syntax without touching the network."""

        if path == "HF":
            return True
        if not path.startswith("HF|"):
            return False
        parts = path.split("|")
        # HF|account|model|repo[|sub-path] (repo ids may contain '/').
        return (
            len(parts) >= 4
            and bool(parts[1])
            and parts[2] in {"model", "dataset"}
            and bool(parts[3])
            and not any(part in {".", ".."} for part in parts[4:])
        )

    async def _validate_record_table_launcher_path(
        request: Request,
        path: str,
        *,
        shared_space_id: UUID | None = None,
    ) -> str:
        """Authorize and verify a virtual project record-table file.

        Record-table entries are rendered as Files ``files`` even though they
        have no filesystem inode.  Validate the table/project row and the
        project read ACL instead of using ``os.path.isfile``.
        """

        match = _RECORD_TABLE_PATH.fullmatch(path)
        if match is None:
            raise HTTPException(
                status_code=400,
                detail="ランチャー対象の仮想ファイルパスが不正です",
            )
        if server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

        project_id = UUID(match.group(1))
        table_id = UUID(match.group(2))
        user_id = await get_bookmark_user_id(request)
        auth_disabled = getattr(server, "auth_enabled", None) is False
        is_admin = auth_disabled or await server._is_admin_user(request)

        from sqlalchemy import select

        from ...memory.models import Project, RecordTable
        from ...memory.project_repository import ProjectRepository

        session = await server._db_manager.get_session()
        try:
            result = await session.execute(
                select(RecordTable).where(
                    RecordTable.id == table_id,
                    RecordTable.project_id == project_id,
                    RecordTable.deleted_at.is_(None),
                )
            )
            table = result.scalar_one_or_none()
            if table is None:
                raise HTTPException(status_code=404, detail="対象が見つかりません")
            if shared_space_id is not None:
                project_result = await session.execute(
                    select(Project).where(
                        Project.id == project_id,
                        Project.space_id == shared_space_id,
                        Project.deleted_at.is_(None),
                    )
                )
                if project_result.scalar_one_or_none() is None:
                    raise HTTPException(
                        status_code=403,
                        detail="共有Space外のレコードテーブルは登録できません",
                    )
            if not is_admin:
                allowed = await ProjectRepository.has_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="read",
                )
                if not allowed:
                    raise HTTPException(status_code=403, detail="Permission denied")
        finally:
            await session.close()
        return path

    async def _validate_sidebar_target(
        request: Request,
        path: str,
        *,
        target: str,
    ) -> str:
        """Authorize a persisted bookmark/launcher target at registration time."""

        raw = str(path or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="パスを指定してください")

        if raw.startswith("HF|") or raw == "HF":
            if target == "launcher":
                raise HTTPException(
                    status_code=400,
                    detail="HF仮想フォルダはランチャーへ登録できません",
                )
            if not _is_hf_virtual_folder(raw):
                raise HTTPException(status_code=400, detail="HF仮想パスが不正です")
            return raw

        if raw == "Hydrus" or raw.startswith("Hydrus|"):
            raise HTTPException(
                status_code=400,
                detail="HydrusのタグはFilesブックマークから管理してください",
            )
        if raw.startswith("remote://"):
            raise HTTPException(
                status_code=400,
                detail="リモートファイルはFilesサイドバーへ登録できません",
            )
        if raw.startswith("aoitalk-record-table:"):
            if target != "launcher":
                raise HTTPException(
                    status_code=400,
                    detail="レコードテーブルはフォルダブックマークへ登録できません",
                )
            return await _validate_record_table_launcher_path(request, raw)

        if not FILE_EXPLORER_AVAILABLE or explorer_get_file_info is None:
            raise HTTPException(status_code=503, detail="File explorer is not available")

        authorized_paths, is_admin = await authorize_explorer_paths(request, [raw])
        authorized_path = authorized_paths[0]
        result = explorer_get_file_info(authorized_path, is_admin=is_admin)
        if not result.get("success"):
            raise HTTPException(
                status_code=404,
                detail=result.get("error", "対象が見つかりません"),
            )
        is_directory = bool(result.get("is_directory"))
        if target == "bookmark" and not is_directory:
            raise HTTPException(
                status_code=400,
                detail="ブックマーク対象はフォルダで指定してください",
            )
        if target == "launcher" and is_directory:
            raise HTTPException(
                status_code=400,
                detail="ランチャー対象はファイルで指定してください",
            )
        return authorized_path

    async def _validate_shared_sidebar_target(
        request: Request,
        path: str,
        *,
        target: str,
        space_id: UUID,
    ) -> str:
        """Validate a target which will be visible to every Space reader.

        A shared row must point to a Project Files object whose canonical
        ``Project.space_id`` is the requested Space.  User storage, provider
        virtual paths, absolute/admin paths, and any ambiguous namespace are
        intentionally rejected.  Record Table virtual files are the one
        additional supported target and are checked through their Project
        relation as well.
        """

        raw = str(path or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="パスを指定してください")

        if raw.startswith("aoitalk-record-table:"):
            if target != "launcher":
                raise HTTPException(
                    status_code=400,
                    detail="レコードテーブルはフォルダブックマークへ登録できません",
                )
            return await _validate_record_table_launcher_path(
                request, raw, shared_space_id=space_id
            )

        if (
            raw == "HF"
            or raw.startswith("HF|")
            or raw == "Hydrus"
            or raw.startswith("Hydrus|")
            or raw.startswith("remote://")
            or raw.startswith("_users/")
            or raw.startswith("_users\\")
        ):
            raise HTTPException(
                status_code=400,
                detail="個人または外部のファイルは共有Spaceへ登録できません",
            )

        candidate = raw.replace("\\", "/").strip("/")
        candidate_path = Path(raw)
        if (
            candidate_path.is_absolute()
            or raw.startswith("/")
            or bool(re.match(r"^[A-Za-z]:[\\/]", raw))
        ):
            raise HTTPException(
                status_code=400,
                detail="絶対パスは共有Spaceへ登録できません",
            )
        parts = [part for part in candidate.split("/") if part]
        if len(parts) < 2 or parts[0].casefold() != "_projects":
            raise HTTPException(
                status_code=400,
                detail="共有SpaceへはProject Filesだけを登録できます",
            )
        project_match = _PROJECT_SEGMENT.fullmatch(parts[1])
        if project_match is None:
            raise HTTPException(status_code=400, detail="無効なプロジェクトパスです")
        project_id = UUID(project_match.group(1))

        if server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        from sqlalchemy import select

        from ...memory.models import Project
        from ...memory.project_repository import ProjectRepository

        session = await server._db_manager.get_session()
        try:
            result = await session.execute(
                select(Project).where(
                    Project.id == project_id,
                    Project.space_id == space_id,
                    Project.deleted_at.is_(None),
                )
            )
            project = result.scalar_one_or_none()
            if project is None:
                raise HTTPException(
                    status_code=403,
                    detail="対象Projectは指定Spaceに属していません",
                )
            user_id = await get_bookmark_user_id(request)
            user_info = await server._get_user_info_from_request(request) or {}
            is_admin = getattr(server, "auth_enabled", None) is False or str(
                user_info.get("role") or ""
            ).lower() == "admin"
            if not is_admin:
                allowed = await ProjectRepository.has_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="read",
                )
                if not allowed:
                    raise HTTPException(status_code=403, detail="Permission denied")
        finally:
            await session.close()

        authorized_paths, is_admin = await authorize_explorer_paths(request, [raw])
        authorized_path = authorized_paths[0]
        if not FILE_EXPLORER_AVAILABLE or explorer_get_file_info is None:
            raise HTTPException(status_code=503, detail="File explorer is not available")
        info = explorer_get_file_info(authorized_path, is_admin=is_admin)
        if not info.get("success"):
            raise HTTPException(status_code=404, detail=info.get("error", "対象が見つかりません"))
        is_directory = bool(info.get("is_directory"))
        if target == "bookmark" and not is_directory:
            raise HTTPException(status_code=400, detail="ブックマーク対象はフォルダで指定してください")
        if target == "launcher" and is_directory:
            raise HTTPException(status_code=400, detail="ランチャー対象はファイルで指定してください")
        return authorized_path

    def _shared_target_project_id(path: object) -> UUID | None:
        """Extract a canonical Project owner from a persisted shared target."""

        raw = str(path or "").strip()
        record_match = _RECORD_TABLE_PATH.fullmatch(raw)
        if record_match is not None:
            return UUID(record_match.group(1))
        candidate = raw.replace("\\", "/").strip("/")
        parts = [part for part in candidate.split("/") if part]
        if len(parts) < 2 or parts[0].casefold() != "_projects":
            return None
        project_match = _PROJECT_SEGMENT.fullmatch(parts[1])
        if project_match is None:
            return None
        return UUID(project_match.group(1))

    async def _filter_shared_sidebar_items(
        request: Request,
        session,
        items: list,
        *,
        space_id: UUID,
        user_id: UUID,
        user_info: dict,
    ) -> list:
        """Project-ACL filter for a Space collection read projection.

        Space read grants access to the collection, but each persisted target
        still has a Project ACL.  Filtering here prevents a stale/revoked
        Project membership from leaking path/name metadata through the shared
        sidebar list.  Folder nodes are retained only when they are empty or
        lead to at least one visible descendant.
        """

        if not items:
            return []

        def is_owned_by_requested_space(item) -> bool:
            # Current models expose both ownership columns.  Keep duck-typed
            # test/rolling objects without those attributes compatible, while
            # never projecting a row that is explicitly personal or belongs
            # to another Space.
            if hasattr(item, "space_id"):
                row_space_id = getattr(item, "space_id", None)
                if row_space_id is None or str(row_space_id) != str(space_id):
                    return False
            if hasattr(item, "user_id") and getattr(item, "user_id", None) is not None:
                return False
            return True

        items = [item for item in items if is_owned_by_requested_space(item)]
        if not items:
            return []

        project_ids = {
            project_id
            for item in items
            if str(getattr(item, "kind", "") or "").strip().lower() != "folder"
            and not str(getattr(item, "path", "") or "").startswith(
                BOOKMARK_FOLDER_PATH_PREFIX
            )
            for project_id in [_shared_target_project_id(getattr(item, "path", None))]
            if project_id is not None
        }

        visible_projects: set[UUID] = set()
        if project_ids:
            from sqlalchemy import select

            from ...memory.models import Project
            from ...memory.project_repository import ProjectRepository

            result = await session.execute(
                select(Project.id).where(
                    Project.id.in_(project_ids),
                    Project.space_id == space_id,
                    Project.deleted_at.is_(None),
                )
            )
            existing_projects = set(result.scalars().all())
            is_admin = getattr(server, "auth_enabled", None) is False or str(
                user_info.get("role") or ""
            ).lower() == "admin"
            if not is_admin:
                try:
                    is_admin = await server._is_admin_user(request)
                except Exception:
                    is_admin = False
            if is_admin:
                visible_projects = existing_projects
            else:
                for project_id in existing_projects:
                    if await ProjectRepository.has_permission(
                        session,
                        project_id=project_id,
                        user_id=user_id,
                        permission="read",
                    ):
                        visible_projects.add(project_id)

        direct_visibility: dict[UUID, bool] = {}
        children: dict[UUID, list] = {}
        for item in items:
            item_id = getattr(item, "id", None)
            kind = str(getattr(item, "kind", "") or "").strip().lower()
            path = str(getattr(item, "path", "") or "")
            if item_id is not None:
                parent_id = getattr(item, "parent_id", None)
                if parent_id is not None:
                    # Keep every direct child in the tree, including leaves.
                    # If only folders are recorded here, a folder whose only
                    # descendants are ACL-hidden leaves is mistaken for an
                    # empty folder and leaks its name through the projection.
                    children.setdefault(parent_id, []).append(item)
            if kind == "folder" or path.startswith(BOOKMARK_FOLDER_PATH_PREFIX):
                continue
            project_id = _shared_target_project_id(path)
            # Unknown/private/absolute/provider paths are never projected from
            # a shared collection, even if a legacy row was migrated into it.
            direct_visibility[item_id] = project_id is not None and project_id in visible_projects

        folder_visibility: dict[UUID, bool] = {}
        visiting_folders: set[UUID] = set()

        def visible_folder(folder) -> bool:
            folder_id = getattr(folder, "id", None)
            if folder_id is None:
                return False
            if folder_id in folder_visibility:
                return folder_visibility[folder_id]
            if folder_id in visiting_folders:
                return False
            visiting_folders.add(folder_id)
            descendants = children.get(folder_id, [])
            if not descendants:
                # An explicitly-created empty Space folder is safe to show.
                folder_visibility[folder_id] = True
                visiting_folders.discard(folder_id)
                return True
            result = any(
                visible_folder(child)
                if str(getattr(child, "kind", "") or "").strip().lower() == "folder"
                else direct_visibility.get(getattr(child, "id", None), False)
                for child in descendants
            )
            folder_visibility[folder_id] = result
            visiting_folders.discard(folder_id)
            return result

        filtered: list = []
        for item in items:
            kind = str(getattr(item, "kind", "") or "").strip().lower()
            path = str(getattr(item, "path", "") or "")
            if kind == "folder" or path.startswith(BOOKMARK_FOLDER_PATH_PREFIX):
                if visible_folder(item):
                    filtered.append(item)
            elif direct_visibility.get(getattr(item, "id", None), False):
                filtered.append(item)
        return filtered

    @app.get("/api/explorer/bookmarks")
    async def explorer_bookmarks_list(
        request: Request, _: None = Depends(require_auth)
    ):
        """Get all bookmarks"""
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                bookmarks = await FileExplorerBookmarkRepository.list_for_user(
                    session, user_id
                )
                return JSONResponse(
                    {
                        "success": True,
                        "bookmarks": [bookmark.to_dict() for bookmark in bookmarks],
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get bookmarks: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    class ExplorerBookmarkPayload(BaseModel):
        name: str = Field(..., min_length=1, max_length=200)
        path: str | None = Field(default=None, max_length=4096)
        icon: str = Field("📁", max_length=64)
        kind: Literal["bookmark", "folder"] = "bookmark"
        parent_id: UUID | None = None

        @field_validator("name")
        @classmethod
        def validate_non_empty_name(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("値を空にできません")
            return value

        @field_validator("path")
        @classmethod
        def validate_optional_path(cls, value: str | None) -> str | None:
            if value is None:
                return None
            value = value.strip()
            return value or None

        @model_validator(mode="after")
        def validate_path_for_kind(self) -> "ExplorerBookmarkPayload":
            if self.kind == "bookmark" and not self.path:
                raise ValueError("ブックマークのパスを指定してください")
            return self

    class ExplorerBookmarkUpdatePayload(BaseModel):
        name: str | None = Field(default=None, max_length=200)
        icon: str | None = Field(default=None, max_length=64)
        sort_order: float | None = None
        parent_id: UUID | None = None

        @field_validator("name")
        @classmethod
        def validate_optional_name(cls, value: str | None) -> str | None:
            if value is None:
                return None
            value = value.strip()
            if not value:
                raise ValueError("ブックマーク名を空にできません")
            return value

        @field_validator("sort_order")
        @classmethod
        def validate_sort_order(cls, value: float | None) -> float | None:
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("sort_orderは有限値で指定してください")
            return value

    @app.post("/api/explorer/bookmarks")
    async def explorer_bookmarks_add(
        request: Request,
        payload: ExplorerBookmarkPayload,
        _: None = Depends(require_auth),
    ):
        """Add a new bookmark"""
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

        try:
            user_id = await get_bookmark_user_id(request)
            validated_path: str | None = None
            if payload.kind == "bookmark":
                if payload.path and payload.path.startswith(BOOKMARK_FOLDER_PATH_PREFIX):
                    raise HTTPException(
                        status_code=400,
                        detail="このパスはブックマークに使用できません",
                    )
                validated_path = await _validate_sidebar_target(
                    request, payload.path, target="bookmark"
                )
            session = await server._db_manager.get_session()
            try:
                bookmark = await FileExplorerBookmarkRepository.add(
                    session,
                    user_id,
                    payload.name,
                    validated_path,
                    payload.icon,
                    kind=payload.kind,
                    parent_id=payload.parent_id,
                )
                return JSONResponse(
                    {"success": True, "bookmark": bookmark.to_dict()}
                )
            finally:
                await session.close()
        except IntegrityError as e:
            # The repository normally normalizes this race, but keep the
            # endpoint contract stable if a backend leaks the raw exception.
            raise HTTPException(
                status_code=400,
                detail="このパスは既にブックマークされています",
            ) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    class ExplorerBookmarkDeletePayload(BaseModel):
        path: str

    @app.delete("/api/explorer/bookmarks")
    async def explorer_bookmarks_delete(
        request: Request,
        payload: ExplorerBookmarkDeletePayload,
        _: None = Depends(require_auth),
    ):
        """Remove a bookmark"""
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")

        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                removed = await FileExplorerBookmarkRepository.remove_by_path(
                    session, user_id, payload.path
                )
            finally:
                await session.close()
            if not removed:
                raise HTTPException(
                    status_code=400,
                    detail="ブックマークが見つかりません",
                )
            return JSONResponse(
                {"success": True, "message": "ブックマークを削除しました"}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.patch("/api/explorer/bookmarks/{bookmark_id}")
    async def explorer_bookmarks_update(
        bookmark_id: UUID,
        request: Request,
        payload: ExplorerBookmarkUpdatePayload,
        _: None = Depends(require_auth),
    ):
        """Rename or reorder one bookmark owned by the authenticated user."""
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                update_kwargs = {
                    "name": payload.name,
                    "icon": payload.icon,
                    "sort_order": payload.sort_order,
                }
                if "parent_id" in payload.model_fields_set:
                    update_kwargs["parent_id"] = payload.parent_id
                bookmark = await FileExplorerBookmarkRepository.update(
                    session,
                    user_id,
                    bookmark_id,
                    **update_kwargs,
                )
            finally:
                await session.close()
            if bookmark is None:
                raise HTTPException(status_code=404, detail="ブックマークが見つかりません")
            return JSONResponse({"success": True, "bookmark": bookmark.to_dict()})
        except ValueError as e:
            if "parent_id" in payload.model_fields_set:
                raise HTTPException(status_code=400, detail=str(e))
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/explorer/bookmarks/{bookmark_id}")
    async def explorer_bookmarks_delete_by_id(
        bookmark_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Remove one bookmark owned by the authenticated user."""
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                removed = await FileExplorerBookmarkRepository.remove(
                    session, user_id, bookmark_id
                )
            finally:
                await session.close()
            if not removed:
                raise HTTPException(status_code=404, detail="ブックマークが見つかりません")
            return JSONResponse({"success": True, "message": "ブックマークを削除しました"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── File Explorer Launcher Endpoints ───────────────────────────────

    class ExplorerLauncherPayload(BaseModel):
        name: str = Field(..., min_length=1, max_length=200)
        path: str = Field(..., min_length=1, max_length=4096)
        icon: str = Field("📄", max_length=64)

        @field_validator("name", "path")
        @classmethod
        def validate_non_empty_text(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("値を空にできません")
            return value

    class ExplorerLauncherUpdatePayload(BaseModel):
        name: str | None = Field(default=None, max_length=200)
        icon: str | None = Field(default=None, max_length=64)
        sort_order: float | None = None

        @field_validator("name")
        @classmethod
        def validate_optional_name(cls, value: str | None) -> str | None:
            if value is None:
                return None
            value = value.strip()
            if not value:
                raise ValueError("ランチャー名を空にできません")
            return value

        @field_validator("sort_order")
        @classmethod
        def validate_sort_order(cls, value: float | None) -> float | None:
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("sort_orderは有限値で指定してください")
            return value

    @app.get("/api/explorer/launchers")
    async def explorer_launchers_list(
        request: Request, _: None = Depends(require_auth)
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                launchers = await FileExplorerLauncherRepository.list_for_user(
                    session, user_id
                )
                return JSONResponse(
                    {
                        "success": True,
                        "launchers": [launcher.to_dict() for launcher in launchers],
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get launchers: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/explorer/launchers")
    async def explorer_launchers_add(
        request: Request,
        payload: ExplorerLauncherPayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            validated_path = await _validate_sidebar_target(
                request, payload.path, target="launcher"
            )
            session = await server._db_manager.get_session()
            try:
                launcher = await FileExplorerLauncherRepository.add(
                    session, user_id, payload.name, validated_path, payload.icon
                )
                return JSONResponse({"success": True, "launcher": launcher.to_dict()})
            finally:
                await session.close()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.patch("/api/explorer/launchers/{launcher_id}")
    async def explorer_launchers_update(
        launcher_id: UUID,
        request: Request,
        payload: ExplorerLauncherUpdatePayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                launcher = await FileExplorerLauncherRepository.update(
                    session,
                    user_id,
                    launcher_id,
                    name=payload.name,
                    icon=payload.icon,
                    sort_order=payload.sort_order,
                )
            finally:
                await session.close()
            if launcher is None:
                raise HTTPException(status_code=404, detail="ランチャーが見つかりません")
            return JSONResponse({"success": True, "launcher": launcher.to_dict()})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/explorer/launchers/{launcher_id}")
    async def explorer_launchers_delete(
        launcher_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        try:
            user_id = await get_bookmark_user_id(request)
            session = await server._db_manager.get_session()
            try:
                removed = await FileExplorerLauncherRepository.remove(
                    session, user_id, launcher_id
                )
            finally:
                await session.close()
            if not removed:
                raise HTTPException(status_code=404, detail="ランチャーが見つかりません")
            return JSONResponse({"success": True, "message": "ランチャーを削除しました"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Canonical Space-scoped File Explorer Sidebar API ────────────────
    #
    # These routes deliberately use the explicit *_for_space repository API.
    # The legacy /api/explorer/* routes above remain personal/user-owned
    # compatibility endpoints and cannot address shared rows by item ID.

    def _space_repo_method(repo, name: str):
        method = getattr(repo, name, None)
        if method is None:
            raise HTTPException(status_code=503, detail="共有サイドバーAPIが利用できません")
        return method

    @app.get("/api/spaces/{space_id}/explorer/bookmarks")
    async def explorer_space_bookmarks_list(
        space_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            _scope, _space, user_id, user_info = await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=False
            )
            bookmarks = await _space_repo_method(
                FileExplorerBookmarkRepository, "list_for_space"
            )(session, space_id)
            bookmarks = await _filter_shared_sidebar_items(
                request,
                session,
                bookmarks,
                space_id=space_id,
                user_id=user_id,
                user_info=user_info,
            )
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "bookmarks": [
                        _sidebar_item_to_dict(bookmark, space_id=space_id)
                        for bookmark in bookmarks
                    ],
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get Space bookmarks: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.post("/api/spaces/{space_id}/explorer/bookmarks")
    async def explorer_space_bookmarks_add(
        space_id: UUID,
        request: Request,
        payload: ExplorerBookmarkPayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            _scope, _space, _user_id, _user_info = await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            validated_path: str | None = None
            if payload.kind == "bookmark":
                if payload.path and payload.path.startswith(BOOKMARK_FOLDER_PATH_PREFIX):
                    raise HTTPException(
                        status_code=400,
                        detail="このパスはブックマークに使用できません",
                    )
                validated_path = await _validate_shared_sidebar_target(
                    request, payload.path, target="bookmark", space_id=space_id
                )
            bookmark = await _space_repo_method(
                FileExplorerBookmarkRepository, "add_for_space"
            )(
                session,
                space_id,
                payload.name,
                validated_path,
                payload.icon,
                kind=payload.kind,
                parent_id=payload.parent_id,
            )
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "bookmark": _sidebar_item_to_dict(bookmark, space_id=space_id),
                }
            )
        except IntegrityError as e:
            raise HTTPException(
                status_code=400,
                detail="このパスは既にブックマークされています",
            ) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add Space bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.delete("/api/spaces/{space_id}/explorer/bookmarks")
    async def explorer_space_bookmarks_delete_by_path(
        space_id: UUID,
        request: Request,
        payload: ExplorerBookmarkDeletePayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            removed = await _space_repo_method(
                FileExplorerBookmarkRepository, "remove_by_path_for_space"
            )(session, space_id, payload.path)
            if not removed:
                raise HTTPException(status_code=404, detail="ブックマークが見つかりません")
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "message": "ブックマークを削除しました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove Space bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.patch("/api/spaces/{space_id}/explorer/bookmarks/{bookmark_id}")
    async def explorer_space_bookmarks_update(
        space_id: UUID,
        bookmark_id: UUID,
        request: Request,
        payload: ExplorerBookmarkUpdatePayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            update_kwargs = {
                "name": payload.name,
                "icon": payload.icon,
                "sort_order": payload.sort_order,
            }
            if "parent_id" in payload.model_fields_set:
                update_kwargs["parent_id"] = payload.parent_id
            bookmark = await _space_repo_method(
                FileExplorerBookmarkRepository, "update_for_space"
            )(session, space_id, bookmark_id, **update_kwargs)
            if bookmark is None:
                raise HTTPException(status_code=404, detail="ブックマークが見つかりません")
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "bookmark": _sidebar_item_to_dict(bookmark, space_id=space_id),
                }
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update Space bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.delete("/api/spaces/{space_id}/explorer/bookmarks/{bookmark_id}")
    async def explorer_space_bookmarks_delete(
        space_id: UUID,
        bookmark_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if FileExplorerBookmarkRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            removed = await _space_repo_method(
                FileExplorerBookmarkRepository, "remove_for_space"
            )(session, space_id, bookmark_id)
            if not removed:
                raise HTTPException(status_code=404, detail="ブックマークが見つかりません")
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "message": "ブックマークを削除しました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove Space bookmark: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.get("/api/spaces/{space_id}/explorer/launchers")
    async def explorer_space_launchers_list(
        space_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            _scope, _space, user_id, user_info = await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=False
            )
            launchers = await _space_repo_method(
                FileExplorerLauncherRepository, "list_for_space"
            )(session, space_id)
            launchers = await _filter_shared_sidebar_items(
                request,
                session,
                launchers,
                space_id=space_id,
                user_id=user_id,
                user_info=user_info,
            )
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "launchers": [
                        _sidebar_item_to_dict(launcher, space_id=space_id)
                        for launcher in launchers
                    ],
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get Space launchers: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.post("/api/spaces/{space_id}/explorer/launchers")
    async def explorer_space_launchers_add(
        space_id: UUID,
        request: Request,
        payload: ExplorerLauncherPayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            validated_path = await _validate_shared_sidebar_target(
                request, payload.path, target="launcher", space_id=space_id
            )
            launcher = await _space_repo_method(
                FileExplorerLauncherRepository, "add_for_space"
            )(session, space_id, payload.name, validated_path, payload.icon)
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "launcher": _sidebar_item_to_dict(launcher, space_id=space_id),
                }
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add Space launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.patch("/api/spaces/{space_id}/explorer/launchers/{launcher_id}")
    async def explorer_space_launchers_update(
        space_id: UUID,
        launcher_id: UUID,
        request: Request,
        payload: ExplorerLauncherUpdatePayload,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            launcher = await _space_repo_method(
                FileExplorerLauncherRepository, "update_for_space"
            )(
                session,
                space_id,
                launcher_id,
                name=payload.name,
                icon=payload.icon,
                sort_order=payload.sort_order,
            )
            if launcher is None:
                raise HTTPException(status_code=404, detail="ランチャーが見つかりません")
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "launcher": _sidebar_item_to_dict(launcher, space_id=space_id),
                }
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update Space launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    @app.delete("/api/spaces/{space_id}/explorer/launchers/{launcher_id}")
    async def explorer_space_launchers_delete(
        space_id: UUID,
        launcher_id: UUID,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if FileExplorerLauncherRepository is None or server._db_manager is None:
            raise HTTPException(status_code=503, detail="Database is not available")
        session = await server._db_manager.get_session()
        try:
            await _resolve_sidebar_scope(
                request, session, scope="shared", space_id=space_id, write=True
            )
            removed = await _space_repo_method(
                FileExplorerLauncherRepository, "remove_for_space"
            )(session, space_id, launcher_id)
            if not removed:
                raise HTTPException(status_code=404, detail="ランチャーが見つかりません")
            return JSONResponse(
                {
                    "success": True,
                    "space_id": str(space_id),
                    "message": "ランチャーを削除しました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove Space launcher: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await session.close()

    # ── File Editor API Endpoints ──────────────────────────────────────

    class SaveFilePayload(BaseModel):
        path: str
        content: str
        encoding: str = "utf-8"

    @app.put("/api/explorer/save")
    async def explorer_save_file_endpoint(
        payload: SaveFilePayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Save text content to a file"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        try:
            [authorized_path], is_admin = await authorize_explorer_paths(
                request, [payload.path], write=True
            )
            result = explorer_save_file(
                authorized_path,
                payload.content,
                payload.encoding,
                is_admin=is_admin,
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to save file"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to save file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/explorer/content")
    async def explorer_get_content(
        request: Request,
        path: str = Query(...),
        _: None = Depends(require_auth),
    ):
        """Get full text content for editor"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        try:
            [path], is_admin = await authorize_explorer_paths(request, [path])
            result = explorer_get_full_content(path, is_admin=is_admin)
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to get content"),
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get file content: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/explorer/search")
    async def explorer_search_endpoint(
        request: Request,
        q: str = Query(..., min_length=1),
        root: str = Query(""),
        limit: int = Query(20, ge=1, le=200),
        regex: bool = Query(False),
        _: None = Depends(require_auth),
    ):
        """Search files and directories by name"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        try:
            [root], is_admin = await authorize_explorer_paths(request, [root])
            result = explorer_search_workspace_entries(
                q,
                path=root,
                include_dirs=True,
                include_files=True,
                max_results=limit,
                is_admin=is_admin,
                regex=regex,
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Search failed")
                )
            result["total"] = result.get(
                "total_returned", len(result.get("results", []))
            )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search files: {e}")
            raise HTTPException(status_code=500, detail=str(e))
