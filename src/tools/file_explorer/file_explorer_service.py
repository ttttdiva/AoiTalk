"""
File Explorer Service - Core logic for unified file management.

Provides comprehensive file operations with directory structure support.
Replaces the old user_files and integrates document handling.
"""

import base64
import bz2
from contextlib import contextmanager
from contextvars import ContextVar
import fnmatch
import filecmp
import gzip
import io
import json
import lzma
import mimetypes
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

import py7zr
from py7zr.io import Py7zIO, WriterFactory

from ..text_content import TextContentError, read_safe_text

# Constants
SHORTCUT_EXTENSION = ".lnk"
ARCHIVE_SUFFIXES = (
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tbz2",
    ".tgz",
    ".txz",
    ".7z",
    ".tar",
    ".zip",
    ".bz2",
    ".gz",
    ".xz",
)
MAX_ARCHIVE_ENTRIES = 10000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".tsv",
    ".log",
    ".toml",
    ".py",
    ".bat",
    ".cmd",
    ".sh",
    ".ps1",
    ".vbs",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".rb",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".properties",
    ".gitignore",
    ".ttl",
    ".nt",
    ".n3",
    ".trig",
    ".rq",
    ".sparql",
    ".rdf",
    ".owl",
    ".graphql",
    ".gql",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".pdf"}
MAIL_EXTENSIONS = {".eml", ".msg"}
# チャット添付のアップロード上限と揃えた、テキスト/Office読取上限
MAX_READ_FILE_SIZE = 50 * 1024 * 1024

_WORKSPACE_ROOT_OVERRIDE: ContextVar[Optional[Path]] = ContextVar(
    "aoitalk_workspace_root_override", default=None
)

# フォルダごとのサムネイル設定を保存する隠しファイル名。中身はフォルダ基準の相対パス or 絶対パス
FOLDER_THUMB_FILE = ".folder-thumb"


def _resolve_shortcut(lnk_path: Path) -> Optional[Path]:
    """Resolve a Windows .lnk shortcut to its target Path (lazy import to avoid cycle)"""
    try:
        from src.tools.absolute_filer_paths import resolve_lnk_target
    except ImportError:
        return None
    try:
        return resolve_lnk_target(lnk_path)
    except Exception:
        return None


