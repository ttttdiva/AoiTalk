"""
File Explorer Service - Core logic for unified file management.

Provides comprehensive file operations with directory structure support.
Replaces the old user_files and integrates document handling.
"""

import base64
import mimetypes
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Constants
MAX_FILE_SIZE_MB = 100
BLOCKED_EXTENSIONS = {'.exe', '.bat', '.cmd', '.sh', '.ps1', '.vbs', '.scr', '.com'}
TEXT_EXTENSIONS = {'.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.csv', '.log', 
                   '.py', '.js', '.ts', '.html', '.css', '.sql', '.ini', '.cfg'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico'}
OFFICE_EXTENSIONS = {'.docx', '.xlsx', '.pptx', '.pdf'}


def get_root_dir() -> Path:
    """Get the workspace root directory (user_files)"""
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    path = Path(files_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _sanitize_name(name: str) -> str:
    """Sanitize file/directory name to prevent path traversal"""
    # Remove path separators and dangerous characters
    name = re.sub(r'[/\\:*?"<>|]', '', name)
    name = name.strip('. ')
    if len(name) > 200:
        name = name[:200]
    if not name:
        name = "unnamed"
    return name


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
        if len(relative_path) >= 2 and relative_path[1] == ':':
            # Windows absolute path (e.g., C:\Users\...)
            target = Path(relative_path).resolve()
            if target.exists():
                return target, True
            return root, False
        elif relative_path.startswith('/'):
            # Unix absolute path
            target = Path(relative_path).resolve()
            if target.exists():
                return target, True
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


def _get_file_type(path: Path) -> str:
    """Determine file type category"""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in TEXT_EXTENSIONS:
        return "text"
    elif ext in OFFICE_EXTENSIONS:
        return "office"
    elif ext in {'.mp4', '.mkv', '.webm', '.avi', '.mov'}:
        return "video"
    elif ext in {'.mp3', '.m4a', '.flac', '.wav', '.ogg'}:
        return "audio"
    else:
        return "binary"


def _get_icon(path: Path, is_dir: bool = False) -> str:
    """Get icon emoji for file/directory"""
    if is_dir:
        return "📁"
    
    ext = path.suffix.lower()
    icons = {
        '.pdf': '📕', '.docx': '📘', '.xlsx': '📗', '.pptx': '📙',
        '.txt': '📄', '.md': '📝', '.json': '📋', '.yaml': '📋', '.yml': '📋',
        '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.html': '🌐', '.css': '🎨',
        '.jpg': '🖼️', '.jpeg': '🖼️', '.png': '🖼️', '.gif': '🖼️', '.webp': '🖼️',
        '.mp4': '🎬', '.mkv': '🎬', '.webm': '🎬',
        '.mp3': '🎵', '.m4a': '🎵', '.flac': '🎵',
        '.zip': '📦', '.rar': '📦', '.7z': '📦',
    }
    return icons.get(ext, '📄')


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
                directories.append({
                    "name": f"{letter}:",
                    "path": f"{letter}:/",
                    "icon": "💾",
                    "item_count": 0,
                    "modified_at": None
                })
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
        "is_admin_mode": True
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
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith('.'):
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
            
            if item.is_dir():
                # Count items in directory
                try:
                    item_count = sum(1 for _ in item.iterdir() if not _.name.startswith('.'))
                except (PermissionError, OSError):
                    item_count = 0
                
                directories.append({
                    "name": item.name,
                    "path": item_path,
                    "icon": "📁",
                    "item_count": item_count,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
            else:
                files.append({
                    "name": item.name,
                    "path": item_path,
                    "icon": _get_icon(item),
                    "type": _get_file_type(item),
                    "extension": item.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "size_display": _format_size(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
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
            "is_admin_mode": is_outside_root or is_admin
        }
        
    except Exception as e:
        return {"success": False, "error": f"ディレクトリの読み取りに失敗: {str(e)}"}


def create_directory(path: str, name: str) -> Dict[str, Any]:
    """
    Create a new directory.
    
    Args:
        path: Parent directory path
        name: New directory name
    """
    parent, valid = _resolve_path(path)
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
        rel_path = str(new_dir.relative_to(root)).replace("\\", "/")
        
        return {
            "success": True,
            "message": f"フォルダ「{safe_name}」を作成しました",
            "name": safe_name,
            "path": rel_path
        }
    except Exception as e:
        return {"success": False, "error": f"フォルダの作成に失敗: {str(e)}"}


def upload_file(path: str, filename: str, content: bytes) -> Dict[str, Any]:
    """
    Upload a file to the specified directory.
    
    Args:
        path: Target directory path
        filename: Name for the uploaded file
        content: File content as bytes
    """
    target_dir, valid = _resolve_path(path)
    if not valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
    
    safe_name = _sanitize_name(filename)
    
    if _is_blocked(safe_name):
        return {"success": False, "error": f"この拡張子はブロックされています: {Path(safe_name).suffix}"}
    
    max_size = MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_size:
        return {"success": False, "error": f"ファイルサイズが制限({MAX_FILE_SIZE_MB}MB)を超えています"}
    
    file_path = target_dir / safe_name
    
    try:
        file_path.write_bytes(content)
        root = get_root_dir()
        rel_path = str(file_path.relative_to(root)).replace("\\", "/")
        
        return {
            "success": True,
            "message": f"ファイル「{safe_name}」をアップロードしました",
            "name": safe_name,
            "path": rel_path,
            "size_bytes": len(content),
            "size_display": _format_size(len(content))
        }
    except Exception as e:
        return {"success": False, "error": f"アップロードに失敗: {str(e)}"}


def download_file(path: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Get file content for download.
    
    Args:
        path: Relative path to file
        
    Returns:
        Tuple of (content_bytes, filename, mime_type) or (None, None, None)
    """
    target, valid = _resolve_path(path)
    
    if not valid or not target.exists() or not target.is_file():
        return None, None, None
    
    try:
        content = target.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(target))
        return content, target.name, mime_type or "application/octet-stream"
    except:
        return None, None, None


def rename_item(path: str, new_name: str) -> Dict[str, Any]:
    """
    Rename a file or directory.
    
    Args:
        path: Path to item
        new_name: New name
    """
    target, valid = _resolve_path(path)
    if not valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}
    
    root = get_root_dir()
    if target == root:
        return {"success": False, "error": "ルートディレクトリは名前変更できません"}
    
    safe_name = _sanitize_name(new_name)
    new_path = target.parent / safe_name
    
    if new_path.exists():
        return {"success": False, "error": f"「{safe_name}」は既に存在します"}
    
    try:
        target.rename(new_path)
        rel_path = str(new_path.relative_to(root)).replace("\\", "/")
        
        return {
            "success": True,
            "message": f"名前を「{safe_name}」に変更しました",
            "new_name": safe_name,
            "new_path": rel_path
        }
    except Exception as e:
        return {"success": False, "error": f"名前変更に失敗: {str(e)}"}


def move_item(src_path: str, dest_path: str) -> Dict[str, Any]:
    """
    Move a file or directory.
    
    Args:
        src_path: Source path
        dest_path: Destination directory path
    """
    src, src_valid = _resolve_path(src_path)
    dest, dest_valid = _resolve_path(dest_path)
    
    if not src_valid or not dest_valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not src.exists():
        return {"success": False, "error": "移動元が見つかりません"}
    
    root = get_root_dir()
    if src == root:
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
        rel_path = str(new_path.relative_to(root)).replace("\\", "/")
        
        return {
            "success": True,
            "message": f"「{src.name}」を移動しました",
            "new_path": rel_path
        }
    except Exception as e:
        return {"success": False, "error": f"移動に失敗: {str(e)}"}


def copy_item(src_path: str, dest_path: str) -> Dict[str, Any]:
    """
    Copy a file or directory.
    
    Args:
        src_path: Source path
        dest_path: Destination directory path
    """
    src, src_valid = _resolve_path(src_path)
    dest, dest_valid = _resolve_path(dest_path)
    
    if not src_valid or not dest_valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not src.exists():
        return {"success": False, "error": "コピー元が見つかりません"}
    
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
        root = get_root_dir()
        if src.is_dir():
            shutil.copytree(str(src), str(new_path))
        else:
            shutil.copy2(str(src), str(new_path))
        
        rel_path = str(new_path.relative_to(root)).replace("\\", "/")
        
        return {
            "success": True,
            "message": f"「{src.name}」をコピーしました",
            "new_path": rel_path,
            "new_name": new_path.name
        }
    except Exception as e:
        return {"success": False, "error": f"コピーに失敗: {str(e)}"}


def delete_item(path: str) -> Dict[str, Any]:
    """
    Delete a file or directory.
    
    Args:
        path: Path to delete
    """
    target, valid = _resolve_path(path)
    if not valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}
    
    root = get_root_dir()
    if target == root:
        return {"success": False, "error": "ルートディレクトリは削除できません"}
    
    try:
        name = target.name
        if target.is_dir():
            shutil.rmtree(str(target))
        else:
            target.unlink()
        
        return {
            "success": True,
            "message": f"「{name}」を削除しました"
        }
    except Exception as e:
        return {"success": False, "error": f"削除に失敗: {str(e)}"}


def get_file_info(path: str) -> Dict[str, Any]:
    """
    Get detailed file/directory information.
    
    Args:
        path: Path to item
    """
    target, valid = _resolve_path(path)
    if not valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not target.exists():
        return {"success": False, "error": "対象が見つかりません"}
    
    try:
        stat = target.stat()
        root = get_root_dir()
        rel_path = str(target.relative_to(root)).replace("\\", "/")
        
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
            info.update({
                "extension": target.suffix.lower(),
                "type": _get_file_type(target),
                "icon": _get_icon(target),
            })
        else:
            info["icon"] = "📁"
            try:
                info["item_count"] = sum(1 for _ in target.iterdir() if not _.name.startswith('.'))
            except:
                info["item_count"] = 0
        
        return info
    except Exception as e:
        return {"success": False, "error": f"情報取得に失敗: {str(e)}"}


def get_preview(path: str, max_chars: int = 5000) -> Dict[str, Any]:
    """
    Get preview content for a file.
    
    Args:
        path: Path to file
        max_chars: Maximum characters for text preview
        
    Returns:
        Preview data depending on file type
    """
    target, valid = _resolve_path(path)
    if not valid:
        return {"success": False, "error": "無効なパスです"}
    
    if not target.exists() or not target.is_file():
        return {"success": False, "error": "ファイルが見つかりません"}
    
    file_type = _get_file_type(target)
    root = get_root_dir()
    rel_path = str(target.relative_to(root)).replace("\\", "/")
    
    try:
        if file_type == "text":
            # Text file preview
            content = target.read_text(encoding='utf-8', errors='replace')
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars] + "..."
            
            return {
                "success": True,
                "type": "text",
                "path": rel_path,
                "content": content,
                "truncated": truncated,
                "extension": target.suffix.lower()
            }
        
        elif file_type == "image":
            # Image: return base64 for inline display
            content = target.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(target))
            b64 = base64.b64encode(content).decode('utf-8')
            
            return {
                "success": True,
                "type": "image",
                "path": rel_path,
                "mime_type": mime_type or "image/png",
                "data_url": f"data:{mime_type or 'image/png'};base64,{b64}",
                "size_bytes": len(content)
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
                    "extension": target.suffix.lower()
                }
            else:
                return {
                    "success": True,
                    "type": "office",
                    "path": rel_path,
                    "content": None,
                    "message": "プレビューを生成できませんでした",
                    "extension": target.suffix.lower()
                }
        
        else:
            # Binary or unsupported type
            return {
                "success": True,
                "type": "binary",
                "path": rel_path,
                "message": "このファイル形式のプレビューはサポートされていません",
                "extension": target.suffix.lower()
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
                if item.name.startswith('.'):
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
                "children": children
            }
        except:
            return None
    
    tree = build_tree(actual_root)
    return {
        "success": True,
        "tree": tree
    }


# ── Bookmark Management ──────────────────────────────────────────────────

# Bookmark file location
import json

def _get_bookmark_file() -> Path:
    """Get the bookmark file path"""
    return Path("config/file_explorer_bookmarks.json")


def load_bookmarks() -> List[Dict[str, str]]:
    """Load bookmarks from JSON file"""
    bookmark_file = _get_bookmark_file()
    
    try:
        if bookmark_file.exists():
            with open(bookmark_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('bookmarks', [])
    except Exception as e:
        print(f"Failed to load bookmarks: {e}")
    
    return []


def save_bookmarks(bookmarks: List[Dict[str, str]]) -> bool:
    """Save bookmarks to JSON file"""
    bookmark_file = _get_bookmark_file()
    
    try:
        # Ensure parent directory exists
        bookmark_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(bookmark_file, 'w', encoding='utf-8') as f:
            json.dump({'bookmarks': bookmarks}, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Failed to save bookmarks: {e}")
        return False


def add_bookmark(name: str, path: str, icon: str = "📁") -> Dict[str, Any]:
    """Add a new bookmark
    
    Args:
        name: Display name for the bookmark
        path: Relative path within user_files
        icon: Emoji icon (default: 📁)
        
    Returns:
        Result dict with success status
    """
    bookmarks = load_bookmarks()
    
    # Check for duplicate
    for bm in bookmarks:
        if bm['path'] == path:
            return {"success": False, "error": "このパスは既にブックマークされています"}
    
    # Validate path exists
    root = get_root_dir()
    full_path = root / path if path else root
    if not full_path.exists():
        return {"success": False, "error": "指定されたパスが存在しません"}
    
    bookmark = {"name": name, "path": path, "icon": icon}
    bookmarks.append(bookmark)
    
    if save_bookmarks(bookmarks):
        return {"success": True, "bookmark": bookmark}
    else:
        return {"success": False, "error": "ブックマークの保存に失敗しました"}


def remove_bookmark(path: str) -> Dict[str, Any]:
    """Remove a bookmark by path
    
    Args:
        path: Path of the bookmark to remove
        
    Returns:
        Result dict with success status
    """
    bookmarks = load_bookmarks()
    
    original_count = len(bookmarks)
    bookmarks = [bm for bm in bookmarks if bm['path'] != path]
    
    if len(bookmarks) == original_count:
        return {"success": False, "error": "ブックマークが見つかりません"}
    
    if save_bookmarks(bookmarks):
        return {"success": True, "message": "ブックマークを削除しました"}
    else:
        return {"success": False, "error": "ブックマークの保存に失敗しました"}


def get_bookmarks() -> Dict[str, Any]:
    """Get all bookmarks
    
    Returns:
        Dict with bookmarks list
    """
    return {
        "success": True,
        "bookmarks": load_bookmarks()
    }
