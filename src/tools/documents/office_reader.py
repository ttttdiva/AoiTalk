"""
Office Document Reader - Convert Office files to Markdown using MarkItDown.

The Office formats handled here are ZIP containers (DOCX/XLSX/PPTX).  Before
passing an uploaded container to a parser, inspect its central directory so a
small ZIP bomb cannot expand into unbounded memory or disk work.
"""

from __future__ import annotations

import io
import logging
import re
import stat
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

ZIP_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})


class OfficeSecurityError(ValueError):
    """An Office container violates a resource or archive safety policy."""


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


def _preflight_zip_archive(content: bytes) -> None:
    """Validate a DOCX/XLSX/PPTX ZIP central directory before conversion."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise OfficeSecurityError("Office ZIPコンテナを読み取れません") from exc

    try:
        try:
            members = archive.infolist()
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise OfficeSecurityError("Office ZIPコンテナを読み取れません") from exc
        if len(members) > OFFICE_ARCHIVE_MAX_MEMBERS:
            raise OfficeSecurityError("Office ZIPのファイル数が上限を超えています")

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
            if uncompressed > OFFICE_ARCHIVE_MAX_MEMBER_BYTES:
                raise OfficeSecurityError("Office ZIPのファイルサイズが上限を超えています")
            total_uncompressed += uncompressed
            if total_uncompressed > OFFICE_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise OfficeSecurityError("Office ZIPの展開サイズが上限を超えています")

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
        raise OfficeSecurityError("変換結果のサイズが上限を超えています")


def convert_office_bytes_to_markdown(content: bytes, filename: str) -> Dict[str, Any]:
    """Convert Office file bytes to Markdown with archive/output guardrails."""

    try:
        from markitdown import MarkItDown

        file_extension = Path(filename).suffix.lower()
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