def get_root_dir() -> Path:
    """Get the workspace root directory (user_files)"""
    override = _WORKSPACE_ROOT_OVERRIDE.get()
    files_dir = override or Path(
        os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    )
    path = Path(files_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


@contextmanager
def workspace_root_context(root: Path | str):
    """Run file-explorer operations against one resolved workspace root.

    Project routes may receive a root resolved by the server at startup.  A
    context variable keeps that value request-local, so file I/O does not
    silently fall back to a changed environment variable in another request.
    """

    selected = Path(root).expanduser().resolve()
    selected.mkdir(parents=True, exist_ok=True)
    token = _WORKSPACE_ROOT_OVERRIDE.set(selected)
    try:
        yield selected
    finally:
        _WORKSPACE_ROOT_OVERRIDE.reset(token)


def _is_link_or_reparse(path: Path) -> bool:
    """Return true for POSIX symlinks and Windows junction/reparse points."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        item_stat = path.lstat()
        return bool(
            getattr(item_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except (OSError, RuntimeError):
        return True


def is_safe_workspace_path(root: Path | str, candidate: Path | str) -> bool:
    """Reject lexical escapes and symlink/reparse-point components.

    ``Path.resolve`` alone follows links, including dangling links after a
    failed existence check.  Inspect every existing component with lstat so
    read and write paths cannot cross the project workspace boundary.
    """

    root_path = Path(root).expanduser().resolve()
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    # abspath normalizes ``..`` without following symlinks.
    lexical = Path(os.path.abspath(os.fspath(candidate_path)))
    try:
        lexical.relative_to(root_path)
    except ValueError:
        return False

    current = root_path
    last_existing = root_path
    try:
        for component in lexical.relative_to(root_path).parts:
            current = current / component
            try:
                item_stat = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(item_stat.st_mode) or bool(
                getattr(item_stat, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ) or (
                callable(getattr(current, "is_junction", None))
                and current.is_junction()
            ):
                return False
            last_existing = current

        real_root = root_path.resolve(strict=True)
        real_existing = last_existing.resolve(strict=True)
        real_existing.relative_to(real_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _find_folder_preview(
    folder: Path, root: Optional[Path] = None
) -> Tuple[Optional[str], bool]:
    """フォルダの代表サムネ用ファイル（画像）パスを返す。

    優先順位:
      1. フォルダ直下の .folder-thumb に書かれたパス（フォルダ基準の相対パス or 絶対パス）
      2. フォルダ直下の最初の画像（名前順。サブフォルダは辿らない）

    Returns:
        (preview_path, has_explicit_thumb) のタプル。
        - preview_path: フロントエンドで画像サムネAPI に渡せるパス文字列。
          ワークスペース内ならルート基準の相対、外なら絶対。見つからなければ None。
        - has_explicit_thumb: `.folder-thumb` で明示的に設定されているなら True。
    """
    if root is None:
        try:
            root = get_root_dir()
        except Exception:
            root = None

    def _to_serve_path(target: Path) -> Optional[str]:
        if root is not None and not is_safe_workspace_path(root, target):
            return None
        try:
            if not target.exists() or not target.is_file():
                return None
        except OSError:
            return None
        if root is not None:
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        return str(target).replace("\\", "/")

    # 1. .folder-thumb を確認
    thumb_file = folder / FOLDER_THUMB_FILE
    try:
        if thumb_file.is_file():
            raw = thumb_file.read_text(encoding="utf-8").strip()
            if raw:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = (folder / raw).resolve()
                resolved = _to_serve_path(candidate)
                if resolved is not None:
                    return resolved, True
    except OSError:
        pass

    # 2. 最初の画像ファイル（名前順、先頭 1 件で打ち切り）
    try:
        entries = []
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                ext = Path(entry.name).suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    entries.append(entry.name)
        if entries:
            entries.sort(key=str.lower)
            first = folder / entries[0]
            served = _to_serve_path(first)
            if served is not None:
                return served, False
    except OSError:
        return None, False

    return None, False


def set_folder_thumbnail(
    folder_path: str, target_path: str, is_admin: bool = False
) -> Dict[str, Any]:
    """フォルダに `.folder-thumb` を書き込み、代表サムネを明示設定する。

    target_path は folder_path と同じ解決方式（ワークスペース相対 or 絶対）。
    .folder-thumb には folder_path からの相対パス（可能なら）を書き込み、
    フォルダ移動・コピーでも設定が追従するようにする。
    """
    folder, valid = _resolve_path(folder_path, is_admin=is_admin)
    if not valid or not folder.exists() or not folder.is_dir():
        return {"success": False, "error": "フォルダが見つかりません"}

    target, target_valid = _resolve_path(target_path, is_admin=is_admin)
    if not target_valid or not target.exists() or not target.is_file():
        return {"success": False, "error": "対象ファイルが見つかりません"}

    # なるべく相対パスで書き込む
    try:
        rel = target.relative_to(folder)
        stored = str(rel).replace("\\", "/")
    except ValueError:
        stored = str(target).replace("\\", "/")

    try:
        (folder / FOLDER_THUMB_FILE).write_text(stored, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": f"書き込みに失敗: {e}"}

    return {"success": True, "folder_path": folder_path, "stored": stored}


def clear_folder_thumbnail(folder_path: str, is_admin: bool = False) -> Dict[str, Any]:
    """フォルダの `.folder-thumb` を削除して明示設定を解除する（無ければ成功扱い）。"""
    folder, valid = _resolve_path(folder_path, is_admin=is_admin)
    if not valid or not folder.exists() or not folder.is_dir():
        return {"success": False, "error": "フォルダが見つかりません"}

    thumb_file = folder / FOLDER_THUMB_FILE
    try:
        if thumb_file.exists():
            thumb_file.unlink()
    except OSError as e:
        return {"success": False, "error": f"削除に失敗: {e}"}

    return {"success": True, "folder_path": folder_path}


def _sanitize_name(name: str) -> str:
    """Sanitize file/directory name to prevent path traversal"""
    # Remove path separators and dangerous characters
    name = re.sub(r'[/\\:*?"<>|]', "", name)
    name = name.strip(". ")
    if len(name) > 200:
        name = name[:200]
    if not name:
        name = "unnamed"
    return name


def _sanitize_relative_file_path(path: str) -> Tuple[List[str], str]:
    """Sanitize a browser-supplied relative file path for folder uploads."""
    raw_parts = path.replace("\\", "/").split("/")
    safe_parts = [
        _sanitize_name(part)
        for part in raw_parts
        if part and part not in {".", ".."}
    ]

    if not safe_parts:
        return [], "unnamed_file"

    return safe_parts[:-1], safe_parts[-1]


def _is_absolute_input(path: str) -> bool:
    """Return whether ``path`` is absolute on the current or peer platform.

    The explorer accepts paths returned by a browser, so a Windows drive or
    UNC path can arrive even when the caller is running on a POSIX host (and
    vice versa).  ``Path.is_absolute`` handles the native platform while the
    drive-letter check keeps the cross-platform wire format working.
    """

    if not path:
        return False
    normalized = path.replace("\\", "/")
    return bool(
        Path(path).is_absolute()
        or Path(normalized).is_absolute()
        # Keep the explorer's cross-platform wire semantics: a leading
        # separator denotes a root-relative/UNC destination even when the
        # host ``pathlib`` implementation does not consider it absolute
        # (for example ``Path('/tmp').is_absolute()`` on Windows).
        or path.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", path)
    )


def _lexical_path(path: Path | str) -> Path:
    """Return an absolute path without following symlinks or reparse points."""

    return Path(os.path.abspath(os.fspath(path)))


def _existing_components_are_safe(path: Path | str) -> bool:
    """Reject symlink/junction/reparse components in an existing path.

    Missing components are allowed because upload creates them.  The caller
    must run this check again after creating parent directories to close the
    usual missing-parent race.
    """

    lexical = _lexical_path(path)
    current = Path(lexical.anchor) if lexical.anchor else Path()
    parts = lexical.parts[1:] if lexical.anchor else lexical.parts
    for component in parts:
        current = current / component
        try:
            current.lstat()
        except FileNotFoundError:
            break
        except (OSError, RuntimeError):
            return False
        if _is_link_or_reparse(current):
            return False
    return True


def _is_safe_upload_boundary_path(
    boundary: Path | str,
    candidate: Path | str,
) -> bool:
    """Validate an upload target against its explicit destination boundary.

    ``boundary`` is either the workspace root (relative uploads) or the
    administrator-selected absolute destination directory.  The latter is
    deliberately not treated as permission to leave that directory: the
    filename and any browser-supplied relative subdirectories must remain
    below it.  Existing link/reparse components are rejected on every path
    component, including the administrator-selected directory itself.
    """

    boundary_lexical = _lexical_path(boundary)
    candidate_lexical = _lexical_path(candidate)
    try:
        candidate_lexical.relative_to(boundary_lexical)
    except ValueError:
        return False

    if not _existing_components_are_safe(boundary_lexical):
        return False
    if not _existing_components_are_safe(candidate_lexical):
        return False

    # Re-check the resolved relationship after the component scan.  This
    # catches a link introduced between the lexical scan and this check.
    try:
        candidate_lexical.resolve(strict=False).relative_to(
            boundary_lexical.resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _resolve_upload_target_context(
    path: str,
    filename: str,
    is_admin: bool = False,
    *,
    allow_external: bool = False,
) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Resolve an upload path and return the boundary used for revalidation."""

    raw_path = str(path or "")
    workspace_root = get_root_dir()

    # Absolute administrator destinations are checked lexically before
    # ``Path.resolve`` can follow a link to another tree.  Relative paths are
    # already checked by ``_resolve_path`` against the workspace root.
    absolute_path = _is_absolute_input(raw_path)
    if is_admin and absolute_path:
        if not _existing_components_are_safe(_lexical_path(raw_path)):
            return None, None, "無効なアップロード先です"

    target_dir, valid = _resolve_path(raw_path, is_admin=is_admin)
    if not valid:
        return None, None, "無効なパスです"

    safe_dirs, safe_name = _sanitize_relative_file_path(filename)
    file_path = target_dir.joinpath(*safe_dirs, safe_name)

    # The destination directory is the boundary for an administrator's
    # absolute upload.  All other calls remain workspace-root constrained;
    # project storage therefore keeps its existing strict default.
    boundary = (
        target_dir
        if allow_external and is_admin and absolute_path
        else workspace_root
    )
    if not _is_safe_upload_boundary_path(boundary, file_path):
        return None, None, "無効なアップロード先です"
    return file_path, boundary, None


def _resolve_path(relative_path: str, is_admin: bool = False) -> Tuple[Path, bool]:
    """
    Resolve a relative path to absolute path within workspace.

    Args:
        relative_path: Relative path from workspace root, or absolute path for admin
        is_admin: If True, allow access to any path on the system

    Returns:
        Tuple of (resolved_path, is_valid)
    """
    root = get_root_dir()

    # Git internals are never exposed through the filer, including admin mode.
    # ゴミ箱(.trash)も同様に、通常のファイル操作経路からは一切見せない。
    normalized_parts = [part.casefold() for part in relative_path.replace("\\", "/").split("/")]
    if ".git" in normalized_parts or TRASH_DIR_NAME in normalized_parts:
        return root, False

    if not relative_path or relative_path == "/":
        return root, True

    # Admin mode: allow absolute paths (including Windows UNC paths).
    if is_admin and _is_absolute_input(relative_path):
        target = Path(relative_path).resolve()
        return target, True

    if _is_absolute_input(relative_path):
        return root, False

    # Normalize path separators and remove leading slashes
    clean_path = relative_path.replace("\\", "/").strip("/")
    project_boundary: Optional[Path] = None
    path_parts = [part for part in clean_path.split("/") if part]
    if (
        len(path_parts) >= 2
        and path_parts[0].casefold() == "_projects"
        and path_parts[1].casefold().startswith("project_")
    ):
        lexical_project_root = root / path_parts[0] / path_parts[1]
        try:
            # A project storage root must itself be canonical.  Otherwise a
            # symlinked project directory could alias another project's ACL.
            if lexical_project_root.is_symlink():
                return root, False
            project_boundary = lexical_project_root.resolve()
        except OSError:
            return root, False

    lexical_target = root / clean_path
    if not is_safe_workspace_path(root, lexical_target):
        return root, False

    # Resolve to absolute path only after the component-level link check.
    target = lexical_target.resolve()

    # Security check: ensure path is within root (skip for admin)
    if is_admin:
        if project_boundary is not None:
            try:
                target.relative_to(project_boundary)
            except ValueError:
                return root, False
        return target, True

    try:
        target.relative_to(root)
        if project_boundary is not None:
            target.relative_to(project_boundary)
        return target, True
    except ValueError:
        return root, False


def resolve_workspace_path(path: str = "", is_admin: bool = False) -> Tuple[Path, bool]:
    """workspaceルート基準のパス解決を外部モジュールへ公開するラッパー。

    統合ファイルツール（read_file / list_directory / search_files）が
    ファイラーと同じ境界チェックを使うために利用する。
    """
    return _resolve_path(path, is_admin=is_admin)


def resolve_upload_target(
    path: str,
    filename: str,
    is_admin: bool = False,
    *,
    allow_external: bool = False,
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve an upload destination with the same validation as the writer.

    External absolute destinations require an explicit ``allow_external``
    opt-in in addition to administrator mode.  Project API callers therefore
    retain the workspace-root boundary by default.
    """
    file_path, _boundary, error = _resolve_upload_target_context(
        path,
        filename,
        is_admin=is_admin,
        allow_external=allow_external,
    )
    return file_path, error


def _format_size(size_bytes: int) -> str:
    """Format file size for display"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_path_for_response(path: Path, root: Optional[Path] = None) -> str:
    """Return workspace-relative paths inside root, absolute paths outside root."""
    if root is None:
        root = get_root_dir()
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _remove_readonly(function, path, _exc_info) -> None:
    """Retry removal after making Windows read-only files writable."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _next_available_path(parent: Path, filename: str) -> Path:
    """Return a non-existing path in parent, preserving the requested extension."""
    def occupied(path: Path) -> bool:
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True

    candidate = parent / filename
    if not occupied(candidate):
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        next_candidate = parent / f"{stem}_{counter}{suffix}"
        if not occupied(next_candidate):
            return next_candidate
        counter += 1


def _text_extension_key(path: Path) -> str:
    """拡張子を返す。`.env` のような拡張子なしドットファイルはファイル名を返す。"""
    return path.suffix.lower() or path.name.lower()


def _get_file_type(path: Path) -> str:
    """Determine file type category"""
    ext = _text_extension_key(path)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in TEXT_EXTENSIONS:
        return "text"
    elif ext in OFFICE_EXTENSIONS:
        return "office"
    elif ext in MAIL_EXTENSIONS:
        return "mail"
    elif ext in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
        return "video"
    elif ext in {".mp3", ".m4a", ".flac", ".wav", ".ogg"}:
        return "audio"
    else:
        return "binary"


def _binary_preview(target: Path, rel_path: str, *, reason: str = "") -> Dict[str, Any]:
    """Return the UI-compatible non-content response for unsafe binary bytes."""

    result: Dict[str, Any] = {
        "success": True,
        "type": "binary",
        "path": rel_path,
        "message": "このファイルはバイナリ形式のためテキストとして読み取れません",
        "extension": target.suffix.lower(),
        "error_code": "binary_file",
    }
    if reason:
        result["classification_reason"] = reason
    return result


def _get_icon(path: Path, is_dir: bool = False) -> str:
    """Get icon emoji for file/directory"""
    if is_dir:
        return "📁"

    ext = path.suffix.lower()
    icons = {
        ".pdf": "📕",
        ".docx": "📘",
        ".xlsx": "📗",
        ".pptx": "📙",
        ".txt": "📄",
        ".md": "📝",
        ".json": "📋",
        ".yaml": "📋",
        ".yml": "📋",
        ".py": "🐍",
        ".js": "🟨",
        ".ts": "🔷",
        ".html": "🌐",
        ".css": "🎨",
        ".jpg": "🖼️",
        ".jpeg": "🖼️",
        ".png": "🖼️",
        ".gif": "🖼️",
        ".webp": "🖼️",
        ".mp4": "🎬",
        ".mkv": "🎬",
        ".webm": "🎬",
        ".mp3": "🎵",
        ".m4a": "🎵",
        ".flac": "🎵",
        ".zip": "📦",
        ".rar": "📦",
        ".7z": "📦",
    }
    return icons.get(ext, "📄")


# ── Core Operations ─────────────────────────────────────────────────


def _list_drives() -> Dict[str, Any]:
    """List available Windows drives (admin only).

    Returns:
        Dict with drive list as directories
    """
    import string

    directories = []

    for letter in string.ascii_uppercase:
        drive_path = f"{letter}:/"
        drive = Path(drive_path)
        if drive.exists():
            try:
                directories.append(
                    {
                        "name": f"{letter}:",
                        "path": f"{letter}:/",
                        "icon": "💾",
                        "item_count": 0,
                        "modified_at": None,
                    }
                )
            except (PermissionError, OSError):
                continue

    return {
        "success": True,
        "current_path": "__drives__",
        "parent_path": None,
        "can_go_up": False,
        "directories": directories,
        "files": [],
        "total_items": len(directories),
        "is_admin_mode": True,
    }


def list_directory(path: str = "", is_admin: bool = False) -> Dict[str, Any]:
    """
    List contents of a directory.

    Args:
        path: Relative path from workspace root (empty for root), or absolute path for admin
              Special path "__drives__" returns list of Windows drives (admin only)
        is_admin: If True, allow access to any path on the system

    Returns:
        Dict with directories and files
    """
    # Special case: list Windows drives (admin only)
    if is_admin and path == "__drives__":
        return _list_drives()

    target, valid = _resolve_path(path, is_admin=is_admin)

    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists():
        # ワークスペース内のパスなら自動作成して空ディレクトリを返す（初回アクセス時）
        root = get_root_dir()
        try:
            target.relative_to(root)
            target.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError):
            return {"success": False, "error": f"パス「{path}」が見つかりません"}

    if not target.is_dir():
        return {"success": False, "error": "指定されたパスはディレクトリではありません"}

    root = get_root_dir()
    directories: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []

    # Check if we're outside the user_files root (admin mode)
    is_outside_root = False
    try:
        target.relative_to(root)
    except ValueError:
        is_outside_root = True

    try:
        for item in sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            if item.name.startswith("."):
                continue

            # Never expose metadata or navigation for aliases inside a
            # project workspace.  ``stat()`` would otherwise follow a link.
            if not is_outside_root and _is_link_or_reparse(item):
                continue

            try:
                stat = item.stat()
            except (PermissionError, OSError):
                # Skip items we can't access
                continue

            # Use absolute path when outside root (admin mode)
            if is_outside_root:
                item_path = str(item).replace("\\", "/")
            else:
                item_path = str(item.relative_to(root)).replace("\\", "/")

            # Resolve .lnk shortcuts (keep item_path as the .lnk location so serving
            # routes through resolve_file_path which re-resolves on demand)
            shortcut_target: Optional[Path] = None
            if item.is_file() and item.suffix.lower() == SHORTCUT_EXTENSION:
                resolved = _resolve_shortcut(item)
                if resolved is None or not resolved.exists():
                    # Broken shortcut — skip so we don't show a dead icon
                    continue
                shortcut_target = resolved

            # Classify as directory if the item (or its resolved target) is a directory
            effective = shortcut_target if shortcut_target is not None else item
            if effective.is_dir():
                # For folder shortcuts, navigation should go to the target absolute path
                if shortcut_target is not None:
                    nav_path = str(shortcut_target).replace("\\", "/")
                else:
                    nav_path = item_path
                try:
                    item_count = sum(
                        1 for _ in effective.iterdir() if not _.name.startswith(".")
                    )
                except (PermissionError, OSError):
                    item_count = 0

                dir_entry: Dict[str, Any] = {
                    "name": item.name,
                    "path": nav_path,
                    "icon": "📁",
                    "item_count": item_count,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
                if shortcut_target is not None:
                    dir_entry["is_shortcut"] = True
                    dir_entry["shortcut_path"] = item_path
                preview, has_explicit = _find_folder_preview(effective, root)
                if preview is not None:
                    dir_entry["preview_path"] = preview
                    if has_explicit:
                        dir_entry["has_explicit_thumb"] = True
                directories.append(dir_entry)
            else:
                try:
                    target_stat = (
                        effective.stat() if shortcut_target is not None else stat
                    )
                except (PermissionError, OSError):
                    target_stat = stat
                extension = effective.suffix.lower()
                file_entry: Dict[str, Any] = {
                    "name": item.name,
                    "path": item_path,
                    "icon": _get_icon(effective),
                    "type": _get_file_type(effective),
                    "extension": extension,
                    "size_bytes": target_stat.st_size,
                    "size_display": _format_size(target_stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
                if shortcut_target is not None:
                    file_entry["is_shortcut"] = True
                    file_entry["shortcut_target"] = str(shortcut_target).replace(
                        "\\", "/"
                    )
                files.append(file_entry)

        # Compute current and parent paths
        if is_outside_root:
            current_path = str(target).replace("\\", "/")
            # At drive root, go to drive list
            if target.parent == target:
                parent_path = "__drives__"
                can_go_up = True
            else:
                parent_path = str(target.parent).replace("\\", "/")
                can_go_up = True
        else:
            current_path = str(target.relative_to(root)).replace("\\", "/")
            if current_path == ".":
                current_path = ""

            if target != root:
                # Inside user_files, navigating within
                parent = target.parent
                parent_rel = str(parent.relative_to(root)).replace("\\", "/")
                parent_path = "" if parent_rel == "." else parent_rel
                can_go_up = True
            elif is_admin:
                # Admin at user_files root - can go up to parent of user_files
                parent_path = str(root.parent).replace("\\", "/")
                can_go_up = True
            else:
                # Regular user at user_files root - cannot go up
                parent_path = None
                can_go_up = False

        return {
            "success": True,
            "current_path": current_path,
            "parent_path": parent_path,
            "can_go_up": can_go_up,
            "directories": directories,
            "files": files,
            "total_items": len(directories) + len(files),
            "is_admin_mode": is_outside_root or is_admin,
        }

    except Exception as e:
        return {"success": False, "error": f"ディレクトリの読み取りに失敗: {str(e)}"}


def create_directory(path: str, name: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Create a new directory.

    Args:
        path: Parent directory path
        name: New directory name
    """
    parent, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not parent.exists() or not parent.is_dir():
        return {"success": False, "error": "親ディレクトリが存在しません"}

    safe_name = _sanitize_name(name)
    new_dir = parent / safe_name

    if new_dir.exists():
        return {"success": False, "error": f"「{safe_name}」は既に存在します"}

    try:
        new_dir.mkdir(parents=True, exist_ok=False)
        root = get_root_dir()
        rel_path = _format_path_for_response(new_dir, root)

        return {
            "success": True,
            "message": f"フォルダ「{safe_name}」を作成しました",
            "name": safe_name,
            "path": rel_path,
        }
    except Exception as e:
        return {"success": False, "error": f"フォルダの作成に失敗: {str(e)}"}


def upload_file_stream(
    path: str,
    filename: str,
    content: BinaryIO,
    is_admin: bool = False,
    allow_overwrite: bool = True,
    *,
    allow_external: bool = False,
) -> Dict[str, Any]:
    """
    Upload a file to the specified directory.

    Args:
        path: Target directory path
        filename: Name for the uploaded file
        content: Binary stream positioned at the start of the file
    """
    file_path, boundary, error = _resolve_upload_target_context(
        path,
        filename,
        is_admin=is_admin,
        allow_external=allow_external,
    )
    if file_path is None:
        return {"success": False, "error": error or "無効なアップロード先です"}

    safe_name = file_path.name
    root = get_root_dir()

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if boundary is None or not _is_safe_upload_boundary_path(boundary, file_path):
            return {"success": False, "error": "無効なアップロード先です"}
        if not allow_overwrite and file_path.exists():
            return {
                "success": False,
                "error": f"ファイル「{safe_name}」は既に存在します",
                "code": "already_exists",
            }
        temp_path: Optional[Path] = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=".upload-", suffix=".tmp", dir=file_path.parent
            )
            temp_path = Path(temp_name)
            size_bytes = 0
            with os.fdopen(fd, "wb") as destination:
                while True:
                    chunk = content.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    size_bytes += len(chunk)
            if allow_overwrite:
                os.replace(temp_path, file_path)
            else:
                try:
                    # Publish atomically without replacing a file created after
                    # the initial existence check.
                    os.link(temp_path, file_path)
                except FileExistsError:
                    return {
                        "success": False,
                        "error": f"ファイル「{safe_name}」は既に存在します",
                        "code": "already_exists",
                    }
                temp_path.unlink()
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        rel_path = _format_path_for_response(file_path, root)

        return {
            "success": True,
            "message": f"ファイル「{safe_name}」をアップロードしました",
            "name": safe_name,
            "path": rel_path,
            "size_bytes": size_bytes,
            "size_display": _format_size(size_bytes),
        }
    except Exception as e:
        return {"success": False, "error": f"アップロードに失敗: {str(e)}"}


def upload_file(
    path: str,
    filename: str,
    content: bytes,
    is_admin: bool = False,
    allow_overwrite: bool = True,
    *,
    allow_external: bool = False,
) -> Dict[str, Any]:
    """Upload in-memory content through the same streaming writer as HTTP uploads."""
    return upload_file_stream(
        path,
        filename,
        io.BytesIO(content),
        is_admin=is_admin,
        allow_overwrite=allow_overwrite,
        allow_external=allow_external,
    )


def download_file(
    path: str, is_admin: bool = False
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Get file or folder content for download.

    Args:
        path: Relative path to file or folder

    Returns:
        Tuple of (content_bytes, filename, mime_type) or (None, None, None)
    """
    target, valid = _resolve_path(path, is_admin=is_admin)

    if not valid or not target.exists():
        return None, None, None

    try:
        if target.is_dir():
            buffer = io.BytesIO()
            archive_root_name = target.name or "workspace"
            archive_name = f"{archive_root_name}.zip"
            with zipfile.ZipFile(
                buffer, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                _write_directory_contents_to_archive(
                    archive,
                    target,
                    empty_root_name=archive_root_name,
                )
            return buffer.getvalue(), archive_name, "application/zip"

        if not target.is_file():
            return None, None, None

        content = target.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(target))
        return content, target.name, mime_type or "application/octet-stream"
    except:
        return None, None, None


def _selected_archive_name(resolved_items: List[Path]) -> str:
    if len(resolved_items) == 1:
        source = resolved_items[0]
        base_name = source.name if source.is_dir() else source.stem
        return f"{base_name or 'archive'}.zip"
    return "archive.zip"


def _write_directory_contents_to_archive(
    archive: zipfile.ZipFile,
    source: Path,
    root_name: str = "",
    empty_root_name: Optional[str] = None,
) -> None:
    """Write a directory tree while preserving empty directories."""
    wrote_entry = False
    for child in sorted(source.rglob("*"), key=lambda p: str(p).lower()):
        if _is_link_or_reparse(child):
            continue
        rel = child.relative_to(source)
        arcname = (
            PurePosixPath(root_name, *rel.parts).as_posix()
            if root_name
            else PurePosixPath(*rel.parts).as_posix()
        )
        if child.is_dir():
            # ZIPには空ディレクトリを明示的なエントリとして保存する。
            if not any(child.iterdir()):
                archive.writestr(f"{arcname}/", b"")
                wrote_entry = True
        elif child.is_file():
            archive.write(child, arcname)
            wrote_entry = True

    if not wrote_entry and empty_root_name:
        archive.writestr(f"{empty_root_name}/", b"")


def _write_selected_items_to_archive(
    archive: zipfile.ZipFile, resolved_items: List[Path]
) -> None:
    used_root_names: set[str] = set()
    for source in resolved_items:
        if _is_link_or_reparse(source):
            continue
        root_name = source.name
        suffix = 2
        while root_name.casefold() in used_root_names:
            if source.is_file():
                root_name = f"{source.stem} ({suffix}){source.suffix}"
            else:
                root_name = f"{source.name} ({suffix})"
            suffix += 1
        used_root_names.add(root_name.casefold())
        if source.is_file():
            archive.write(source, root_name)
            continue

        _write_directory_contents_to_archive(
            archive,
            source,
            root_name=root_name,
            empty_root_name=root_name,
        )


def _resolve_selected_items(
    paths: List[str], is_admin: bool = False
) -> Tuple[List[Path], Optional[str]]:
    if not paths:
        return [], "対象が選択されていません"

    root = get_root_dir()

    resolved_items: List[Path] = []
    for raw_path in paths:
        target, valid = _resolve_path(raw_path, is_admin=is_admin)
        if not valid or not target.exists():
            return [], f"対象が見つかりません: {raw_path}"
        if target == root or (is_admin and target.parent == target):
            return [], "ルートディレクトリは対象にできません"
        resolved_items.append(target)
    return resolved_items, None


def download_items(
    paths: List[str], is_admin: bool = False
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Get selected files/folders as a downloadable payload without creating files."""
    if len(paths) == 1:
        return download_file(paths[0], is_admin=is_admin)

    resolved_items, error = _resolve_selected_items(paths, is_admin=is_admin)
    if error:
        return None, None, None

    try:
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            _write_selected_items_to_archive(archive, resolved_items)
        return (
            buffer.getvalue(),
            _selected_archive_name(resolved_items),
            "application/zip",
        )
    except Exception:
        return None, None, None


def archive_items(
    paths: List[str], dest_path: str = "", is_admin: bool = False
) -> Dict[str, Any]:
    """Create a zip archive from selected files/directories in dest_path."""
    if not paths:
        return {"success": False, "error": "圧縮対象が選択されていません"}

    dest, dest_valid = _resolve_path(dest_path, is_admin=is_admin)
    if not dest_valid:
        return {"success": False, "error": "無効な圧縮先です"}
    if not dest.exists() or not dest.is_dir():
        return {"success": False, "error": "圧縮先ディレクトリが見つかりません"}

    resolved_items, error = _resolve_selected_items(paths, is_admin=is_admin)
    if error:
        return {
            "success": False,
            "error": error.replace("対象にできません", "圧縮できません"),
        }

    for item in resolved_items:
        if not item.is_dir():
            continue
        try:
            dest.relative_to(item)
            return {
                "success": False,
                "error": "圧縮先を圧縮対象ディレクトリ自身の配下には指定できません",
            }
        except ValueError:
            pass

    root = get_root_dir()
    archive_name = _selected_archive_name(resolved_items)
    archive_path = _next_available_path(dest, _sanitize_name(archive_name))
    temporary_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dest,
            prefix=".aoitalk-archive-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        with zipfile.ZipFile(
            temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            _write_selected_items_to_archive(archive, resolved_items)
        os.replace(temporary_path, archive_path)
        temporary_path = None

        return {
            "success": True,
            "message": f"「{archive_path.name}」を作成しました",
            "archive_name": archive_path.name,
            "archive_path": _format_path_for_response(archive_path, root),
            "count": len(resolved_items),
        }
    except Exception as e:
        try:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"success": False, "error": f"圧縮に失敗: {str(e)}"}


def _safe_archive_member_parts(name: str) -> Optional[Tuple[str, ...]]:
    raw_name = name.replace("\\", "/")
    if not raw_name or raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        return None
    parts = PurePosixPath(raw_name).parts
    if not parts or any(
        part in {"", ".", ".."}
        or part.casefold() == ".git"
        or ":" in part
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
        for part in parts
    ):
        return None
    return tuple(parts)


def _archive_format(path: Path) -> Optional[str]:
    name = path.name.casefold()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(
        (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")
    ):
        return "tar"
    if name.endswith(".gz"):
        return "gz"
    if name.endswith(".bz2"):
        return "bz2"
    if name.endswith(".xz"):
        return "xz"
    return None


def _archive_stem(path: Path) -> str:
    name = path.name
    lower_name = name.casefold()
    for suffix in ARCHIVE_SUFFIXES:
        if lower_name.endswith(suffix):
            return name[: -len(suffix)] or "archive"
    return path.stem or "archive"


def _safe_archive_target(extract_root: Path, name: str, *, is_dir: bool) -> Path:
    parts = _safe_archive_member_parts(name)
    if parts is None:
        raise ValueError("安全でないパスを含む圧縮ファイルです")

    target_path = extract_root.joinpath(*parts)
    try:
        target_path.resolve().relative_to(extract_root.resolve())
    except ValueError as exc:
        raise ValueError("安全でないパスを含む圧縮ファイルです") from exc

    return target_path


def _copy_stream_with_limit(source: Any, target: Any) -> int:
    """Copy decompressed bytes while enforcing the per-archive output cap."""
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return total
        total += len(chunk)
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("圧縮ファイルの展開サイズが上限を超えています")
        target.write(chunk)


def _extract_zip_archive(archive_path: Path, extract_root: Path) -> None:
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        planned: List[Tuple[zipfile.ZipInfo, Path, bool]] = []
        total_size = 0
        for member in archive.infolist():
            if len(planned) >= MAX_ARCHIVE_ENTRIES:
                raise ValueError("圧縮ファイルのエントリ数が上限を超えています")
            if member.flag_bits & 0x1:
                raise ValueError("パスワード付き圧縮ファイルには対応していません")
            is_dir = member.is_dir() or member.filename.endswith(("/", "\\"))
            unix_mode = member.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode) or (
                file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
            ):
                raise ValueError(
                    "リンクまたは特殊ファイルを含む圧縮ファイルは展開できません"
                )
            target_path = _safe_archive_target(
                extract_root, member.filename, is_dir=is_dir
            )
            if not is_dir:
                total_size += max(0, int(member.file_size or 0))
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError("圧縮ファイルの展開サイズが上限を超えています")
            planned.append((member, target_path, is_dir))

        extract_root.mkdir(parents=True, exist_ok=False)
        for member, target_path, is_dir in planned:
            if is_dir:
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, target_path.open("wb") as out:
                _copy_stream_with_limit(src, out)


def _extract_tar_archive(archive_path: Path, extract_root: Path) -> None:
    with tarfile.open(archive_path, mode="r:*") as archive:
        planned: List[Tuple[tarfile.TarInfo, Path, bool]] = []
        total_size = 0
        for member in archive.getmembers():
            if len(planned) >= MAX_ARCHIVE_ENTRIES:
                raise ValueError("圧縮ファイルのエントリ数が上限を超えています")
            if not (member.isdir() or member.isreg()):
                raise ValueError(
                    "リンクまたは特殊ファイルを含む圧縮ファイルは展開できません"
                )
            if member.isdir() and member.name.replace("\\", "/").rstrip("/") == ".":
                continue
            target_path = _safe_archive_target(
                extract_root, member.name, is_dir=member.isdir()
            )
            if member.isreg():
                total_size += max(0, int(member.size or 0))
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError("圧縮ファイルの展開サイズが上限を超えています")
            planned.append((member, target_path, member.isdir()))

        extract_root.mkdir(parents=True, exist_ok=False)
        for member, target_path, is_dir in planned:
            if is_dir:
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(
                    f"圧縮ファイル内のデータを読み取れません: {member.name}"
                )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with source, target_path.open("wb") as out:
                _copy_stream_with_limit(source, out)


def _extract_7z_archive(archive_path: Path, extract_root: Path) -> None:
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        if archive.needs_password():
            raise ValueError("パスワード付き圧縮ファイルには対応していません")
        file_targets: Dict[str, Path] = {}
        directory_targets: List[Path] = []
        total_size = 0
        for member in archive.list():
            if len(file_targets) + len(directory_targets) >= MAX_ARCHIVE_ENTRIES:
                raise ValueError("圧縮ファイルのエントリ数が上限を超えています")
            if member.is_symlink or not (member.is_directory or member.is_file):
                raise ValueError(
                    "リンクまたは特殊ファイルを含む圧縮ファイルは展開できません"
                )
            target_path = _safe_archive_target(
                extract_root, member.filename, is_dir=member.is_directory
            )
            if member.is_directory:
                directory_targets.append(target_path)
            else:
                if member.filename in file_targets:
                    raise ValueError("同じパスが重複する圧縮ファイルは展開できません")
                file_targets[member.filename] = target_path
                member_size = getattr(member, "uncompressed", None)
                if member_size is None:
                    member_size = getattr(member, "uncompressed_size", None)
                if member_size is not None:
                    total_size += max(0, int(member_size))
                    if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        raise ValueError("圧縮ファイルの展開サイズが上限を超えています")
        extract_root.mkdir(parents=True, exist_ok=False)
        for target_path in directory_targets:
            target_path.mkdir(parents=True, exist_ok=True)
        writer_factory = _SevenZipWriterFactory(file_targets)
        try:
            archive.extractall(factory=writer_factory)
        finally:
            writer_factory.close()


class _SevenZipFileWriter(Py7zIO):
    def __init__(self, target_path: Path, remaining_budget: List[int]):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = target_path.open("w+b")
        self._remaining_budget = remaining_budget

    def write(self, data: bytes | bytearray) -> int:
        if len(data) > self._remaining_budget[0]:
            raise ValueError("圧縮ファイルの展開サイズが上限を超えています")
        self._remaining_budget[0] -= len(data)
        return self._file.write(data)

    def read(self, size: Optional[int] = None) -> bytes:
        return self._file.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def flush(self) -> None:
        self._file.flush()

    def size(self) -> int:
        position = self._file.tell()
        self._file.seek(0, os.SEEK_END)
        size = self._file.tell()
        self._file.seek(position)
        return size

    def close(self) -> None:
        self._file.close()


class _SevenZipWriterFactory(WriterFactory):
    def __init__(self, targets: Dict[str, Path]):
        self._targets = targets
        self._writers: List[_SevenZipFileWriter] = []
        self._remaining_budget = [MAX_ARCHIVE_UNCOMPRESSED_BYTES]

    def create(self, filename: str) -> Py7zIO:
        target_path = self._targets.get(filename)
        if target_path is None:
            raise ValueError(f"未検証のパスは展開できません: {filename}")
        writer = _SevenZipFileWriter(target_path, self._remaining_budget)
        self._writers.append(writer)
        return writer

    def close(self) -> None:
        for writer in self._writers:
            writer.close()


def _extract_single_stream(
    archive_path: Path, extract_root: Path, archive_format: str
) -> None:
    output_name = _archive_stem(archive_path)
    target_path = _safe_archive_target(extract_root, output_name, is_dir=False)
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[archive_format]
    extract_root.mkdir(parents=True, exist_ok=False)
    with opener(archive_path, "rb") as source, target_path.open("wb") as out:
        _copy_stream_with_limit(source, out)


def extract_archives(
    paths: List[str], dest_path: str = "", is_admin: bool = False
) -> Dict[str, Any]:
    """Extract supported archives into folders under dest_path."""
    if not paths:
        return {"success": False, "error": "展開対象が選択されていません"}

    dest, dest_valid = _resolve_path(dest_path, is_admin=is_admin)
    if not dest_valid:
        return {"success": False, "error": "無効な展開先です"}
    if not dest.exists() or not dest.is_dir():
        return {"success": False, "error": "展開先ディレクトリが見つかりません"}

    root = get_root_dir()
    archives: List[Tuple[Path, str]] = []
    for raw_path in paths:
        target, valid = _resolve_path(raw_path, is_admin=is_admin)
        if not valid or not target.exists() or not target.is_file():
            return {
                "success": False,
                "error": f"圧縮ファイルが見つかりません: {raw_path}",
            }
        archive_format = _archive_format(target)
        if archive_format is None:
            return {
                "success": False,
                "error": f"対応していない圧縮形式です: {target.name}",
            }
        if archive_format == "zip" and not zipfile.is_zipfile(target):
            return {
                "success": False,
                "error": f"正しいZIPファイルではありません: {target.name}",
            }
        if archive_format == "tar" and not tarfile.is_tarfile(target):
            return {
                "success": False,
                "error": f"正しいTARファイルではありません: {target.name}",
            }
        archives.append((target, archive_format))

    extracted: List[Dict[str, str]] = []
    created_roots: List[Path] = []
    try:
        for archive_path, archive_format in archives:
            extract_root = _next_available_path(
                dest, _sanitize_name(_archive_stem(archive_path))
            )
            created_roots.append(extract_root)
            if archive_format == "zip":
                _extract_zip_archive(archive_path, extract_root)
            elif archive_format == "tar":
                _extract_tar_archive(archive_path, extract_root)
            elif archive_format == "7z":
                _extract_7z_archive(archive_path, extract_root)
            else:
                _extract_single_stream(archive_path, extract_root, archive_format)

            extracted.append(
                {
                    "archive_name": archive_path.name,
                    "path": _format_path_for_response(extract_root, root),
                    "name": extract_root.name,
                }
            )

        return {
            "success": True,
            "message": f"{len(extracted)}件の圧縮ファイルを展開しました",
            "extracted": extracted,
        }
    except ValueError as e:
        for created_root in created_roots:
            shutil.rmtree(created_root, ignore_errors=True)
        return {"success": False, "error": str(e)}
    except Exception as e:
        for created_root in created_roots:
            shutil.rmtree(created_root, ignore_errors=True)
        return {"success": False, "error": f"展開に失敗: {str(e)}"}


def resolve_file_path(path: str, is_admin: bool = False) -> Optional[Path]:
    """
    Resolve a workspace path to an absolute Path for inline serving.

    Resolves Windows .lnk shortcuts to their target file transparently.

    Args:
        path: Relative path to file within workspace (may point at a .lnk)

    Returns:
        Resolved absolute Path if valid file exists, None otherwise
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid or not target.exists() or not target.is_file():
        return None
    # A Windows .lnk target is not a workspace path and cannot be safely
    # revalidated on all supported hosts.  Reject it rather than turning an
    # authenticated inline-serve endpoint into an arbitrary file reader.
    if target.suffix.lower() == SHORTCUT_EXTENSION:
        return None
    return target


def _tree_contains_link_or_reparse(root: Path) -> bool:
    """Reject recursive operations that would copy or move a link target."""
    if _is_link_or_reparse(root):
        return True
    try:
        for current, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            if current_path != root and _is_link_or_reparse(current_path):
                return True
            safe_dirnames = []
            for dirname in dirnames:
                child = current_path / dirname
                if _is_link_or_reparse(child):
                    return True
                safe_dirnames.append(dirname)
            dirnames[:] = safe_dirnames
            for filename in filenames:
                if _is_link_or_reparse(current_path / filename):
                    return True
    except OSError:
        return True
    return False


def rename_item(path: str, new_name: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Rename a file or directory.

    Args:
        path: Path to item
        new_name: New name
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}

    root = get_root_dir()
    if target == root or (is_admin and target.parent == target):
        return {"success": False, "error": "ルートディレクトリは名前変更できません"}

    safe_name = _sanitize_name(new_name)
    new_path = target.parent / safe_name

    if new_path.exists():
        return {"success": False, "error": f"「{safe_name}」は既に存在します"}

    try:
        target.rename(new_path)
        rel_path = _format_path_for_response(new_path, root)

        return {
            "success": True,
            "message": f"名前を「{safe_name}」に変更しました",
            "new_name": safe_name,
            "new_path": rel_path,
        }
    except Exception as e:
        return {"success": False, "error": f"名前変更に失敗: {str(e)}"}


def move_item(src_path: str, dest_path: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Move a file or directory.

    Args:
        src_path: Source path
        dest_path: Destination directory path
    """
    src, src_valid = _resolve_path(src_path, is_admin=is_admin)
    dest, dest_valid = _resolve_path(dest_path, is_admin=is_admin)

    if not src_valid or not dest_valid:
        return {"success": False, "error": "無効なパスです"}

    if not src.exists():
        return {"success": False, "error": "移動元が見つかりません"}

    root = get_root_dir()
    if src == root or (is_admin and src.parent == src):
        return {"success": False, "error": "ルートディレクトリは移動できません"}

    if src.is_dir():
        try:
            dest.relative_to(src)
        except ValueError:
            pass
        else:
            return {
                "success": False,
                "error": "ディレクトリ自身またはその配下へは移動できません",
            }
        if _tree_contains_link_or_reparse(src):
            return {
                "success": False,
                "error": "リンクまたは特殊ファイルを含むディレクトリは移動できません",
            }

    # Ensure dest is a directory
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    elif not dest.is_dir():
        return {"success": False, "error": "移動先はディレクトリである必要があります"}

    new_path = dest / src.name
    same_path = os.path.normcase(os.path.abspath(src)) == os.path.normcase(
        os.path.abspath(new_path)
    )
    if same_path:
        return {
            "success": True,
            "message": f"「{src.name}」は既に移動先にあります",
            "new_path": _format_path_for_response(src, root),
        }
    if new_path.exists():
        return {"success": False, "error": f"移動先に「{src.name}」が既に存在します"}

    try:
        shutil.move(str(src), str(new_path))
        rel_path = _format_path_for_response(new_path, root)

        return {
            "success": True,
            "message": f"「{src.name}」を移動しました",
            "new_path": rel_path,
        }
    except Exception as e:
        return {"success": False, "error": f"移動に失敗: {str(e)}"}


def copy_item(
    src_path: str,
    dest_path: str,
    is_admin: bool = False,
    conflict_strategy: str = "rename",
) -> Dict[str, Any]:
    """
    Copy a file or directory.

    Args:
        src_path: Source path
        dest_path: Destination directory path
    """
    src, src_valid = _resolve_path(src_path, is_admin=is_admin)
    dest, dest_valid = _resolve_path(dest_path, is_admin=is_admin)

    if not src_valid or not dest_valid:
        return {"success": False, "error": "無効なパスです"}

    if not src.exists():
        return {"success": False, "error": "コピー元が見つかりません"}

    root = get_root_dir()
    if src == root or (is_admin and src.parent == src):
        return {"success": False, "error": "ルートディレクトリはコピーできません"}

    if src.is_dir():
        try:
            dest.relative_to(src)
        except ValueError:
            pass
        else:
            return {
                "success": False,
                "error": "ディレクトリ自身またはその配下へはコピーできません",
            }
        if _tree_contains_link_or_reparse(src):
            return {
                "success": False,
                "error": "リンクまたは特殊ファイルを含むディレクトリはコピーできません",
            }

    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    elif not dest.is_dir():
        return {"success": False, "error": "コピー先はディレクトリである必要があります"}

    new_path = dest / src.name

    # Web/UI callers keep the historical auto-rename behavior. Agent callers use
    # reuse_identical so a retry never creates _copy1/_copy2 duplicates.
    if new_path.exists():
        if conflict_strategy == "reuse_identical":
            if src.is_file() and new_path.is_file() and filecmp.cmp(
                src,
                new_path,
                shallow=False,
            ):
                return {
                    "success": True,
                    "created": False,
                    "message": f"「{src.name}」は同一内容で配置済みです",
                    "new_path": _format_path_for_response(new_path, root),
                    "new_name": new_path.name,
                }
            return {
                "success": False,
                "error": f"コピー先に内容の異なる「{src.name}」が既に存在します",
            }
        if conflict_strategy == "error":
            return {"success": False, "error": f"コピー先に「{src.name}」が既に存在します"}
        if conflict_strategy != "rename":
            return {"success": False, "error": "不明な競合処理です"}
        base = src.stem
        ext = src.suffix
        counter = 1
        while new_path.exists():
            new_path = dest / f"{base}_copy{counter}{ext}"
            counter += 1

    try:
        if src.is_dir():
            shutil.copytree(str(src), str(new_path))
        else:
            shutil.copy2(str(src), str(new_path))

        rel_path = _format_path_for_response(new_path, root)

        return {
            "success": True,
            "created": True,
            "message": f"「{src.name}」をコピーしました",
            "new_path": rel_path,
            "new_name": new_path.name,
        }
    except Exception as e:
        return {"success": False, "error": f"コピーに失敗: {str(e)}"}


# ── ゴミ箱（削除の取り消し用） ──────────────────────────────────────────
# ストレージルート直下の `.trash/<token>/` に退避する。
#   .trash/<token>/payload/<元の名前>   … 実体
#   .trash/<token>/meta.json            … 復元用メタデータ
TRASH_DIR_NAME = ".trash"
TRASH_PAYLOAD_DIR_NAME = "payload"
TRASH_META_FILE_NAME = "meta.json"

# 物理削除へ進む前にゴミ箱へ退避する期間。保持期間は Web/Next の
# conversation cleanup と同じ環境変数を共有し、Python 側で別の値を
# ハードコードしない。環境変数は起動時に読み取るが、テストや埋め込み
# 起動経路が環境を差し替えた場合に備えて ``get_trash_retention_days``
# でも同じ検証を再利用する。
DEFAULT_TRASH_RETENTION_DAYS = 30
MAX_TRASH_RETENTION_DAYS = 3650


def _parse_retention_days(value: Any, default: int = DEFAULT_TRASH_RETENTION_DAYS) -> int:
    """安全な非負整数として削除保持期間を読み取る。"""

    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if 0 < parsed <= MAX_TRASH_RETENTION_DAYS else default


def get_trash_retention_days() -> int:
    """Return the configured deletion retention period in days.

    ``AOITALK_DELETION_RETENTION_DAYS`` is deliberately the only public
    configuration key. Invalid and negative values fail closed to the
    conservative default instead of triggering an immediate purge.
    """

    raw = os.environ.get("AOITALK_DELETION_RETENTION_DAYS")
    if raw is None:
        # During module initialisation the constant is not defined yet; after
        # import this preserves the historical monkeypatch/test seam while
        # keeping one parser and one environment key.
        return int(globals().get("TRASH_RETENTION_DAYS", DEFAULT_TRASH_RETENTION_DAYS))
    return _parse_retention_days(raw)


TRASH_RETENTION_DAYS = get_trash_retention_days()


def _trash_root() -> Path:
    """ゴミ箱のルートディレクトリ（ストレージルート直下の `.trash`）"""
    return get_root_dir() / TRASH_DIR_NAME


def _is_inside_root(target: Path, root: Path) -> bool:
    """target がストレージルート配下かどうか"""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _is_hidden_under(item: Path, base: Path) -> bool:
    """base からの相対パーツに `.` 始まりが含まれるか。

    再帰検索で `.trash/<token>/payload/xxx` のような隠しディレクトリの
    子孫を除外するために使う（`item.name` だけでは祖先を判定できない）。
    """
    try:
        parts = item.relative_to(base).parts
    except ValueError:
        parts = (item.name,)
    return any(part.startswith(".") for part in parts)


def _sanitize_trash_token(token: str) -> str:
    """ゴミ箱トークンからパス区切り等を除去する"""
    return re.sub(r"[^0-9A-Za-z_-]", "", token or "")


def _move_to_trash(
    target: Path,
    root: Path,
    *,
    require_metadata: bool = False,
) -> Dict[str, Any]:
    """target をゴミ箱へ退避し、復元用の情報を返す"""
    original_path = _format_path_for_response(target, root)
    is_directory = target.is_dir() and not target.is_symlink()
    token = uuid.uuid4().hex
    entry_dir = _trash_root() / token
    payload_dir = entry_dir / TRASH_PAYLOAD_DIR_NAME
    payload_target = payload_dir / target.name
    payload_dir.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(target), str(payload_target))
    except Exception:
        # 退避に失敗したら空エントリを残さない
        shutil.rmtree(str(entry_dir), ignore_errors=True)
        raise

    meta = {
        "token": token,
        "original_path": original_path,
        "name": target.name,
        "is_directory": is_directory,
        "deleted_at": int(time.time()),
    }
    try:
        (entry_dir / TRASH_META_FILE_NAME).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as metadata_error:
        if require_metadata:
            try:
                shutil.move(str(payload_target), str(target))
            except Exception as restore_error:
                # The payload is intentionally left in the trash entry so an
                # operator can recover it even if restoring the original path
                # also failed.
                raise RuntimeError(
                    f"ゴミ箱メタデータの作成と元パスへの復元に失敗: {entry_dir}"
                ) from restore_error
            shutil.rmtree(str(entry_dir), ignore_errors=True)
            raise metadata_error
        # メタが書けなくても実体は退避済み。purge 側は mtime で処理できる。
        pass

    return {
        "token": token,
        "original_path": original_path,
        "name": target.name,
        "is_directory": is_directory,
    }


