"""ファイラー・ファイルエクスプローラー系ルート (server.py から移設)"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from ..router_helpers import cookie_auth_dependency

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
        upload_file as explorer_upload_file,
        download_file as explorer_download_file,
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
        search_files as explorer_search_files,
        resolve_file_path as explorer_resolve_file_path,
        # Folder thumbnail
        set_folder_thumbnail as explorer_set_folder_thumbnail,
        clear_folder_thumbnail as explorer_clear_folder_thumbnail,
    )

    FILE_EXPLORER_AVAILABLE = True
except ImportError:
    FILE_EXPLORER_AVAILABLE = False
    explorer_list_directory = None
    explorer_create_directory = None
    explorer_upload_file = None
    explorer_download_file = None
    explorer_rename_item = None
    explorer_move_item = None
    explorer_copy_item = None
    explorer_archive_items = None
    explorer_extract_archives = None
    explorer_delete_item = None
    explorer_get_file_info = None
    explorer_get_preview = None
    explorer_get_directory_tree = None
    explorer_resolve_file_path = None

# Import storage context (memo 保存先解決用)
try:
    from ...tools.file_explorer.storage_context import ensure_user_storage
except ImportError:
    ensure_user_storage = None

# Import bookmark repository (server.py と同じフォールバック付き)
try:
    from ...memory.file_explorer_bookmark_repository import (
        FileExplorerBookmarkRepository,
    )
except ImportError:
    FileExplorerBookmarkRepository = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_file_explorer_routes(app: FastAPI, server: "WebChatServer") -> None:
    """filer / explorer / bookmark / editor 系ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def require_admin_filer_access(request: Request) -> None:
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="管理者権限が必要です")

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

        thumbnail_path = get_video_thumbnail_path(path)
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

    @app.get("/api/explorer/tree")
    async def get_explorer_tree(root: str = "", _: None = Depends(require_auth)):
        """Get directory tree structure"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )

        try:
            result = explorer_get_directory_tree(root_path=root)
            return JSONResponse(result)
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
            is_admin = await server._is_admin_user(request)

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
            is_admin = await server._is_admin_user(request)
            result = explorer_create_directory(
                payload.path, payload.name, is_admin=is_admin
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
            file_bytes = await file.read()
            filename = relative_path or file.filename or "unnamed_file"

            is_admin = await server._is_admin_user(request)
            result = explorer_upload_file(
                path, filename, file_bytes, is_admin=is_admin
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

        is_admin = await server._is_admin_user(request)
        content, filename, mime_type = explorer_download_file(
            path, is_admin=is_admin
        )
        if content is None:
            raise HTTPException(status_code=404, detail="File not found")

        from fastapi.responses import Response
        from urllib.parse import quote

        # RFC 5987: Use filename* for non-ASCII filenames
        # Also include ASCII-safe filename for compatibility
        ascii_filename = filename.encode("ascii", "replace").decode("ascii")
        encoded_filename = quote(filename, safe="")
        content_disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"

        return Response(
            content=content,
            media_type=mime_type,
            headers={"Content-Disposition": content_disposition},
        )

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

        is_admin = await server._is_admin_user(request)
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

        is_admin = await server._is_admin_user(request)
        file_path = explorer_resolve_file_path(path, is_admin=is_admin)
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        thumbnail_path = get_video_thumbnail_path(str(file_path))
        if thumbnail_path is None:
            raise HTTPException(
                status_code=404, detail="Video thumbnail not available"
            )

        return FileResponse(
            path=str(thumbnail_path),
            media_type="image/jpeg",
            filename=thumbnail_path.name,
        )

    async def _serve_image_thumbnail(file_path: Path, size: int) -> Response:
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
        is_admin = await server._is_admin_user(request)
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
        is_admin = await server._is_admin_user(request)
        result = explorer_set_folder_thumbnail(
            payload.folder_path, payload.target_path, is_admin=is_admin
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
        is_admin = await server._is_admin_user(request)
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
            is_admin = await server._is_admin_user(request)
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
            is_admin = await server._is_admin_user(request)
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
            is_admin = await server._is_admin_user(request)
            result = explorer_rename_item(
                payload.path, payload.new_name, is_admin=is_admin
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
            is_admin = await server._is_admin_user(request)
            result = explorer_move_item(
                payload.src, payload.dest, is_admin=is_admin
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
            is_admin = await server._is_admin_user(request)
            result = explorer_copy_item(
                payload.src, payload.dest, is_admin=is_admin
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
            is_admin = await server._is_admin_user(request)
            result = explorer_archive_items(
                payload.paths, payload.dest, is_admin=is_admin
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
            is_admin = await server._is_admin_user(request)
            result = explorer_extract_archives(
                payload.paths, payload.dest, is_admin=is_admin
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
            is_admin = await server._is_admin_user(request)
            result = explorer_delete_item(path, is_admin=is_admin)
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

    # ── File Explorer Bookmark Endpoints ────────────────────────────────

    async def get_bookmark_user_id(request: Request):
        user_info = await server._get_user_info_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        from uuid import UUID as _UUID

        try:
            return _UUID(user_info["id"])
        except ValueError:
            raise HTTPException(status_code=401, detail="Unauthorized")

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
        name: str
        path: str
        icon: str = "📁"

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
            session = await server._db_manager.get_session()
            try:
                bookmark = await FileExplorerBookmarkRepository.add(
                    session,
                    user_id,
                    payload.name.strip() or "Bookmark",
                    payload.path,
                    payload.icon,
                )
                return JSONResponse(
                    {"success": True, "bookmark": bookmark.to_dict()}
                )
            finally:
                await session.close()
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
            is_admin = await server._is_admin_user(request)
            result = explorer_save_file(
                payload.path,
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
            is_admin = await server._is_admin_user(request)
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
        limit: int = Query(20, ge=1, le=50),
        _: None = Depends(require_auth),
    ):
        """Search files by name"""
        if not FILE_EXPLORER_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="File explorer is not available"
            )
        try:
            is_admin = await server._is_admin_user(request)
            result = explorer_search_files(q, root, limit, is_admin=is_admin)
            if not result.get("success"):
                raise HTTPException(
                    status_code=400, detail=result.get("error", "Search failed")
                )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search files: {e}")
            raise HTTPException(status_code=500, detail=str(e))
