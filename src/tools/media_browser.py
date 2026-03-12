#!/usr/bin/env python3
"""
Media Browser - Local media file browsing functionality

Provides directory scanning and file serving for images and videos
with free navigation from a configurable root path and bookmark support.
Supports Windows shortcut (.lnk) files.
Supports video thumbnail generation via FFmpeg.
"""

import os
import re
import json
import struct
import logging
import subprocess
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


def _natural_sort_key(path: Path):
    """
    Generate a sort key for natural sorting.
    Splits the filename into text and number parts so that
    '1.png' < '2.png' < '10.png' instead of '1.png' < '10.png' < '2.png'
    """
    name = path.name.lower()
    # Split into text and number parts
    parts = re.split(r'(\d+)', name)
    # Convert number parts to integers for proper numeric comparison
    return [int(part) if part.isdigit() else part for part in parts]


# Supported file extensions
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.wmv'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac', '.wma', '.opus'}
SHORTCUT_EXTENSION = '.lnk'

# Configuration cache
_config_cache: Dict[str, Any] = {}


def _get_config() -> Dict[str, Any]:
    """Get media browser configuration from environment"""
    global _config_cache
    
    if _config_cache:
        return _config_cache
    
    root_path = os.environ.get('MEDIA_ROOT_PATH', '')
    bookmarks_file = os.environ.get('MEDIA_BOOKMARKS_FILE', 'config/media_bookmarks.json')
    thumbnail_cache = os.environ.get('VIDEO_THUMBNAIL_CACHE', 'cache/video_thumbnails')
    
    _config_cache = {
        'root_path': Path(root_path) if root_path else None,
        'bookmarks_file': Path(bookmarks_file),
        'thumbnail_cache': Path(thumbnail_cache)
    }
    
    if _config_cache['root_path'] and _config_cache['root_path'].exists():
        logger.info(f"Media root path configured: {root_path}")
    else:
        logger.warning(f"Media root path not configured or does not exist: {root_path}")
    
    return _config_cache


def _is_safe_path(target_path: Path, root_path: Path) -> bool:
    """
    Check if target_path is safely within root_path
    (prevents path traversal attacks)
    """
    try:
        resolved_root = root_path.resolve()
        resolved_target = target_path.resolve()
        return str(resolved_target).startswith(str(resolved_root)) or resolved_target == resolved_root
    except Exception:
        return False


def resolve_lnk_target(lnk_path: Path) -> Optional[Path]:
    """
    Parse Windows .lnk shortcut file and return the target path.
    Pure Python implementation - no external dependencies.
    
    Args:
        lnk_path: Path to the .lnk file
        
    Returns:
        Target path if successfully resolved, None otherwise
    """
    try:
        with open(lnk_path, 'rb') as f:
            content = f.read()
        
        # Check LNK signature (4C 00 00 00)
        if len(content) < 76 or content[:4] != b'\x4c\x00\x00\x00':
            return None
        
        # Read flags at offset 0x14 (20)
        flags = struct.unpack('<I', content[0x14:0x18])[0]
        
        has_link_target_id_list = flags & 0x01
        has_link_info = flags & 0x02
        
        offset = 0x4C  # Start after header (76 bytes)
        
        # Skip LinkTargetIDList if present
        if has_link_target_id_list:
            if offset + 2 > len(content):
                return None
            id_list_size = struct.unpack('<H', content[offset:offset+2])[0]
            offset += 2 + id_list_size
        
        # Parse LinkInfo if present
        if has_link_info:
            if offset + 4 > len(content):
                return None
            
            link_info_size = struct.unpack('<I', content[offset:offset+4])[0]
            link_info_start = offset
            
            if offset + 28 > len(content):
                return None
            
            # LinkInfoHeaderSize at offset+4
            link_info_header_size = struct.unpack('<I', content[offset+4:offset+8])[0]
            
            # LinkInfoFlags at offset+8
            link_info_flags = struct.unpack('<I', content[offset+8:offset+12])[0]
            
            # Check if VolumeIDAndLocalBasePath is present
            if link_info_flags & 0x01:
                # LocalBasePathOffset at offset+16
                local_base_path_offset = struct.unpack('<I', content[offset+16:offset+20])[0]
                
                # Read LocalBasePath (null-terminated string)
                path_start = link_info_start + local_base_path_offset
                path_end = content.find(b'\x00', path_start)
                if path_end == -1:
                    path_end = len(content)
                
                local_path = content[path_start:path_end].decode('mbcs', errors='ignore')
                
                if local_path:
                    target = Path(local_path)
                    if target.exists():
                        return target
            
            # Try Unicode path if available (LinkInfoHeaderSize >= 0x24)
            if link_info_header_size >= 0x24 and offset + 0x24 <= len(content):
                # LocalBasePathOffsetUnicode at offset+0x1C
                unicode_offset_pos = offset + 0x1C
                if unicode_offset_pos + 4 <= len(content):
                    unicode_path_offset = struct.unpack('<I', content[unicode_offset_pos:unicode_offset_pos+4])[0]
                    if unicode_path_offset > 0:
                        path_start = link_info_start + unicode_path_offset
                        # Find null-terminated UTF-16 string
                        path_end = path_start
                        while path_end + 1 < len(content):
                            if content[path_end:path_end+2] == b'\x00\x00':
                                break
                            path_end += 2
                        
                        try:
                            unicode_path = content[path_start:path_end].decode('utf-16-le', errors='ignore')
                            if unicode_path:
                                target = Path(unicode_path)
                                if target.exists():
                                    return target
                        except:
                            pass
        
        return None
        
    except Exception as e:
        logger.debug(f"Failed to parse shortcut {lnk_path}: {e}")
        return None


