"""ドキュメント変換アップロード・ストレージコンテキスト系ルート (server.py から移設)"""

import logging
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency

# Import document converter (server.py と同じフォールバック付き)
try:
    from ...tools.documents.office_reader import (
        convert_office_bytes_to_markdown,
        is_supported as is_office_file_supported,
        SUPPORTED_EXTENSIONS as OFFICE_SUPPORTED_EXTENSIONS,
    )

    OFFICE_READER_AVAILABLE = True
except ImportError:
    OFFICE_READER_AVAILABLE = False
    convert_office_bytes_to_markdown = None
    is_office_file_supported = None
    OFFICE_SUPPORTED_EXTENSIONS = set()

# Import storage context service (server.py と同じフォールバック付き)
try:
    from ...tools.file_explorer.storage_context import (
        StorageContextType,
        get_context_root,
        ensure_user_storage,
        get_available_contexts_for_user,
        calculate_storage_usage,
    )

    STORAGE_CONTEXT_AVAILABLE = True
except ImportError:
    STORAGE_CONTEXT_AVAILABLE = False
    StorageContextType = None
    get_context_root = None
    ensure_user_storage = None
    get_available_contexts_for_user = None
    calculate_storage_usage = None

# Project routes 利用可否 (storage contexts でのプロジェクト一覧取得に使用)
try:
    from ..project_routes import create_project_router  # noqa: F401

    PROJECT_ROUTES_AVAILABLE = True
except ImportError:
    PROJECT_ROUTES_AVAILABLE = False

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_document_storage_routes(app: FastAPI, server: "WebChatServer") -> None:
    """documents/upload / storage contexts / storage usage ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    # ── Document Upload API Endpoints ─────────────────────────────────────
    # Convert Office files (docx, xlsx, pptx, pdf) to Markdown
    # Also supports plain text/data files directly

    # Supported text file extensions (read directly without conversion)
    TEXT_FILE_EXTENSIONS = {
        # テキスト
        ".txt",
        ".log",
        ".md",
        ".markdown",
        ".rst",
        ".text",
        # データ/設定ファイル
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        # Web関連
        ".html",
        ".htm",
        ".css",
        # コード（主要言語）
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".bat",
        ".ps1",
        ".sql",
        ".r",
        ".swift",
        ".kt",
        ".scala",
        ".lua",
    }

    def is_text_file(filename: str) -> bool:
        """Check if file is a plain text file"""
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext in TEXT_FILE_EXTENSIONS

    @app.post("/api/documents/upload")
    async def upload_document(
        file: UploadFile = File(...), _: None = Depends(require_auth)
    ):
        """Upload and convert document to text/markdown"""
        filename = file.filename or "unnamed_file"

        try:
            # Read file content
            file_bytes = await file.read()

            # Check if it's a plain text file
            if is_text_file(filename):
                # Decode text file directly
                try:
                    content = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    # Try other encodings
                    try:
                        content = file_bytes.decode("shift_jis")
                    except UnicodeDecodeError:
                        content = file_bytes.decode("utf-8", errors="replace")

                logger.info(f"Text file read: {filename} ({len(file_bytes)} bytes)")

                return JSONResponse(
                    {
                        "success": True,
                        "filename": filename,
                        "content": content,
                        "size_bytes": len(file_bytes),
                    }
                )

            # Check if it's an Office file
            if OFFICE_READER_AVAILABLE and is_office_file_supported(filename):
                # Convert using office_reader
                result = convert_office_bytes_to_markdown(file_bytes, filename)

                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "ファイル変換に失敗しました"),
                    )

                logger.info(
                    f"Document converted: {filename} ({len(file_bytes)} bytes)"
                )

                return JSONResponse(
                    {
                        "success": True,
                        "filename": filename,
                        "content": result.get("content", ""),
                        "size_bytes": len(file_bytes),
                    }
                )

            # Unsupported file type
            all_supported = list(TEXT_FILE_EXTENSIONS)
            if OFFICE_READER_AVAILABLE:
                all_supported.extend(OFFICE_SUPPORTED_EXTENSIONS)
            supported_list = ", ".join(sorted(all_supported))
            raise HTTPException(
                status_code=400,
                detail=f"対応していないファイル形式です。対応形式: {supported_list}",
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to process document: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Storage Context API Endpoints ────────────────────────────────────

    @app.get("/api/storage/contexts")
    async def get_storage_contexts(
        request: Request, _: None = Depends(require_auth)
    ):
        """Get available storage contexts for the current user"""
        if not STORAGE_CONTEXT_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Storage context service not available"
            )

        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            from uuid import UUID

            user_id = UUID(user_info["id"])

            # Check if user is admin
            is_admin = await server._is_admin_user(request)

            # Ensure user storage exists
            ensure_user_storage(user_id)

            # Get user's projects if ProjectRepository is available
            projects = []
            if server._db_manager and PROJECT_ROUTES_AVAILABLE:
                from ...memory.project_repository import ProjectRepository

                session = await server._db_manager.get_session()
                try:
                    projects = await ProjectRepository.get_user_projects(
                        session, user_id
                    )
                finally:
                    await session.close()

            contexts = get_available_contexts_for_user(user_id, projects)

            return JSONResponse(
                {
                    "success": True,
                    "contexts": contexts,
                    "current_context": {"type": "personal", "id": str(user_id)},
                    "is_admin": is_admin,
                }
            )
        except Exception as e:
            logger.error(f"Failed to get storage contexts: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/storage/usage")
    async def get_storage_usage(
        request: Request,
        context_type: str = "personal",
        context_id: Optional[str] = None,
        _: None = Depends(require_auth),
    ):
        """Get storage usage for a context"""
        if not STORAGE_CONTEXT_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Storage context service not available"
            )

        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            from uuid import UUID

            user_id = UUID(user_info["id"])

            ctx_type = StorageContextType(context_type)
            ctx_id = UUID(context_id) if context_id else None

            root_path, valid = get_context_root(ctx_type, ctx_id, user_id)
            if not valid:
                raise HTTPException(
                    status_code=400, detail="Invalid storage context"
                )

            usage = calculate_storage_usage(root_path)

            return JSONResponse(
                {
                    "success": True,
                    "context_type": context_type,
                    "context_id": context_id,
                    "usage": usage,
                }
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid context type: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to get storage usage: {e}")
            raise HTTPException(status_code=500, detail=str(e))
