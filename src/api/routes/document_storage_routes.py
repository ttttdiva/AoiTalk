"""ドキュメント変換アップロード・ストレージコンテキスト系ルート (server.py から移設)"""

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
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


# Keep this endpoint bounded even when the multipart parser has already
# accepted a large request.  ``+1`` is intentional: it lets us distinguish an
# exactly-at-limit upload from an oversized one without an unbounded read.
DOCUMENT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
OFFICE_CONVERSION_MAX_WORKERS = 2
OFFICE_CONVERSION_TIMEOUT_SECONDS = 20.0
OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS = 1.0
OFFICE_CONVERSION_OUTPUT_MAX_BYTES = 16 * 1024 * 1024
OFFICE_CONVERSION_RESULT_MAX_BYTES = OFFICE_CONVERSION_OUTPUT_MAX_BYTES + 1024 * 1024
_OFFICE_CONVERSION_SLOTS = threading.BoundedSemaphore(OFFICE_CONVERSION_MAX_WORKERS)
_OFFICE_PROCESS_CONTEXT = multiprocessing.get_context("spawn")


class OfficeConversionBusyError(RuntimeError):
    """All bounded Office conversion workers are currently occupied."""


class OfficeConversionTimeoutError(RuntimeError):
    """An Office conversion exceeded its absolute time budget."""


class OfficeConversionError(RuntimeError):
    """An Office converter failed without exposing parser details to clients."""


def _safe_office_result(result: object) -> dict[str, object]:
    """Normalize a converter result before it crosses the process boundary."""

    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        return {"success": False, "error": "ファイル変換に失敗しました"}
    if not result["success"]:
        return {"success": False, "error": "ファイル変換に失敗しました"}

    content = result.get("content")
    if not isinstance(content, str) or not content:
        return {"success": False, "error": "ファイル変換に失敗しました"}
    try:
        encoded_size = len(content.encode("utf-8", errors="replace"))
    except (MemoryError, UnicodeError):
        return {"success": False, "error": "ファイル変換に失敗しました"}
    if encoded_size > OFFICE_CONVERSION_OUTPUT_MAX_BYTES:
        return {"success": False, "error": "ファイル変換に失敗しました"}

    normalized: dict[str, object] = {"success": True, "content": content}
    filename = result.get("filename")
    if isinstance(filename, str) and len(filename) <= 4096:
        normalized["filename"] = filename
    return normalized


def _write_office_child_result(result_path: str, result: object) -> None:
    """Write one bounded, atomically replaced JSON result for the parent."""

    normalized = _safe_office_result(result)
    try:
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (MemoryError, TypeError, ValueError, UnicodeError):
        payload = b'{"success":false,"error":"conversion failed"}'
    if len(payload) > OFFICE_CONVERSION_RESULT_MAX_BYTES:
        payload = b'{"success":false,"error":"conversion failed"}'

    result_file = Path(result_path)
    temporary_file = result_file.with_name(result_file.name + ".tmp")
    try:
        with temporary_file.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_file, result_file)
    except (AssertionError, OSError, ValueError):
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass


def _office_conversion_child(
    input_path: str,
    result_path: str,
    filename: str,
) -> None:
    """Spawn target: convert one closed input file and publish a safe result."""

    try:
        input_file = Path(input_path)
        if input_file.stat().st_size > DOCUMENT_UPLOAD_MAX_BYTES:
            raise ValueError("upload too large")
        content = input_file.read_bytes()
        # Parser libraries are third-party code; keep child diagnostics from
        # inheriting the server's stdout/stderr and growing unbounded logs.
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                # Import in the child so this target remains picklable under
                # Windows ``spawn`` and does not depend on monkeypatched
                # parent globals.
                from ...tools.documents.office_reader import (
                    convert_office_bytes_to_markdown,
                )

                result = convert_office_bytes_to_markdown(content, filename)
    except BaseException:
        result = {"success": False, "error": "ファイル変換に失敗しました"}
    _write_office_child_result(result_path, result)


_OFFICE_CONVERSION_TARGET = _office_conversion_child