def _get_file_info(file_path: Path, resolve_shortcuts: bool = True) -> Optional[Dict[str, Any]]:
    """Get file metadata, optionally resolving shortcuts"""
    try:
        ext = file_path.suffix.lower()
        
        # Handle shortcuts
        actual_path = file_path
        is_shortcut = False
        
        if ext == SHORTCUT_EXTENSION and resolve_shortcuts:
            target = resolve_lnk_target(file_path)
            if target and target.is_file():
                actual_path = target
                ext = actual_path.suffix.lower()
                is_shortcut = True
            else:
                # Skip shortcuts that can't be resolved or point to non-files
                return None
        
        # Check if it's a media file
        if ext not in IMAGE_EXTENSIONS and ext not in VIDEO_EXTENSIONS and ext not in AUDIO_EXTENSIONS:
            return None
        
        stat = actual_path.stat()
        
        file_type = "other"
        if ext in IMAGE_EXTENSIONS:
            file_type = "image"
        elif ext in VIDEO_EXTENSIONS:
            file_type = "video"
        elif ext in AUDIO_EXTENSIONS:
            file_type = "audio"
        
        result = {
            "name": file_path.name if is_shortcut else actual_path.name,
            "path": str(actual_path),  # Always use actual path for serving
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "type": file_type,
            "extension": ext
        }
        
        if is_shortcut:
            result["is_shortcut"] = True
            result["shortcut_path"] = str(file_path)
        
        return result
    except Exception as e:
        logger.error(f"Failed to get file info for {file_path}: {e}")
        return None


# ── Video Thumbnail Generation ─────────────────────────────────────────────


def _get_thumbnail_cache_path(video_path: Path) -> Path:
    """Get the cache path for a video thumbnail"""
    config = _get_config()
    cache_dir = config['thumbnail_cache']
    
    # Create a hash of the video path for cache filename
    path_hash = hashlib.md5(str(video_path).encode()).hexdigest()
    return cache_dir / f"{path_hash}.jpg"


def _check_ffmpeg_available() -> bool:
    """Check if FFmpeg is available on the system"""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False


