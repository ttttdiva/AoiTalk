"""
User Files service - Core logic for file management.

Provides upload, download, list, and delete operations for user files.
Similar to document_tools but supports any file format.
"""

import base64
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Constants
MAX_FILE_SIZE_MB = 50  # Default max file size
BLOCKED_EXTENSIONS = {'.exe', '.bat', '.cmd', '.sh', '.ps1', '.vbs', '.js'}


def _get_user_files_dir() -> Path:
    """Get the user files directory path from environment or default"""
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    path = Path(files_dir)
    
    # Create directory if it doesn't exist
    path.mkdir(parents=True, exist_ok=True)
    
    return path.resolve()


def _sanitize_filename(name: str) -> str:
    """
    Sanitize filename to prevent path traversal attacks.
    Only allows safe characters.
    """
    # Remove path separators and dangerous characters
    name = re.sub(r'[/\\:*?"<>|]', '', name)
    # Remove leading/trailing dots and spaces
    name = name.strip('. ')
    # Limit length
    if len(name) > 200:
        name = name[:200]
    # Ensure name is not empty
    if not name:
        name = "unnamed_file"
    return name


def _get_file_path(filename: str) -> Path:
    """Get the full path for a file, ensuring it stays within user_files dir"""
    files_dir = _get_user_files_dir()
    safe_name = _sanitize_filename(filename)
    
    path = files_dir / safe_name
    
    # Security check: ensure path is within user_files directory
    try:
        path.resolve().relative_to(files_dir)
    except ValueError:
        raise ValueError(f"Invalid filename: {filename}")
    
    return path


def _is_blocked_extension(filename: str) -> bool:
    """Check if file extension is blocked"""
    ext = Path(filename).suffix.lower()
    return ext in BLOCKED_EXTENSIONS