def _physical_delete(target: Path) -> None:
    """ゴミ箱を経由しない物理削除"""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(str(target), onerror=_remove_readonly)
    else:
        target.unlink()


def _read_trash_meta(entry_dir: Path) -> Optional[Dict[str, Any]]:
    """meta.json を読む。壊れている/無い場合は None"""
    meta_path = entry_dir / TRASH_META_FILE_NAME
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def purge_trash(
    days: Optional[int] = None,
    *,
    max_entries: Optional[int] = None,
) -> Dict[str, Any]:
    """`days` 日より古いゴミ箱エントリを削除する。

    meta.json の deleted_at を基準にし、meta が壊れている/無い場合は
    エントリディレクトリの mtime を基準にする。

    ``max_entries`` は定期 housekeeping 用の上限。省略時は従来どおり
    ゴミ箱全体を走査するため、既存の管理者/テスト呼び出しの挙動を変えない。
    """
    retention_days = (
        get_trash_retention_days() if days is None else _parse_retention_days(days)
    )
    return _purge_trash_root(
        _trash_root(),
        retention_days,
        max_entries=max_entries,
    )


def _purge_trash_root(
    trash_root: Path,
    days: int,
    *,
    max_entries: Optional[int] = None,
) -> Dict[str, Any]:
    """指定したゴミ箱ルートを掃除する（purge_trash の実体）"""
    if not trash_root.is_dir():
        return {"success": True, "removed": 0}

    cutoff = time.time() - max(days, 0) * 86400
    removed = 0
    inspected = 0
    entry_limit = None
    if max_entries is not None:
        try:
            entry_limit = max(0, int(max_entries))
        except (TypeError, ValueError):
            entry_limit = 0
        if entry_limit == 0:
            return {"success": True, "removed": 0}

    try:
        entries = list(trash_root.iterdir())
    except OSError as e:
        return {"success": False, "error": f"ゴミ箱の読み取りに失敗: {e}"}

    for entry in entries:
        if entry_limit is not None and inspected >= entry_limit:
            break
        inspected += 1
        try:
            if entry.is_dir():
                meta = _read_trash_meta(entry)
                deleted_at = None
                if meta is not None:
                    raw = meta.get("deleted_at")
                    if isinstance(raw, (int, float)):
                        deleted_at = float(raw)
                if deleted_at is None:
                    deleted_at = entry.stat().st_mtime
                if deleted_at >= cutoff:
                    continue
                shutil.rmtree(str(entry), onerror=_remove_readonly)
            else:
                # 想定外のファイルも mtime 基準で掃除する
                if entry.stat().st_mtime >= cutoff:
                    continue
                entry.unlink()
            removed += 1
        except OSError:
            continue

    return {"success": True, "removed": removed}


