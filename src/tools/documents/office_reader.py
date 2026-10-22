"""
Office Document Reader - Convert Office files to Markdown using MarkItDown.

The Office formats handled here are ZIP containers (DOCX/XLSX/PPTX).  Before
passing an uploaded container to a parser, inspect its central directory so a
small ZIP bomb cannot expand into unbounded memory or disk work.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict

logger = logging.getLogger(__name__)


# These limits are deliberately below the 50 MiB upload limit while allowing
# ordinary workbooks/presentations to contain large media and XML parts.
OFFICE_ARCHIVE_MAX_MEMBERS = 10_000
OFFICE_ARCHIVE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
OFFICE_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
OFFICE_ARCHIVE_MAX_COMPRESSION_RATIO = 100.0
OFFICE_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
OFFICE_CONVERSION_MAX_WORKERS = 1
OFFICE_CONVERSION_TIMEOUT_SECONDS = 30.0
OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS = 1.0
# JSON is emitted as UTF-8 and may grow beyond the decoded text size for
# escaped/non-ASCII characters.  The decoded output guard remains authoritative.
OFFICE_CONVERSION_RESULT_MAX_BYTES = OFFICE_MAX_OUTPUT_BYTES * 6 + 1024 * 1024
OFFICE_CONVERSION_WORKER_PATH = str(
    Path(__file__).resolve().with_name("office_conversion_worker.py")
)

ZIP_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
_OFFICE_CONVERSION_SLOT = threading.BoundedSemaphore(OFFICE_CONVERSION_MAX_WORKERS)


class OfficeSecurityError(ValueError):
    """An Office container violates a resource or archive safety policy."""


class OfficeSizeLimitError(OfficeSecurityError):
    """A well-formed container is only too large for the in-memory converter.

    Unlike encryption, symlink or traversal rejections, an XLSX in this state
    can still be read by the bounded streaming extractor.
    """


OFFICE_TIMEOUT_ERROR = "ファイル変換がタイムアウトしました"
OFFICE_OUTPUT_LIMIT_ERROR = "変換結果のサイズが上限を超えています"


def _unsafe_zip_member_path(name: str) -> bool:
    """Reject absolute, traversal, drive-qualified, or malformed member paths."""

    normalized = name.replace("\\", "/")
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
    ):
        return True
    if "//" in normalized:
        return True
    parts = PurePosixPath(normalized).parts
    return any(part in {"", ".", ".."} for part in parts)


def _preflight_zip_archive(
    content: bytes,
    *,
    max_members: int | None = None,
    max_member_bytes: int | None = None,
    max_total_bytes: int | None = None,
) -> None:
    """Validate a DOCX/XLSX/PPTX ZIP central directory before conversion."""

    max_members = OFFICE_ARCHIVE_MAX_MEMBERS if max_members is None else max_members
    max_member_bytes = OFFICE_ARCHIVE_MAX_MEMBER_BYTES if max_member_bytes is None else max_member_bytes
    max_total_bytes = (OFFICE_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES if max_total_bytes is None
                       else max_total_bytes)

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise OfficeSecurityError("Office ZIPコンテナを読み取れません") from exc

    try:
        try:
            members = archive.infolist()
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise OfficeSecurityError("Office ZIPコンテナを読み取れません") from exc
        if len(members) > max_members:
            raise OfficeSizeLimitError("Office ZIPのファイル数が上限を超えています")

        seen_names: set[str] = set()
        total_uncompressed = 0
        for member in members:
            name = str(member.filename)
            normalized = name.replace("\\", "/")
            if _unsafe_zip_member_path(name):
                raise OfficeSecurityError("Office ZIPに安全でないファイル名があります")
            if normalized in seen_names:
                raise OfficeSecurityError("Office ZIPに重複したファイル名があります")
            seen_names.add(normalized)

            # Bit 0 means that the member is encrypted.  MarkItDown should
            # never be handed encrypted content or asked to prompt for a key.
            if member.flag_bits & 0x1:
                raise OfficeSecurityError("暗号化されたOffice ZIPは利用できません")

            mode = (member.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise OfficeSecurityError("Office ZIPのシンボリックリンクは利用できません")

            uncompressed = int(member.file_size)
            compressed = int(member.compress_size)
            if uncompressed < 0 or compressed < 0:
                raise OfficeSecurityError("Office ZIPのサイズ情報が不正です")
            if uncompressed > max_member_bytes:
                raise OfficeSizeLimitError("Office ZIPのファイルサイズが上限を超えています")
            total_uncompressed += uncompressed
            if total_uncompressed > max_total_bytes:
                raise OfficeSizeLimitError("Office ZIPの展開サイズが上限を超えています")

            if uncompressed and compressed == 0:
                raise OfficeSecurityError("Office ZIPの圧縮率が安全な範囲を超えています")
            if compressed and uncompressed / compressed > OFFICE_ARCHIVE_MAX_COMPRESSION_RATIO:
                raise OfficeSecurityError("Office ZIPの圧縮率が安全な範囲を超えています")
    except OfficeSecurityError:
        raise
    except (OSError, OverflowError, ValueError, zipfile.BadZipFile) as exc:
        raise OfficeSecurityError("Office ZIPのサイズ情報が不正です") from exc
    finally:
        archive.close()


def _check_output_size(text_content: str) -> None:
    if len(text_content.encode("utf-8", errors="replace")) > OFFICE_MAX_OUTPUT_BYTES:
        raise OfficeSizeLimitError(OFFICE_OUTPUT_LIMIT_ERROR)


def _convert_office_in_process(content: bytes, filename: str) -> Dict[str, Any]:
    """Convert one document inside the dedicated conversion worker."""

    file_extension = Path(filename).suffix.lower()
    try:
        from markitdown import MarkItDown

        if file_extension in ZIP_OFFICE_EXTENSIONS:
            _preflight_zip_archive(content)

        md = MarkItDown()
        result = md.convert_stream(io.BytesIO(content), file_extension=file_extension)
        text_content = result.text_content if result else ""
        if not isinstance(text_content, str) or not text_content:
            return {
                "success": False,
                "error": "変換結果が空でした",
            }
        _check_output_size(text_content)
        return {
            "success": True,
            "content": text_content,
            "filename": filename,
        }

    except ImportError:
        logger.error("markitdown is not installed")
        return {
            "success": False,
            "error": "markitdown がインストールされていません",
        }
    except OfficeSecurityError as exc:
        logger.warning("Office conversion rejected (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": str(exc),
        }
    except Exception as exc:
        # Keep parser/library details out of the client response and logs.
        logger.error("Office conversion failed (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": "ファイル変換に失敗しました",
        }


def _safe_conversion_result(result: object) -> Dict[str, Any]:
    """Keep worker output bounded and compatible with the old result schema."""

    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        return {"success": False, "error": "ファイル変換に失敗しました"}
    if not result["success"]:
        error = result.get("error")
        if isinstance(error, str) and error and len(error) <= 4096:
            return {"success": False, "error": error}
        return {"success": False, "error": "ファイル変換に失敗しました"}

    content = result.get("content")
    if not isinstance(content, str) or not content:
        return {"success": False, "error": "ファイル変換に失敗しました"}
    try:
        _check_output_size(content)
    except (OfficeSecurityError, MemoryError, UnicodeError):
        return {"success": False, "error": "ファイル変換に失敗しました"}
    normalized: Dict[str, Any] = {"success": True, "content": content}
    filename = result.get("filename")
    if isinstance(filename, str) and len(filename) <= 4096:
        normalized["filename"] = filename
    return normalized


def _windows_process_options() -> Dict[str, Any]:
    """Return subprocess options that never open a visible Windows console."""

    options: Dict[str, Any] = {
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        # ``python -m`` must resolve the package even when the API was
        # launched from a directory other than the repository root.
        "cwd": str(Path(__file__).resolve().parents[3]),
    }
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup_info
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Terminate a worker and synchronously reap it, including timeout races."""

    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        else:
            process.wait(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        # ``communicate`` closes the pipe handles after the child has exited.
        # A second timeout is defensive against a platform pipe race.
        try:
            process.communicate(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _conversion_request(content: bytes, filename: str) -> bytes:
    """Build a length-delimited stdin request without exposing a source path."""

    header = json.dumps(
        {"filename": filename, "size": len(content)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return header + b"\n" + content


def _run_office_conversion_subprocess(content: bytes, filename: str) -> Dict[str, Any]:
    """Run MarkItDown in one bounded, killable child process."""

    process: subprocess.Popen[bytes] | None = None
    deadline_timer: threading.Timer | None = None
    deadline_expired = threading.Event()
    deadline = time.monotonic() + OFFICE_CONVERSION_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", OFFICE_CONVERSION_WORKER_PATH],
            **_windows_process_options(),
        )
        request = _conversion_request(content, filename)
        remaining = max(0.0, deadline - time.monotonic())

        def _kill_at_deadline() -> None:
            if process is None or process.poll() is not None:
                return
            deadline_expired.set()
            try:
                process.kill()
            except (OSError, ValueError):
                pass

        # ``communicate(input=...)`` may block while writing stdin on Windows.
        # The timer also kills a child that never drains stdin, so its pipe
        # write cannot outlive the absolute conversion deadline.
        deadline_timer = threading.Timer(remaining, _kill_at_deadline)
        deadline_timer.daemon = True
        deadline_timer.start()
        stdout, _ = process.communicate(input=request, timeout=remaining)
        if deadline_expired.is_set():
            _terminate_and_reap(process)
            return {"success": False, "error": OFFICE_TIMEOUT_ERROR}
        if process.returncode != 0:
            return {"success": False, "error": "ファイル変換に失敗しました"}
        if not isinstance(stdout, bytes) or not stdout:
            return {"success": False, "error": "ファイル変換に失敗しました"}
        if len(stdout) > OFFICE_CONVERSION_RESULT_MAX_BYTES:
            return {"success": False, "error": "ファイル変換に失敗しました"}
        result = json.loads(stdout.decode("utf-8"))
        return _safe_conversion_result(result)
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate_and_reap(process)
        return {"success": False, "error": OFFICE_TIMEOUT_ERROR}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if deadline_expired.is_set():
            if process is not None:
                _terminate_and_reap(process)
            return {"success": False, "error": OFFICE_TIMEOUT_ERROR}
        logger.error("Office conversion worker failed (%s)", type(exc).__name__)
        return {"success": False, "error": "ファイル変換に失敗しました"}
    except Exception as exc:
        if deadline_expired.is_set():
            if process is not None:
                _terminate_and_reap(process)
            return {"success": False, "error": OFFICE_TIMEOUT_ERROR}
        logger.error("Office conversion worker failed (%s)", type(exc).__name__)
        return {"success": False, "error": "ファイル変換に失敗しました"}
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()
            deadline_timer.join(timeout=OFFICE_CONVERSION_JOIN_TIMEOUT_SECONDS)
        if process is not None and process.poll() is None:
            _terminate_and_reap(process)


def convert_office_bytes_to_markdown(content: bytes, filename: str) -> Dict[str, Any]:
    """Convert Office file bytes to Markdown with bounded process isolation."""

    try:
        file_extension = Path(filename).suffix.lower()
        if file_extension in ZIP_OFFICE_EXTENSIONS:
            # Keep archive inspection in the caller so malformed or hostile
            # containers are rejected before a parser process is started.
            _preflight_zip_archive(content)

        acquired = _OFFICE_CONVERSION_SLOT.acquire(
            timeout=max(0.0, OFFICE_CONVERSION_TIMEOUT_SECONDS)
        )
        if not acquired:
            return {"success": False, "error": "ファイル変換が混雑しています"}
        try:
            return _run_office_conversion_subprocess(content, filename)
        finally:
            _OFFICE_CONVERSION_SLOT.release()
    except OfficeSecurityError as exc:
        logger.warning("Office conversion rejected (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": str(exc),
        }
    except Exception as exc:
        # Keep parser/library details out of the client response and logs.
        logger.error("Office conversion failed (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": "ファイル変換に失敗しました",
        }


def convert_office_file_to_markdown(file_path: str) -> Dict[str, Any]:
    """Read an Office file and convert it to Markdown."""

    path = Path(file_path)
    if not path.exists():
        return {
            "success": False,
            "error": f"ファイルが見つかりません: {file_path}",
        }

    try:
        content = path.read_bytes()
        return convert_office_bytes_to_markdown(content, path.name)
    except Exception as exc:
        logger.error("Failed to read office file (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": "ファイル読み込みに失敗しました",
        }


# Supported extensions
SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".pdf"}


def is_supported(filename: str) -> bool:
    """Check if the file extension is supported."""

    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