def _format_file_size(size_bytes: int) -> str:
    """Format file size for display"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


# ── Implementation Functions ─────────────────────────────────────────────


def upload_file_impl(filename: str, content_base64: str) -> Dict[str, Any]:
    """
    Upload a file with base64 encoded content.
    
    Args:
        filename: Name of the file to create
        content_base64: Base64 encoded file content
        
    Returns:
        Result dict with success status and file info
    """
    try:
        # Check blocked extensions
        if _is_blocked_extension(filename):
            return {
                "success": False,
                "error": f"この拡張子のファイルはアップロードできません: {Path(filename).suffix}",
                "filename": filename
            }
        
        # Decode content
        try:
            content = base64.b64decode(content_base64)
        except Exception:
            return {
                "success": False,
                "error": "Base64デコードに失敗しました",
                "filename": filename
            }
        
        # Check file size
        max_size = MAX_FILE_SIZE_MB * 1024 * 1024
        if len(content) > max_size:
            return {
                "success": False,
                "error": f"ファイルサイズが制限({MAX_FILE_SIZE_MB}MB)を超えています",
                "filename": filename
            }
        
        path = _get_file_path(filename)
        
        # Write file
        path.write_bytes(content)
        
        return {
            "success": True,
            "message": f"ファイル「{filename}」をアップロードしました",
            "filename": path.name,
            "path": str(path),
            "size_bytes": len(content),
            "size_display": _format_file_size(len(content))
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイルのアップロードに失敗しました: {str(e)}",
            "filename": filename
        }


def upload_file_bytes_impl(filename: str, content: bytes) -> Dict[str, Any]:
    """
    Upload a file with raw bytes content (for API use).
    
    Args:
        filename: Name of the file to create
        content: Raw bytes content
        
    Returns:
        Result dict with success status and file info
    """
    try:
        # Check blocked extensions
        if _is_blocked_extension(filename):
            return {
                "success": False,
                "error": f"この拡張子のファイルはアップロードできません: {Path(filename).suffix}",
                "filename": filename
            }
        
        # Check file size
        max_size = MAX_FILE_SIZE_MB * 1024 * 1024
        if len(content) > max_size:
            return {
                "success": False,
                "error": f"ファイルサイズが制限({MAX_FILE_SIZE_MB}MB)を超えています",
                "filename": filename
            }
        
        path = _get_file_path(filename)
        
        # Write file
        path.write_bytes(content)
        
        return {
            "success": True,
            "message": f"ファイル「{filename}」をアップロードしました",
            "filename": path.name,
            "path": str(path),
            "size_bytes": len(content),
            "size_display": _format_file_size(len(content))
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイルのアップロードに失敗しました: {str(e)}",
            "filename": filename
        }


def download_file_impl(filename: str) -> Dict[str, Any]:
    """
    Download a file as base64 encoded content.
    
    Args:
        filename: Name of the file to download
        
    Returns:
        Result dict with success status and base64 content
    """
    try:
        path = _get_file_path(filename)
        
        if not path.exists():
            return {
                "success": False,
                "error": f"ファイル「{filename}」が見つかりません",
                "filename": filename
            }
        
        content = path.read_bytes()
        content_base64 = base64.b64encode(content).decode('utf-8')
        stat = path.stat()
        
        return {
            "success": True,
            "filename": path.name,
            "content_base64": content_base64,
            "size_bytes": stat.st_size,
            "size_display": _format_file_size(stat.st_size),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイルのダウンロードに失敗しました: {str(e)}",
            "filename": filename
        }


def get_file_bytes_impl(filename: str) -> Optional[bytes]:
    """
    Get file content as raw bytes (for API streaming).
    
    Args:
        filename: Name of the file
        
    Returns:
        File content as bytes, or None if not found
    """
    try:
        path = _get_file_path(filename)
        if path.exists():
            return path.read_bytes()
        return None
    except Exception:
        return None


def get_file_path_impl(filename: str) -> Optional[Path]:
    """
    Get file path for serving (for API streaming).
    
    Args:
        filename: Name of the file
        
    Returns:
        Path object if file exists, None otherwise
    """
    try:
        path = _get_file_path(filename)
        if path.exists():
            return path
        return None
    except Exception:
        return None


def list_files_impl() -> Dict[str, Any]:
    """
    List all user files.
    
    Returns:
        Result dict with file list
    """
    try:
        files_dir = _get_user_files_dir()
        files: List[Dict[str, Any]] = []
        
        for path in sorted(files_dir.iterdir()):
            if path.is_file() and not path.name.startswith('.'):
                stat = path.stat()
                files.append({
                    "filename": path.name,
                    "extension": path.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "size_display": _format_file_size(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
        return {
            "success": True,
            "count": len(files),
            "files": files,
            "message": f"{len(files)}件のファイルが見つかりました" if files else "ファイルがありません"
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイル一覧の取得に失敗しました: {str(e)}",
            "files": []
        }


def delete_file_impl(filename: str) -> Dict[str, Any]:
    """
    Delete a user file.
    
    Args:
        filename: Name of the file to delete
        
    Returns:
        Result dict with success status
    """
    try:
        path = _get_file_path(filename)
        
        if not path.exists():
            return {
                "success": False,
                "error": f"ファイル「{filename}」が見つかりません",
                "filename": filename
            }
        
        path.unlink()
        
        return {
            "success": True,
            "message": f"ファイル「{filename}」を削除しました",
            "filename": filename
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイルの削除に失敗しました: {str(e)}",
            "filename": filename
        }


def get_file_info_impl(filename: str) -> Dict[str, Any]:
    """
    Get file metadata.
    
    Args:
        filename: Name of the file
        
    Returns:
        Result dict with file metadata
    """
    try:
        path = _get_file_path(filename)
        
        if not path.exists():
            return {
                "success": False,
                "error": f"ファイル「{filename}」が見つかりません",
                "filename": filename
            }
        
        stat = path.stat()
        
        return {
            "success": True,
            "filename": path.name,
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "size_display": _format_file_size(stat.st_size),
            "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "path": str(path)
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"ファイル情報の取得に失敗しました: {str(e)}",
            "filename": filename
        }