def restore_from_trash(
    token: str,
    *,
    allowed_root: Optional[str] = None,
) -> Dict[str, Any]:
    """ゴミ箱のエントリを元の場所へ復元する"""
    safe_token = _sanitize_trash_token(token)
    if not safe_token:
        return {"success": False, "error": "無効なトークンです", "code": "not_found"}

    root = get_root_dir()
    entry_dir = _trash_root() / safe_token
    if not entry_dir.is_dir():
        return {
            "success": False,
            "error": "復元対象が見つかりません（期限切れの可能性があります）",
            "code": "not_found",
        }

    payload_dir = entry_dir / TRASH_PAYLOAD_DIR_NAME
    try:
        children = list(payload_dir.iterdir()) if payload_dir.is_dir() else []
    except OSError:
        children = []
    if not children:
        return {
            "success": False,
            "error": "復元対象の実体が見つかりません",
            "code": "not_found",
        }

    source = children[0]
    meta = _read_trash_meta(entry_dir) or {}
    original_path = str(meta.get("original_path") or source.name).replace("\\", "/")

    # meta.json は信頼しない。`_resolve_path` と同じく `.git` / `.trash` を含む
    # 復元先は拒否し、絶対パス指定もルート直下扱いに落とす。
    original_parts = [part.casefold() for part in original_path.split("/")]
    if ".git" in original_parts or TRASH_DIR_NAME in original_parts:
        original_path = source.name
    if _is_absolute_input(original_path):
        original_path = source.name

    if allowed_root is not None:
        normalized_root = allowed_root.replace("\\", "/").strip("/")
        normalized_original = original_path.strip("/")
        root_key = normalized_root.casefold()
        original_key = normalized_original.casefold()
        if not root_key or not (
            original_key == root_key or original_key.startswith(f"{root_key}/")
        ):
            return {
                "success": False,
                "error": "このプロジェクトの復元対象ではありません",
                "code": "forbidden",
            }

    # 復元先の親ディレクトリを決める（ルート外へは絶対に出さない）
    parent_rel = PurePosixPath(original_path).parent
    dest_parent = root if str(parent_rel) in {".", "", "/"} else (root / str(parent_rel))
    dest_parent = dest_parent.resolve()
    if not _is_inside_root(dest_parent, root):
        dest_parent = root

    try:
        dest_parent.mkdir(parents=True, exist_ok=True)
        # 同名が既にある場合は連番で退避する
        dest_path = _next_available_path(dest_parent, source.name)
        shutil.move(str(source), str(dest_path))
    except OSError as e:
        return {"success": False, "error": f"復元に失敗: {e}"}

    try:
        shutil.rmtree(str(entry_dir), onerror=_remove_readonly)
    except OSError:
        pass

    return {
        "success": True,
        "message": f"「{dest_path.name}」を復元しました",
        "restored_path": _format_path_for_response(dest_path, root),
        "name": dest_path.name,
        "is_directory": dest_path.is_dir(),
    }