def _terminate_office_process_sync(process: multiprocessing.Process) -> bool:
    """Best-effort synchronous cleanup used by detached bootstrap workers."""

    try:
        if process.is_alive():
            process.terminate()
            process.join(OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
            process.join(OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            return False
        process.close()
        return True
    except (AssertionError, OSError, ValueError):
        try:
            return not process.is_alive()
        except (AssertionError, OSError, ValueError):
            return False


class _OfficeBootstrap:
    """One bounded daemon thread that owns Process.start until handoff."""

    def __init__(
        self,
        process: multiprocessing.Process,
        temporary_directory: str,
    ) -> None:
        self.process = process
        self.temporary_directory = temporary_directory
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._abandoned = False
        self._started = False
        self.error: BaseException | None = None

    def abandon(self) -> bool:
        """Detach a pending start, or transfer an already-started child."""

        with self._lock:
            self._abandoned = True
            if self._started:
                self._started = False
                return True
            return False

    def claim_started_process(self) -> bool:
        """Claim cleanup ownership after a completed successful start."""

        with self._lock:
            if not self.done.is_set() or self.error is not None or not self._started:
                return False
            self._started = False
            return True

    def run(self) -> None:
        cleanup_owned = False
        try:
            self.process.start()
            with self._lock:
                self._started = True
                cleanup_owned = self._abandoned
                if cleanup_owned:
                    self._started = False
        except BaseException as exc:
            self.error = exc
            cleanup_owned = True
        finally:
            self.done.set()

        if cleanup_owned:
            if self.error is None:
                cleaned = _terminate_office_process_sync(self.process)
            else:
                try:
                    self.process.close()
                    cleaned = True
                except (AssertionError, OSError, ValueError):
                    cleaned = False
            if cleaned:
                shutil.rmtree(self.temporary_directory, ignore_errors=True)
                _OFFICE_CONVERSION_SLOTS.release()


async def _terminate_office_process(process: multiprocessing.Process) -> None:
    """Terminate a child and ensure it is dead before resources are released."""

    try:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(
                process.join,
                OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS,
            )
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
            await asyncio.to_thread(
                process.join,
                OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS,
            )
    except (AssertionError, OSError, ValueError):
        # A process that already exited can race with terminate/is_alive on
        # Windows.  The caller still checks ``is_alive`` before close().
        pass


async def _cleanup_office_process(process: multiprocessing.Process) -> None:
    await _terminate_office_process(process)
    try:
        if process.is_alive():
            raise OfficeConversionError
        process.close()
    except (AssertionError, OSError, ValueError):
        # ``close`` may race with interpreter shutdown; never leak a live
        # process silently, but keep the route-level error generic.
        if process.is_alive():
            raise OfficeConversionError


def _read_office_child_result(result_path: Path) -> dict[str, object]:
    try:
        size = result_path.stat().st_size
        if size <= 0 or size > OFFICE_CONVERSION_RESULT_MAX_BYTES:
            raise OfficeConversionError
        payload = result_path.read_bytes()
        if len(payload) != size or len(payload) > OFFICE_CONVERSION_RESULT_MAX_BYTES:
            raise OfficeConversionError
        result = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OfficeConversionError from exc
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise OfficeConversionError
    if not result["success"]:
        return {"success": False, "error": "ファイル変換に失敗しました"}
    content = result.get("content")
    if not isinstance(content, str) or not content:
        raise OfficeConversionError
    if len(content.encode("utf-8", errors="replace")) > OFFICE_CONVERSION_OUTPUT_MAX_BYTES:
        raise OfficeConversionError
    normalized: dict[str, object] = {"success": True, "content": content}
    filename = result.get("filename")
    if isinstance(filename, str) and len(filename) <= 4096:
        normalized["filename"] = filename
    return normalized


async def _convert_office_with_limits(content: bytes, filename: str):
    """Run one Office conversion in a killable subprocess with hard bounds."""

    if not _OFFICE_CONVERSION_SLOTS.acquire(blocking=False):
        raise OfficeConversionBusyError
    process: multiprocessing.Process | None = None
    process_started = False
    process_cleaned = False
    bootstrap_detached = False
    input_write_task: asyncio.Task[None] | None = None
    bootstrap: _OfficeBootstrap | None = None
    temporary_directory: str | None = None
    deadline = time.monotonic() + OFFICE_CONVERSION_TIMEOUT_SECONDS
    try:
        if len(content) > DOCUMENT_UPLOAD_MAX_BYTES:
            raise OfficeConversionError
        temporary_directory = tempfile.mkdtemp(prefix="office-convert-")
        input_path = Path(temporary_directory) / "input.bin"
        result_path = Path(temporary_directory) / "result.json"
        input_write_task = asyncio.create_task(
            asyncio.to_thread(input_path.write_bytes, content)
        )
        await asyncio.shield(input_write_task)
        process = _OFFICE_PROCESS_CONTEXT.Process(
            target=_OFFICE_CONVERSION_TARGET,
            args=(str(input_path), str(result_path), filename),
            daemon=True,
        )
        bootstrap = _OfficeBootstrap(process, temporary_directory)
        threading.Thread(
            target=bootstrap.run,
            name="office-process-bootstrap",
            daemon=True,
        ).start()
        while not bootstrap.done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process_started = bootstrap.abandon()
                bootstrap_detached = not process_started
                raise OfficeConversionTimeoutError
            await asyncio.sleep(min(0.05, remaining))
        if bootstrap.error is not None:
            bootstrap_detached = True
            raise OfficeConversionError from bootstrap.error
        if not bootstrap.claim_started_process():
            raise OfficeConversionError
        process_started = True
        while process.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfficeConversionTimeoutError
            await asyncio.sleep(min(0.05, remaining))
        await asyncio.to_thread(process.join, 0)
        if process.exitcode not in (0, None):
            raise OfficeConversionError
        return await asyncio.to_thread(_read_office_child_result, result_path)
    except asyncio.CancelledError:
        if bootstrap is not None and not process_started:
            process_started = bootstrap.abandon()
            bootstrap_detached = not process_started
        if input_write_task is not None and not input_write_task.done():
            await asyncio.shield(input_write_task)
        raise
    except OfficeConversionTimeoutError:
        raise
    except OfficeConversionBusyError:
        raise
    except OfficeConversionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfficeConversionError from exc
    finally:
        if input_write_task is not None and not input_write_task.done():
            try:
                await asyncio.shield(input_write_task)
            except (OSError, RuntimeError):
                pass
        if bootstrap is not None and not process_started and not bootstrap.done.is_set():
            # Ownership stays with the bounded daemon bootstrap.  A late
            # successful start is killed there before its slot/temp are freed.
            process_started = bootstrap.abandon()
            bootstrap_detached = not process_started
        if bootstrap_detached:
            process = None
            temporary_directory = None
        if process is not None and process_started:
            try:
                await asyncio.shield(_cleanup_office_process(process))
                process_cleaned = True
            except (OfficeConversionError, OSError, ValueError):
                # Do not release the slot while a child might still be alive.
                logger.error("Office conversion child cleanup failed")
        elif process is not None:
            # ``start()`` can fail before a child exists.  Closing an
            # unstarted Process is safe and avoids leaking its bookkeeping.
            try:
                process.close()
            except (AssertionError, OSError, ValueError):
                pass
        if not bootstrap_detached and (
            process_cleaned or process is None or not process_started
        ):
            _OFFICE_CONVERSION_SLOTS.release()
        if temporary_directory is not None and (
            process is None or not process_started or process_cleaned
        ):
            await asyncio.to_thread(
                shutil.rmtree,
                temporary_directory,
                ignore_errors=True,
            )


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
            file_bytes = await file.read(DOCUMENT_UPLOAD_MAX_BYTES + 1)
            if len(file_bytes) > DOCUMENT_UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="ファイルサイズは 50 MB までです",
                )

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
                # Office conversion invokes parser libraries that may perform
                # blocking CPU/file work.  Keep it in a bounded pool and fail
                # fast when all workers are occupied.
                try:
                    result = await _convert_office_with_limits(
                        file_bytes,
                        filename,
                    )
                except OfficeConversionBusyError as exc:
                    raise HTTPException(
                        status_code=429,
                        detail="ファイル変換が混雑しています。しばらくしてから再試行してください",
                    ) from exc
                except OfficeConversionTimeoutError as exc:
                    raise HTTPException(
                        status_code=504,
                        detail="ファイル変換がタイムアウトしました",
                    ) from exc
                except OfficeConversionError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail="ファイル変換に失敗しました",
                    ) from exc

                if not result.get("success"):
                    raise HTTPException(status_code=400, detail="ファイル変換に失敗しました")

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
        except Exception:
            logger.exception("Failed to process document")
            raise HTTPException(
                status_code=500,
                detail="ドキュメント処理に失敗しました",
            )

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

            if ctx_type == StorageContextType.APP:
                from ...memory.models import App
                from ...services.app_service import AppAccessError, AppService

                session = await server._db_manager.get_session()
                try:
                    app = await session.get(App, ctx_id) if ctx_id else None
                    if app is None:
                        raise HTTPException(status_code=404, detail="App not found")
                    try:
                        await AppService().require_permission(
                            session,
                            app,
                            user_id=user_id,
                            required="viewer",
                            user_role=user_info.get("role"),
                        )
                    except AppAccessError as exc:
                        raise HTTPException(status_code=403, detail="Appへの権限がありません") from exc
                finally:
                    await session.close()

            if ctx_type == StorageContextType.PROJECT:
                if ctx_id is None:
                    raise HTTPException(status_code=400, detail="Project context id is required")
                from ...memory.project_repository import ProjectRepository

                session = await server._db_manager.get_session()
                try:
                    if not await ProjectRepository.has_permission(
                        session,
                        ctx_id,
                        user_id,
                        "read",
                    ):
                        raise HTTPException(
                            status_code=404,
                            detail="Storage context not found",
                        )
                finally:
                    await session.close()

            root_path, valid = get_context_root(
                ctx_type,
                ctx_id,
                user_id,
                create=False,
            )
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
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid context type: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to get storage usage: {e}")
            raise HTTPException(status_code=500, detail=str(e))