def generate_video_thumbnail(video_path: Path, force: bool = False) -> Optional[Path]:
    """
    Generate a thumbnail for a video file using FFmpeg.
    
    Args:
        video_path: Path to the video file
        force: If True, regenerate even if cached version exists
    
    Returns:
        Path to the thumbnail image, or None if generation failed
    """
    if not video_path.exists() or not video_path.is_file():
        return None
    
    ext = video_path.suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        return None
    
    cache_path = _get_thumbnail_cache_path(video_path)
    
    # Check if cached thumbnail exists and is newer than video
    if not force and cache_path.exists():
        try:
            video_mtime = video_path.stat().st_mtime
            cache_mtime = cache_path.stat().st_mtime
            if cache_mtime >= video_mtime:
                return cache_path
        except Exception:
            pass
    
    # Ensure cache directory exists
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        # Use FFmpeg to extract a frame at 1 second (or 10% of video)
        # -ss 1: seek to 1 second
        # -vframes 1: extract 1 frame
        # -vf scale=480:-1: scale to 480px width, maintain aspect ratio
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-ss', '1',
                '-i', str(video_path),
                '-vframes', '1',
                '-vf', 'scale=480:-1',
                '-q:v', '2',
                str(cache_path)
            ],
            capture_output=True,
            timeout=30
        )
        
        if result.returncode == 0 and cache_path.exists():
            logger.debug(f"Generated thumbnail for {video_path}")
            return cache_path
        else:
            # Try at 0 seconds for very short videos
            result = subprocess.run(
                [
                    'ffmpeg', '-y',
                    '-ss', '0',
                    '-i', str(video_path),
                    '-vframes', '1',
                    '-vf', 'scale=480:-1',
                    '-q:v', '2',
                    str(cache_path)
                ],
                capture_output=True,
                timeout=30
            )
            if result.returncode == 0 and cache_path.exists():
                logger.debug(f"Generated thumbnail for {video_path} (at 0s)")
                return cache_path
            logger.warning(f"FFmpeg failed to generate thumbnail for {video_path}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.warning(f"Thumbnail generation timed out for {video_path}")
        return None
    except FileNotFoundError:
        logger.warning("FFmpeg not found. Video thumbnails will not be available.")
        return None
    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {video_path}: {e}")
        return None


def get_video_thumbnail_path(video_path: str) -> Optional[Path]:
    """
    Get the thumbnail path for a video file.
    Generates the thumbnail if it doesn't exist.
    
    Args:
        video_path: Absolute path to the video file
    
    Returns:
        Path to the thumbnail image, or None if unavailable
    """
    video = Path(video_path)
    return generate_video_thumbnail(video)


# ── Bookmark Management ──────────────────────────────────────────────────


def load_bookmarks() -> List[Dict[str, str]]:
    """Load bookmarks from JSON file"""
    config = _get_config()
    bookmarks_file = config['bookmarks_file']
    
    try:
        if bookmarks_file.exists():
            with open(bookmarks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('bookmarks', [])
    except Exception as e:
        logger.error(f"Failed to load bookmarks: {e}")
    
    return []


def save_bookmarks(bookmarks: List[Dict[str, str]]) -> bool:
    """Save bookmarks to JSON file"""
    config = _get_config()
    bookmarks_file = config['bookmarks_file']
    
    try:
        # Ensure parent directory exists
        bookmarks_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(bookmarks_file, 'w', encoding='utf-8') as f:
            json.dump({'bookmarks': bookmarks}, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save bookmarks: {e}")
        return False


def add_bookmark(name: str, path: str, icon: str = "📁") -> Dict[str, Any]:
    """Add a new bookmark"""
    bookmarks = load_bookmarks()
    
    # Check for duplicate
    for bm in bookmarks:
        if bm['path'] == path:
            return {"success": False, "error": "このパスは既にブックマークされています"}
    
    # Validate path exists
    if not Path(path).exists():
        return {"success": False, "error": "指定されたパスが存在しません"}
    
    bookmark = {"name": name, "path": path, "icon": icon}
    bookmarks.append(bookmark)
    
    if save_bookmarks(bookmarks):
        return {"success": True, "bookmark": bookmark}
    else:
        return {"success": False, "error": "ブックマークの保存に失敗しました"}


def remove_bookmark(path: str) -> Dict[str, Any]:
    """Remove a bookmark by path"""
    bookmarks = load_bookmarks()
    
    original_count = len(bookmarks)
    bookmarks = [bm for bm in bookmarks if bm['path'] != path]
    
    if len(bookmarks) == original_count:
        return {"success": False, "error": "ブックマークが見つかりません"}
    
    if save_bookmarks(bookmarks):
        return {"success": True, "message": "ブックマークを削除しました"}
    else:
        return {"success": False, "error": "ブックマークの保存に失敗しました"}


# ── Media Browser Core ──────────────────────────────────────────────────


def _get_folder_thumbnail(folder_path: Path, max_depth: int = 2) -> Optional[str]:
    """
    Find the first image in a folder to use as thumbnail.
    Searches recursively up to max_depth levels.
    
    Args:
        folder_path: Path to the folder
        max_depth: Maximum recursion depth (default 2)
    
    Returns:
        Absolute path to first image found, or None
    """
    if max_depth <= 0:
        return None
    
    try:
        items = sorted(folder_path.iterdir(), key=_natural_sort_key)
        
        # First pass: look for images in current folder
        for item in items:
            if item.name.startswith('.') or item.name.startswith('~'):
                continue
            
            if item.is_file():
                ext = item.suffix.lower()
                
                # Handle shortcuts
                if ext == SHORTCUT_EXTENSION:
                    target = resolve_lnk_target(item)
                    if target and target.is_file():
                        target_ext = target.suffix.lower()
                        if target_ext in IMAGE_EXTENSIONS:
                            return str(target)
                elif ext in IMAGE_EXTENSIONS:
                    return str(item)
        
        # Second pass: recurse into subfolders
        for item in items:
            if item.name.startswith('.') or item.name.startswith('~'):
                continue
            
            if item.is_dir():
                thumbnail = _get_folder_thumbnail(item, max_depth - 1)
                if thumbnail:
                    return thumbnail
        
        return None
    except Exception as e:
        logger.debug(f"Failed to get folder thumbnail for {folder_path}: {e}")
        return None


def get_media_config() -> Dict[str, Any]:
    """
    Get media browser configuration including root path and bookmarks
    
    Returns:
        {
            "configured": True,
            "root_path": "/path/to/media",
            "bookmarks": [...]
        }
    """
    config = _get_config()
    root_path = config['root_path']
    
    if not root_path or not root_path.exists():
        return {
            "configured": False,
            "root_path": None,
            "bookmarks": []
        }
    
    return {
        "configured": True,
        "root_path": str(root_path),
        "bookmarks": load_bookmarks()
    }


def list_folder_contents(path: str = "") -> Dict[str, Any]:
    """
    List contents of a directory
    
    Args:
        path: Absolute path to browse (empty for root path)
    
    Returns:
        {
            "success": True,
            "current_path": "/path/to/media/subfolder",
            "parent_path": "/path/to/media" or null,
            "can_go_up": True,
            "is_bookmarked": False,
            "folders": [...],
            "files": [...]
        }
    """
    config = _get_config()
    root_path = config['root_path']
    
    if not root_path or not root_path.exists():
        return {
            "success": False,
            "error": "メディアルートパスが設定されていません"
        }
    
    # Determine target path
    if path:
        target_path = Path(path)
    else:
        target_path = root_path
    
    # Note: Security check removed to allow full folder navigation
    # This is a local application, so path traversal is acceptable
    
    if not target_path.exists():
        return {
            "success": False,
            "error": "パスが存在しません"
        }
    
    if not target_path.is_dir():
        return {
            "success": False,
            "error": "パスがディレクトリではありません"
        }
    
    # List folder contents
    folders = []
    files = []
    
    try:
        for item in sorted(target_path.iterdir(), key=_natural_sort_key):
            # Skip hidden files and system files
            if item.name.startswith('.') or item.name.startswith('~'):
                continue
            
            if item.is_dir():
                # Count items in subfolder
                try:
                    item_count = len(list(item.iterdir()))
                except:
                    item_count = 0
                
                # Get folder thumbnail
                thumbnail = _get_folder_thumbnail(item)
                
                folder_data = {
                    "name": item.name,
                    "path": str(item),
                    "item_count": item_count
                }
                
                if thumbnail:
                    folder_data["thumbnail"] = thumbnail
                
                folders.append(folder_data)
            elif item.is_file():
                ext = item.suffix.lower()
                
                # Include images, videos, audio files, and shortcuts (which may point to media)
                if ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS or ext == SHORTCUT_EXTENSION:
                    file_info = _get_file_info(item)
                    if file_info:
                        files.append(file_info)
        
    except PermissionError:
        return {
            "success": False,
            "error": "アクセスが拒否されました"
        }
    except Exception as e:
        logger.error(f"Failed to list folder contents: {e}")
        return {
            "success": False,
            "error": str(e)
        }
    
    # Calculate parent path - allow navigation to any parent directory
    resolved_target = target_path.resolve()
    
    parent_path = None
    can_go_up = False
    
    # Allow going up as long as there is a parent directory
    parent = target_path.parent
    if parent != target_path and parent.exists():
        parent_path = str(parent)
        can_go_up = True
    
    # Check if current path is bookmarked
    bookmarks = load_bookmarks()
    is_bookmarked = any(bm['path'] == str(target_path) for bm in bookmarks)
    
    return {
        "success": True,
        "current_path": str(target_path),
        "parent_path": parent_path,
        "can_go_up": can_go_up,
        "is_bookmarked": is_bookmarked,
        "folders": folders,
        "files": files,
        "total_folders": len(folders),
        "total_files": len(files)
    }


def get_file_path(path: str) -> Optional[Path]:
    """
    Get validated file path for serving
    
    Args:
        path: Absolute path to file (or shortcut target path)
    
    Returns:
        Resolved Path if valid, None otherwise
    """
    file_path = Path(path)
    
    # If it's a shortcut, resolve it
    if file_path.suffix.lower() == SHORTCUT_EXTENSION:
        target = resolve_lnk_target(file_path)
        if target and target.is_file():
            file_path = target
        else:
            return None
    else:
        # For regular files, verify they exist
        if not file_path.exists() or not file_path.is_file():
            return None
    
    return file_path


def get_media_mime_type(file_path: Path) -> str:
    """Get MIME type for a media file"""
    import mimetypes
    
    ext = file_path.suffix.lower()
    
    # Common MIME types
    mime_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.mp4': 'video/mp4',
        '.mkv': 'video/x-matroska',
        '.webm': 'video/webm',
        '.avi': 'video/x-msvideo',
        '.mov': 'video/quicktime',
        '.wmv': 'video/x-ms-wmv',
        # Audio formats
        '.mp3': 'audio/mpeg',
        '.m4a': 'audio/mp4',
        '.flac': 'audio/flac',
        '.wav': 'audio/wav',
        '.ogg': 'audio/ogg',
        '.aac': 'audio/aac',
        '.wma': 'audio/x-ms-wma',
        '.opus': 'audio/opus'
    }
    
    return mime_map.get(ext, mimetypes.guess_type(str(file_path))[0] or 'application/octet-stream')