def delete_item(
    path: str,
    is_admin: bool = False,
    *,
    require_trash: bool = False,
) -> Dict[str, Any]:
    """
    Delete a file or directory.

    ストレージルート配下は物理削除せず `.trash/` へ退避し、復元用トークンを返す。
    ルート外（管理者の絶対パス等）は従来どおり物理削除する。

    Args:
        path: Path to delete
        require_trash: ゴミ箱への退避に失敗した場合、物理削除へフォールバック
            せず失敗させる。DB更新と組み合わせる呼び出し側で使用する。
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}

    root = get_root_dir()
    if target == root or (is_admin and target.parent == target):
        return {"success": False, "error": "ルートディレクトリは削除できません"}

    try:
        name = target.name
        trash_info: Optional[Dict[str, Any]] = None

        if _is_inside_root(target, root):
            try:
                trash_info = _move_to_trash(
                    target,
                    root,
                    require_metadata=require_trash,
                )
            except (OSError, shutil.Error):
                if require_trash:
                    raise
                # ゴミ箱へ退避できない場合でも削除自体は完遂させる（復元は不可）
                trash_info = None
                if target.exists():
                    _physical_delete(target)
        else:
            if require_trash:
                return {
                    "success": False,
                    "error": "ゴミ箱へ退避できない場所は削除できません",
                }
            _physical_delete(target)

        return {
            "success": True,
            "message": f"「{name}」を削除しました",
            "trash": trash_info,
        }
    except Exception as e:
        return {"success": False, "error": f"削除に失敗: {str(e)}"}


def get_file_info(path: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Get detailed file/directory information.

    Args:
        path: Path to item
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}

    try:
        stat = target.stat()
        root = get_root_dir()
        rel_path = _format_path_for_response(target, root)

        info = {
            "success": True,
            "name": target.name,
            "path": rel_path,
            "is_directory": target.is_dir(),
            "size_bytes": stat.st_size,
            "size_display": _format_size(stat.st_size),
            "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }

        if target.is_file():
            info.update(
                {
                    "extension": target.suffix.lower(),
                    "type": _get_file_type(target),
                    "icon": _get_icon(target),
                }
            )
        else:
            info["icon"] = "📁"
            try:
                info["item_count"] = sum(
                    1 for _ in target.iterdir() if not _.name.startswith(".")
                )
            except:
                info["item_count"] = 0

        return info
    except Exception as e:
        return {"success": False, "error": f"情報取得に失敗: {str(e)}"}


def _slice_preview_text(
    content: str, *, offset: int, max_chars: int
) -> Dict[str, Any]:
    """変換後テキストから offset..offset+max_chars を切り出し、続きの取り方を添える。"""
    total = len(content)
    start = max(0, min(int(offset or 0), total))
    end = min(total, start + max(1, int(max_chars)))
    result: Dict[str, Any] = {
        "content": content[start:end],
        "truncated": end < total,
        "offset": start,
        "total_chars": total,
    }
    if end < total:
        result["next_offset"] = end
        result["message"] = (
            f"全体{total}文字中 {start}〜{end}文字目を表示しています。"
            f"続きは offset={end} を指定して取得してください。"
        )
    elif start > 0:
        result["message"] = f"全体{total}文字中 {start}〜{end}文字目を表示しています。"
    return result


def _read_size_error(target: Path) -> Optional[Dict[str, Any]]:
    """チャット添付上限を超えるファイルは読み取らずエラーを返す。"""
    size = target.stat().st_size
    if size <= MAX_READ_FILE_SIZE:
        return None
    return {
        "success": False,
        "error": (
            f"ファイルサイズが{size / 1024 / 1024:.1f}MBあり、"
            f"読み取り上限の{MAX_READ_FILE_SIZE // 1024 // 1024}MBを超えています。"
        ),
    }


def get_preview(
    path: str, max_chars: int = 5000, is_admin: bool = False, offset: int = 0
) -> Dict[str, Any]:
    """
    Get preview content for a file.

    Args:
        path: Path to file
        max_chars: Maximum characters for text preview
        offset: 変換後テキストの読み出し開始位置（文字数）

    Returns:
        Preview data depending on file type
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists() or not target.is_file():
        return {"success": False, "error": "ファイルが見つかりません"}

    file_type = _get_file_type(target)
    root = get_root_dir()
    rel_path = _format_path_for_response(target, root)

    try:
        if file_type in {"text", "binary"}:
            size_error = _read_size_error(target)
            if size_error:
                return size_error
            try:
                content, encoding = read_safe_text(
                    target,
                    known_text_extensions=TEXT_EXTENSIONS,
                )
            except TextContentError as exc:
                return _binary_preview(target, rel_path, reason=str(exc))

            return {
                "success": True,
                "type": "text",
                "path": rel_path,
                "extension": target.suffix.lower(),
                "encoding": encoding,
                **_slice_preview_text(content, offset=offset, max_chars=max_chars),
            }

        elif file_type == "image":
            # Image: return base64 for inline display
            content = target.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(target))
            b64 = base64.b64encode(content).decode("utf-8")

            return {
                "success": True,
                "type": "image",
                "path": rel_path,
                "mime_type": mime_type or "image/png",
                "data_url": f"data:{mime_type or 'image/png'};base64,{b64}",
                "size_bytes": len(content),
            }

        elif file_type == "office":
            size_error = _read_size_error(target)
            if size_error:
                return size_error
            converted = _convert_office_to_text(target)
            if converted.get("success"):
                return {
                    "success": True,
                    "type": "office",
                    "path": rel_path,
                    "extension": target.suffix.lower(),
                    **_slice_preview_text(
                        str(converted.get("content") or ""),
                        offset=offset,
                        max_chars=max_chars,
                    ),
                }
            return {
                "success": True,
                "type": "office",
                "path": rel_path,
                "content": None,
                "message": (
                    "プレビューを生成できませんでした: "
                    f"{converted.get('error') or '原因不明'}"
                ),
                "error_detail": converted.get("error"),
                "extension": target.suffix.lower(),
            }

        elif file_type == "mail":
            size_error = _read_size_error(target)
            if size_error:
                return size_error
            converted = _convert_mail_to_text(target)
            if not converted.get("success"):
                return {
                    "success": False,
                    "error": (
                        "メール本文を解析できませんでした: "
                        f"{converted.get('error') or '原因不明'}"
                    ),
                    "extension": target.suffix.lower(),
                }
            return {
                "success": True,
                "type": "mail",
                "path": rel_path,
                "extension": target.suffix.lower(),
                **_slice_preview_text(
                    str(converted.get("content") or ""),
                    offset=offset,
                    max_chars=max_chars,
                ),
            }

        else:
            return _binary_preview(target, rel_path)

    except Exception as e:
        return {"success": False, "error": f"プレビュー生成に失敗: {str(e)}"}


