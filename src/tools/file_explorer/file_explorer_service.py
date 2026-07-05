"""
File Explorer Service - Core logic for unified file management.

Provides comprehensive file operations with directory structure support.
Replaces the old user_files and integrates document handling.
"""

import base64
import io
import mimetypes
import os
import re
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

# Constants
MAX_FILE_SIZE_MB = 100
SHORTCUT_EXTENSION = ".lnk"
BLOCKED_EXTENSIONS = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".scr", ".com"}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sql",
    ".ini",
    ".cfg",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".pdf"}

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
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    path = Path(files_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


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
    return bool(path) and (
        (len(path) >= 2 and path[1] == ":") or path.startswith("/")
    )


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

    if not relative_path or relative_path == "/":
        return root, True

    # Admin mode: allow absolute paths
    if is_admin:
        # Check if this looks like an absolute path (Windows or Unix)
        if len(relative_path) >= 2 and relative_path[1] == ":":
            # Windows absolute path (e.g., C:\Users\...)
            target = Path(relative_path).resolve()
            return target, True
        elif relative_path.startswith("/"):
            # Unix absolute path
            target = Path(relative_path).resolve()
            return target, True

    if _is_absolute_input(relative_path):
        return root, False

    # Normalize path separators and remove leading slashes
    clean_path = relative_path.replace("\\", "/").strip("/")

    # Resolve to absolute path
    target = (root / clean_path).resolve()

    # Security check: ensure path is within root (skip for admin)
    if is_admin:
        return target, True

    try:
        target.relative_to(root)
        return target, True
    except ValueError:
        return root, False


def _is_blocked(filename: str) -> bool:
    """Check if file extension is blocked"""
    ext = Path(filename).suffix.lower()
    return ext in BLOCKED_EXTENSIONS


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
    candidate = parent / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        next_candidate = parent / f"{stem}_{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def _get_file_type(path: Path) -> str:
    """Determine file type category"""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in TEXT_EXTENSIONS:
        return "text"
    elif ext in OFFICE_EXTENSIONS:
        return "office"
    elif ext in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
        return "video"
    elif ext in {".mp3", ".m4a", ".flac", ".wav", ".ogg"}:
        return "audio"
    else:
        return "binary"


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


def upload_file(
    path: str, filename: str, content: bytes, is_admin: bool = False
) -> Dict[str, Any]:
    """
    Upload a file to the specified directory.

    Args:
        path: Target directory path
        filename: Name for the uploaded file
        content: File content as bytes
    """
    target_dir, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    safe_dirs, safe_name = _sanitize_relative_file_path(filename)

    if _is_blocked(safe_name):
        return {
            "success": False,
            "error": f"この拡張子はブロックされています: {Path(safe_name).suffix}",
        }

    max_size = MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_size:
        return {
            "success": False,
            "error": f"ファイルサイズが制限({MAX_FILE_SIZE_MB}MB)を超えています",
        }

    file_path = target_dir.joinpath(*safe_dirs, safe_name)

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        root = get_root_dir()
        rel_path = _format_path_for_response(file_path, root)

        return {
            "success": True,
            "message": f"ファイル「{safe_name}」をアップロードしました",
            "name": safe_name,
            "path": rel_path,
            "size_bytes": len(content),
            "size_display": _format_size(len(content)),
        }
    except Exception as e:
        return {"success": False, "error": f"アップロードに失敗: {str(e)}"}


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
            archive_name = f"{target.name or 'workspace'}.zip"
            with zipfile.ZipFile(
                buffer, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for file_path in sorted(
                    (
                        p
                        for p in target.rglob("*")
                        if p.is_file() and not p.is_symlink()
                    ),
                    key=lambda p: str(p).lower(),
                ):
                    archive.write(
                        file_path,
                        str(file_path.relative_to(target)).replace("\\", "/"),
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


def _write_selected_items_to_archive(
    archive: zipfile.ZipFile, resolved_items: List[Path]
) -> None:
    for source in resolved_items:
        if source.is_symlink():
            continue
        if source.is_file():
            archive.write(source, source.name)
            continue

        for child in sorted(source.rglob("*"), key=lambda p: str(p).lower()):
            if child.is_symlink():
                continue
            rel = child.relative_to(source)
            arcname = PurePosixPath(source.name, *rel.parts).as_posix()
            if child.is_dir():
                # Preserve empty directories.
                if not any(child.iterdir()):
                    archive.writestr(f"{arcname}/", b"")
            elif child.is_file():
                archive.write(child, arcname)


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

    root = get_root_dir()
    archive_name = _selected_archive_name(resolved_items)
    archive_path = _next_available_path(dest, _sanitize_name(archive_name))

    try:
        with zipfile.ZipFile(
            archive_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            _write_selected_items_to_archive(archive, resolved_items)

        return {
            "success": True,
            "message": f"「{archive_path.name}」を作成しました",
            "archive_name": archive_path.name,
            "archive_path": _format_path_for_response(archive_path, root),
            "count": len(resolved_items),
        }
    except Exception as e:
        try:
            if archive_path.exists():
                archive_path.unlink()
        except OSError:
            pass
        return {"success": False, "error": f"圧縮に失敗: {str(e)}"}


def _safe_zip_member_parts(name: str) -> Optional[Tuple[str, ...]]:
    raw_name = name.replace("\\", "/")
    if not raw_name or raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        return None
    parts = PurePosixPath(raw_name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return tuple(parts)


def extract_archives(
    paths: List[str], dest_path: str = "", is_admin: bool = False
) -> Dict[str, Any]:
    """Extract selected zip archives into folders under dest_path."""
    if not paths:
        return {"success": False, "error": "展開対象が選択されていません"}

    dest, dest_valid = _resolve_path(dest_path, is_admin=is_admin)
    if not dest_valid:
        return {"success": False, "error": "無効な展開先です"}
    if not dest.exists() or not dest.is_dir():
        return {"success": False, "error": "展開先ディレクトリが見つかりません"}

    root = get_root_dir()
    archives: List[Path] = []
    for raw_path in paths:
        target, valid = _resolve_path(raw_path, is_admin=is_admin)
        if not valid or not target.exists() or not target.is_file():
            return {"success": False, "error": f"ZIPファイルが見つかりません: {raw_path}"}
        if target.suffix.lower() != ".zip" or not zipfile.is_zipfile(target):
            return {"success": False, "error": f"ZIPファイルではありません: {target.name}"}
        archives.append(target)

    extracted: List[Dict[str, str]] = []
    created_roots: List[Path] = []
    try:
        for archive_path in archives:
            extract_root = _next_available_path(
                dest, _sanitize_name(archive_path.stem or "archive")
            )
            extract_root_resolved = extract_root.resolve()

            with zipfile.ZipFile(archive_path, mode="r") as archive:
                members = archive.infolist()
                planned: List[Tuple[zipfile.ZipInfo, Path, bool]] = []
                for member in members:
                    parts = _safe_zip_member_parts(member.filename)
                    if parts is None:
                        raise ValueError(
                            f"安全でないパスを含むZIPです: {archive_path.name}"
                        )
                    target_path = extract_root.joinpath(*parts)
                    target_resolved = target_path.resolve()
                    try:
                        target_resolved.relative_to(extract_root_resolved)
                    except ValueError:
                        raise ValueError(
                            f"安全でないパスを含むZIPです: {archive_path.name}"
                        )

                    is_dir = member.is_dir() or member.filename.endswith(("/", "\\"))
                    if not is_dir and _is_blocked(target_path.name):
                        raise ValueError(
                            f"ブロックされた拡張子を含むZIPです: {target_path.name}"
                        )
                    planned.append((member, target_path, is_dir))

                extract_root.mkdir(parents=True, exist_ok=False)
                created_roots.append(extract_root)
                for member, target_path, is_dir in planned:
                    if is_dir:
                        target_path.mkdir(parents=True, exist_ok=True)
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as src, target_path.open("wb") as out:
                        shutil.copyfileobj(src, out)

            extracted.append(
                {
                    "archive_name": archive_path.name,
                    "path": _format_path_for_response(extract_root, root),
                    "name": extract_root.name,
                }
            )

        return {
            "success": True,
            "message": f"{len(extracted)}件のZIPを展開しました",
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
    if target.suffix.lower() == SHORTCUT_EXTENSION:
        resolved = _resolve_shortcut(target)
        if resolved is None or not resolved.exists() or not resolved.is_file():
            return None
        return resolved
    return target


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

    # Ensure dest is a directory
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    elif not dest.is_dir():
        return {"success": False, "error": "移動先はディレクトリである必要があります"}

    new_path = dest / src.name
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


def copy_item(src_path: str, dest_path: str, is_admin: bool = False) -> Dict[str, Any]:
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

    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    elif not dest.is_dir():
        return {"success": False, "error": "コピー先はディレクトリである必要があります"}

    new_path = dest / src.name

    # Handle name collision
    if new_path.exists():
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
            "message": f"「{src.name}」をコピーしました",
            "new_path": rel_path,
            "new_name": new_path.name,
        }
    except Exception as e:
        return {"success": False, "error": f"コピーに失敗: {str(e)}"}


def delete_item(path: str, is_admin: bool = False) -> Dict[str, Any]:
    """
    Delete a file or directory.

    Args:
        path: Path to delete
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
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(str(target), onerror=_remove_readonly)
        else:
            target.unlink()

        return {"success": True, "message": f"「{name}」を削除しました"}
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


def get_preview(
    path: str, max_chars: int = 5000, is_admin: bool = False
) -> Dict[str, Any]:
    """
    Get preview content for a file.

    Args:
        path: Path to file
        max_chars: Maximum characters for text preview

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
        if file_type == "text":
            # Text file preview
            content = target.read_text(encoding="utf-8", errors="replace")
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars] + "..."

            return {
                "success": True,
                "type": "text",
                "path": rel_path,
                "content": content,
                "truncated": truncated,
                "extension": target.suffix.lower(),
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
            # Try to convert Office document to text
            preview_text = _convert_office_to_text(target)
            if preview_text:
                truncated = len(preview_text) > max_chars
                if truncated:
                    preview_text = preview_text[:max_chars] + "..."

                return {
                    "success": True,
                    "type": "office",
                    "path": rel_path,
                    "content": preview_text,
                    "truncated": truncated,
                    "extension": target.suffix.lower(),
                }
            else:
                return {
                    "success": True,
                    "type": "office",
                    "path": rel_path,
                    "content": None,
                    "message": "プレビューを生成できませんでした",
                    "extension": target.suffix.lower(),
                }

        else:
            # Binary or unsupported type
            return {
                "success": True,
                "type": "binary",
                "path": rel_path,
                "message": "このファイル形式のプレビューはサポートされていません",
                "extension": target.suffix.lower(),
            }

    except Exception as e:
        return {"success": False, "error": f"プレビュー生成に失敗: {str(e)}"}


def _convert_office_to_text(file_path: Path) -> Optional[str]:
    """Convert Office document to text using markitdown if available"""
    try:
        from ..documents.office_reader import convert_office_bytes_to_markdown

        content = file_path.read_bytes()
        result = convert_office_bytes_to_markdown(content, file_path.name)
        return result.get("content") if result.get("success") else None
    except:
        return None


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

    # Determine actual root for tree
    if root_path:
        actual_root = workspace_root / root_path
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


def inspect_workspace_tree(
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

    def walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or truncated:
            return
        try:
            children = sorted(
                current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
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


def find_workspace_items(
    query: str,
    path: str = "",
    include_dirs: bool = True,
    include_files: bool = True,
    max_results: int = 50,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Search workspace items by name, including directories."""
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
    results: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    try:
        for item in target.rglob("*"):
            scanned += 1
            if scanned > 10000:
                truncated = True
                break
            if item.name.startswith("."):
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
            if query_lower not in item.name.lower():
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

    ext = target.suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        return {"success": False, "error": f"この拡張子はブロックされています: {ext}"}

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
        Full content without truncation (max 1MB)
    """
    target, valid = _resolve_path(path, is_admin=is_admin)
    if not valid:
        return {"success": False, "error": "無効なパスです"}

    if not target.exists() or not target.is_file():
        return {"success": False, "error": "ファイルが見つかりません"}

    stat = target.stat()
    if stat.st_size > MAX_EDITOR_FILE_SIZE:
        return {
            "success": False,
            "error": "ファイルサイズが1MBを超えています。エディタでは開けません。",
        }

    ext = target.suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        return {"success": False, "error": "テキストファイルのみ対応しています"}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "content": content,
            "path": path,
            "name": target.name,
            "extension": ext,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
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

            if item.name.startswith("."):
                continue

            if not item.is_file():
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