def _convert_office_to_text(file_path: Path) -> Dict[str, Any]:
    """OfficeドキュメントをmarkitdownでMarkdown化する。失敗理由も返す。"""
    try:
        from ..documents.office_reader import convert_office_bytes_to_markdown

        content = file_path.read_bytes()
        return convert_office_bytes_to_markdown(content, file_path.name)
    except Exception as exc:
        return {"success": False, "error": f"ファイル変換に失敗しました: {exc}"}


def _convert_mail_to_text(file_path: Path) -> Dict[str, Any]:
    """Parse RFC 822 or Outlook mail into a bounded, model-readable transcript."""
    try:
        from ...services.mail_parser import parse_mail_file

        parsed = parse_mail_file(file_path)
        lines = [
            "[非信頼メール資料 BEGIN]",
            f"Subject: {parsed.subject}",
            f"From: {parsed.sender}",
            f"To: {', '.join(parsed.to)}",
            f"Cc: {', '.join(parsed.cc)}",
            f"Bcc: {', '.join(parsed.bcc)}",
            f"Date: {parsed.date}",
        ]
        if parsed.message_id:
            lines.append(f"Message-ID: {parsed.message_id}")
        if parsed.in_reply_to:
            lines.append(f"In-Reply-To: {parsed.in_reply_to}")
        if parsed.references:
            lines.append(f"References: {' '.join(parsed.references)}")
        lines.extend(["", "Body:", parsed.body, "[非信頼メール資料 END]"])
        return {"success": True, "content": "\n".join(lines)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_directory_tree(max_depth: int = 3, root_path: str = "") -> Dict[str, Any]:
    """
    Get directory tree structure.

    Args:
        max_depth: Maximum depth to traverse
        root_path: Optional path relative to workspace root to use as tree root

    Returns:
        Nested tree structure
    """
    workspace_root = get_root_dir()

    # Determine actual root for tree through the same boundary resolver as
    # every other explorer operation; never join an untrusted ``../`` value
    # directly to the workspace root.
    if root_path:
        actual_root, valid = _resolve_path(root_path, is_admin=False)
        if not valid:
            return {"success": False, "error": "Invalid workspace path."}
        if not actual_root.exists():
            actual_root.mkdir(parents=True, exist_ok=True)
    else:
        actual_root = workspace_root

    def build_tree(path: Path, depth: int = 0) -> Dict[str, Any]:
        if depth > max_depth:
            return None

        try:
            children = []
            for item in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                if _is_link_or_reparse(item):
                    continue
                if item.is_dir():
                    child_tree = build_tree(item, depth + 1)
                    if child_tree:
                        children.append(child_tree)

            # Calculate relative path from actual_root (not workspace_root)
            rel_path = str(path.relative_to(actual_root)).replace("\\", "/")
            if rel_path == ".":
                rel_path = ""

            return {
                "name": path.name if path != actual_root else "Workspace",
                "path": rel_path,
                "type": "directory",
                "children": children,
            }
        except:
            return None

    tree = build_tree(actual_root)
    return {"success": True, "tree": tree}


def walk_workspace_tree(
    path: str = "",
    max_depth: int = 3,
    include_files: bool = True,
    max_entries: int = 300,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Return a bounded recursive inventory of files and folders."""
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "Invalid workspace path."}
    if not target.exists() or not target.is_dir():
        return {"success": False, "error": "Path is not an existing directory."}

    root = get_root_dir()
    max_depth = max(1, min(int(max_depth or 1), 8))
    max_entries = max(1, min(int(max_entries or 1), 1000))
    entries: list[dict[str, Any]] = []
    truncated = False
    resolved_root = root.resolve()

    def is_safe_child(item: Path) -> bool:
        """Reject links/junctions that resolve outside the authorized workspace."""
        try:
            if _is_link_or_reparse(item):
                return False
            item.resolve().relative_to(resolved_root)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or truncated:
            return
        try:
            children = [item for item in current.iterdir() if is_safe_child(item)]
            children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        except (OSError, PermissionError):
            return

        for item in children:
            if item.name.startswith("."):
                continue
            if len(entries) >= max_entries:
                truncated = True
                return
            try:
                stat = item.stat()
                is_dir = item.is_dir()
            except (OSError, PermissionError):
                continue
            if not is_dir and not include_files:
                continue

            entry: dict[str, Any] = {
                "name": item.name,
                "path": _format_path_for_response(item, root),
                "kind": "directory" if is_dir else "file",
                "depth": depth,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            if is_dir:
                try:
                    entry["item_count"] = sum(
                        1 for child in item.iterdir() if not child.name.startswith(".")
                    )
                except (OSError, PermissionError):
                    entry["item_count"] = None
            else:
                entry.update(
                    {
                        "extension": item.suffix.lower(),
                        "type": _get_file_type(item),
                        "size_bytes": stat.st_size,
                        "size_display": _format_size(stat.st_size),
                    }
                )
            entries.append(entry)

            if is_dir and depth < max_depth:
                walk(item, depth + 1)

    walk(target, 1)

    return {
        "success": True,
        "root_path": _format_path_for_response(target, root),
        "max_depth": max_depth,
        "include_files": include_files,
        "entries": entries,
        "total_returned": len(entries),
        "truncated": truncated,
    }


def search_workspace_entries(
    query: str,
    path: str = "",
    include_dirs: bool = True,
    include_files: bool = True,
    max_results: int = 50,
    extensions: Optional[List[str]] = None,
    is_admin: bool = False,
    regex: bool = False,
) -> Dict[str, Any]:
    """Search files and folders by name, with glob, regex, and extension filters."""
    query_text = str(query or "").strip()
    if not query_text:
        return {"success": False, "error": "query is required."}

    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "Invalid workspace path."}
    if not target.exists() or not target.is_dir():
        return {"success": False, "error": "Search root is not an existing directory."}

    root = get_root_dir()
    max_results = max(1, min(int(max_results or 1), 200))
    query_lower = query_text.lower()
    query_pattern: Optional[re.Pattern[str]] = None
    if regex:
        try:
            query_pattern = re.compile(query_text, re.IGNORECASE)
        except re.error as exc:
            return {"success": False, "error": f"正規表現が無効です: {exc}"}
    # `*` / `?` を含むクエリは glob、含まなければ従来通りの部分一致で扱う。
    use_glob = not regex and ("*" in query_text or "?" in query_text)
    extension_filter = {
        str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}"
        for item in (extensions or [])
        if str(item or "").strip()
    }
    results: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    try:
        for item in target.rglob("*"):
            scanned += 1
            if scanned > 10000:
                truncated = True
                break
            if _is_hidden_under(item, target):
                continue
            if _is_link_or_reparse(item):
                continue
            try:
                is_dir = item.is_dir()
                is_file = item.is_file()
            except OSError:
                continue
            if is_dir and not include_dirs:
                continue
            if is_file and not include_files:
                continue
            if not is_dir and not is_file:
                continue
            if extension_filter and (is_dir or item.suffix.lower() not in extension_filter):
                continue
            if query_pattern is not None:
                if query_pattern.search(item.name) is None:
                    continue
            elif use_glob:
                if not fnmatch.fnmatch(item.name.lower(), query_lower):
                    continue
            elif query_lower not in item.name.lower():
                continue

            try:
                stat = item.stat()
            except (OSError, PermissionError):
                continue
            result: dict[str, Any] = {
                "name": item.name,
                "path": _format_path_for_response(item, root),
                "kind": "directory" if is_dir else "file",
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            if is_file:
                result.update(
                    {
                        "extension": item.suffix.lower(),
                        "type": _get_file_type(item),
                        "size_bytes": stat.st_size,
                        "size_display": _format_size(stat.st_size),
                    }
                )
            if is_dir:
                try:
                    result["item_count"] = sum(
                        1 for child in item.iterdir() if not child.name.startswith(".")
                    )
                except (OSError, PermissionError):
                    result["item_count"] = None
            results.append(result)
            if len(results) >= max_results:
                truncated = True
                break
    except Exception as exc:
        return {"success": False, "error": f"Search failed: {exc}"}

    return {
        "success": True,
        "query": query_text,
        "root_path": _format_path_for_response(target, root),
        "results": results,
        "total_returned": len(results),
        "truncated": truncated,
    }


# ── Editor Operations ───────────────────────────────────────────────────


MAX_EDITOR_FILE_SIZE = 1 * 1024 * 1024  # 1MB


def save_file(
    path: str, content: str, encoding: str = "utf-8", is_admin: bool = False
) -> Dict[str, Any]:
    """
    Save text content to an existing file.

    Args:
        path: Path to file
        content: Text content to save
        encoding: File encoding (default: utf-8)
        is_admin: If True, allow access to any path
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists():
        # Allow creating new files if parent exists
        if not target.parent.exists():
            return {"success": False, "error": "親ディレクトリが存在しません"}
    elif not target.is_file():
        return {"success": False, "error": "対象はファイルではありません"}

    ext = _text_extension_key(target)
    if ext not in TEXT_EXTENSIONS:
        return {"success": False, "error": f"テキストファイルのみ保存可能です: {ext}"}

    content_bytes = content.encode(encoding)
    if len(content_bytes) > MAX_EDITOR_FILE_SIZE:
        return {"success": False, "error": "ファイルサイズが1MBを超えています"}

    try:
        target.write_text(content, encoding=encoding)
        stat = target.stat()
        return {
            "success": True,
            "message": f"ファイル「{target.name}」を保存しました",
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
    except Exception as e:
        return {"success": False, "error": f"保存に失敗: {str(e)}"}


def get_full_content(path: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Get full text content of a file for editor use.

    Args:
        path: Path to file
        is_admin: If True, allow access to any path

    Returns:
        Full content without truncation
        (テキストは1MBまで、Office/PDF/メールは変換して50MBまで)
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists() or not target.is_file():
        return {"success": False, "error": "ファイルが見つかりません"}

    stat = target.stat()
    ext = _text_extension_key(target)
    is_office = ext in OFFICE_EXTENSIONS
    is_mail = ext in MAIL_EXTENSIONS
    if is_office or is_mail:
        size_error = _read_size_error(target)
        if size_error:
            return size_error
    elif stat.st_size > MAX_EDITOR_FILE_SIZE:
        return {
            "success": False,
            "error": "ファイルサイズが1MBを超えています。エディタでは開けません。",
        }

    try:
        if is_office:
            converted = _convert_office_to_text(target)
            if not converted.get("success"):
                return {
                    "success": False,
                    "error": (
                        "テキストへ変換できませんでした: "
                        f"{converted.get('error') or '原因不明'}"
                    ),
                }
            content = str(converted.get("content") or "")
        elif is_mail:
            converted = _convert_mail_to_text(target)
            if not converted.get("success"):
                return {
                    "success": False,
                    "error": (
                        "メール本文を解析できませんでした: "
                        f"{converted.get('error') or '原因不明'}"
                    ),
                }
            content = str(converted.get("content") or "")
        else:
            content, encoding = read_safe_text(
                target,
                known_text_extensions=TEXT_EXTENSIONS,
            )
        return {
            "success": True,
            "content": content,
            "path": path,
            "name": target.name,
            "extension": ext,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "converted": is_office or is_mail,
            **({"encoding": encoding} if not (is_office or is_mail) else {}),
        }
    except TextContentError as e:
        return {
            "success": False,
            "error": "バイナリファイルはテキストとして読み取れません",
            "error_code": getattr(e, "error_code", "binary_file"),
        }
    except Exception as e:
        return {"success": False, "error": f"読み取りに失敗: {str(e)}"}


def search_files(
    query: str, root_path: str = "", max_results: int = 20, is_admin: bool = False
) -> Dict[str, Any]:
    """
    Search files by name within a directory tree.

    Args:
        query: Search query (partial filename match)
        root_path: Root directory to search from
        max_results: Maximum number of results
        is_admin: If True, allow access to any path
    """
    if not query or len(query) < 1:
        return {"success": True, "results": [], "total": 0}

    target, valid = _resolve_path(root_path, is_admin=is_admin)
    if not valid or not target.exists() or not target.is_dir():
        return {"success": False, "error": "無効な検索パスです"}

    root = get_root_dir()
    query_lower = query.lower()
    results = []
    count = 0

    try:
        for item in target.rglob("*"):
            if count >= max_results * 5:  # Safety limit for traversal
                break

            if _is_hidden_under(item, target):
                continue

            if _is_link_or_reparse(item) or not item.is_file():
                continue

            if query_lower in item.name.lower():
                try:
                    item.relative_to(root)
                    item_path = str(item.relative_to(root)).replace("\\", "/")
                except ValueError:
                    item_path = str(item).replace("\\", "/")

                stat = item.stat()
                results.append(
                    {
                        "name": item.name,
                        "path": item_path,
                        "type": _get_file_type(item),
                        "extension": item.suffix.lower(),
                        "size_bytes": stat.st_size,
                        "size_display": _format_size(stat.st_size),
                        "icon": _get_icon(item),
                    }
                )
                count += 1

                if count >= max_results:
                    break

        return {
            "success": True,
            "results": results,
            "total": len(results),
            "query": query,
        }
    except Exception as e:
        return {"success": False, "error": f"検索に失敗: {str(e)}"}
