"""
Storage Context Service - Manages user/project storage paths.

Provides context-aware storage root resolution for file explorer.
"""

import os
import stat
from pathlib import Path
from typing import Optional, Tuple
from uuid import UUID
from enum import Enum


def _is_storage_link(path: Path) -> bool:
    """Return True for symlinks and Windows reparse/junction entries."""
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(item_stat.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    if os.name == "nt":
        attributes = getattr(item_stat, "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    return False


def _assert_storage_path_has_no_links(path: Path | str) -> Path:
    """Validate every existing component without following reparse points."""
    candidate = Path(os.path.abspath(os.fspath(path)))
    current = Path(candidate.anchor) if candidate.anchor else Path()
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        if _is_storage_link(current):
            raise OSError(f"Storage path contains a link or reparse point: {current}")
    return candidate


class StorageContextType(str, Enum):
    """Storage context types"""
    PERSONAL = "personal"   # User's personal storage
    PROJECT = "project"     # Project shared storage
    APP = "app"             # App source workspace
    APP_INSTANCE = "app_instance"  # Project-specific App runtime data
    APP_ARTIFACT = "app_artifact"  # Immutable App release artifacts
    LEGACY = "legacy"       # Legacy user_files root (backward compat)


def get_base_storage_dir() -> Path:
    """Get the base storage directory (user_files)"""
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    path = _assert_storage_path_has_no_links(files_dir)
    path.mkdir(parents=True, exist_ok=True)
    _assert_storage_path_has_no_links(path)
    return path.resolve()


def ensure_user_storage(user_id: UUID) -> Path:
    """
    Ensure user's personal storage directory exists.
    
    Args:
        user_id: User UUID
        
    Returns:
        Path to user's storage directory
    """
    base = get_base_storage_dir()
    user_dir = base / "_users" / f"user_{user_id}"
    _assert_storage_path_has_no_links(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    _assert_storage_path_has_no_links(user_dir)
    return user_dir


def ensure_project_storage(project_id: UUID) -> Path:
    """
    Ensure project's shared storage directory exists.
    
    Args:
        project_id: Project UUID
        
    Returns:
        Path to project's storage directory
    """
    base = get_base_storage_dir()
    project_dir = base / "_projects" / f"project_{project_id}"
    _assert_storage_path_has_no_links(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    _assert_storage_path_has_no_links(project_dir)
    return project_dir


def get_user_storage_path(user_id: UUID) -> str:
    """Get relative path to user storage from base"""
    return f"_users/user_{user_id}"


def get_project_storage_path(project_id: UUID) -> str:
    """Get relative path to project storage from base"""
    return f"_projects/project_{project_id}"


def get_context_root(
    context_type: StorageContextType,
    context_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    *,
    app_id: Optional[UUID] = None,
    release_id: Optional[UUID] = None,
    create: bool = True,
) -> Tuple[Path, bool]:
    """
    Get the root directory for a storage context.
    
    Args:
        context_type: Type of storage context
        context_id: Project ID (for PROJECT context; App ID for APP)
        user_id: User ID (for PERSONAL context)
        app_id: App ID for APP_INSTANCE
        release_id: Release ID for APP_ARTIFACT
        
    Returns:
        Tuple of (root_path, is_valid)
    """
    base = get_base_storage_dir()
    
    if context_type == StorageContextType.LEGACY:
        return base, True
    
    elif context_type == StorageContextType.PERSONAL:
        if not user_id:
            return base, False
        user_dir = (
            ensure_user_storage(user_id)
            if create
            else base / "_users" / f"user_{user_id}"
        )
        _assert_storage_path_has_no_links(user_dir)
        return user_dir, True
    
    elif context_type == StorageContextType.PROJECT:
        if not context_id:
            return base, False
        project_dir = (
            ensure_project_storage(context_id)
            if create
            else base / "_projects" / f"project_{context_id}"
        )
        _assert_storage_path_has_no_links(project_dir)
        return project_dir, True

    elif context_type == StorageContextType.APP:
        from ...services.app_storage import ensure_app_workspace

        app_uuid = app_id or context_id
        if not app_uuid:
            return base, False
        return ensure_app_workspace(app_uuid), True

    elif context_type == StorageContextType.APP_INSTANCE:
        from ...services.app_storage import ensure_app_instance

        if not context_id or not app_id:
            return base, False
        return ensure_app_instance(context_id, app_id), True

    elif context_type == StorageContextType.APP_ARTIFACT:
        from ...services.app_storage import ensure_app_artifact

        artifact_app_id = app_id or context_id
        if not artifact_app_id or not release_id:
            return base, False
        return ensure_app_artifact(artifact_app_id, release_id), True
    
    return base, False


def get_available_contexts_for_user(user_id: UUID, projects: list) -> list:
    """
    Get all available storage contexts for a user.
    
    Args:
        user_id: User UUID
        projects: List of project dicts the user is a member of
        
    Returns:
        List of available context dicts
    """
    contexts = [
        {
            "type": StorageContextType.PERSONAL,
            "id": str(user_id),
            "name": "個人ストレージ",
            "icon": "👤"
        }
    ]
    
    # Add project contexts
    for project in projects:
        contexts.append({
            "type": StorageContextType.PROJECT,
            "id": project.get("id"),
            "name": project.get("name", "Project"),
            "icon": "📁"
        })
    
    return contexts


def calculate_storage_usage(root_path: Path, *, strict: bool = False) -> dict:
    """
    Calculate storage usage for a directory.
    
    Args:
        root_path: Root directory to measure
        
    Returns:
        Dict with usage info (total_bytes, file_count, dir_count)
    """
    total_bytes = 0
    file_count = 0
    dir_count = 0
    
    try:
        _assert_storage_path_has_no_links(root_path)
        if not root_path.exists():
            if strict:
                raise OSError(f"Storage root does not exist: {root_path}")
            return {
                "total_bytes": 0,
                "total_mb": 0.0,
                "file_count": 0,
                "dir_count": 0,
            }
        if _is_storage_link(root_path):
            if strict:
                raise OSError(f"Storage root is a link: {root_path}")
            return {
                "total_bytes": 0,
                "total_mb": 0.0,
                "file_count": 0,
                "dir_count": 0,
            }
        def on_walk_error(error: OSError) -> None:
            if strict:
                raise error

        for current_root, directory_names, file_names in os.walk(
            root_path,
            topdown=True,
            onerror=on_walk_error,
            followlinks=False,
        ):
            current = Path(current_root)
            safe_directories = []
            for name in directory_names:
                candidate = current / name
                if strict:
                    try:
                        candidate.lstat()
                    except OSError as error:
                        raise OSError(
                            f"Storage usage scan could not inspect: {candidate}"
                        ) from error
                if not _is_storage_link(candidate):
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            dir_count += len(safe_directories)

            for name in file_names:
                item = current / name
                if strict:
                    try:
                        item.lstat()
                    except OSError as error:
                        raise OSError(
                            f"Storage usage scan could not inspect: {item}"
                        ) from error
                if _is_storage_link(item):
                    continue
                try:
                    item_stat = item.stat()
                except OSError as error:
                    if strict:
                        raise OSError(
                            f"Storage usage scan could not stat: {item}"
                        ) from error
                    continue
                if stat.S_ISREG(item_stat.st_mode):
                    total_bytes += item_stat.st_size
                    file_count += 1
    except (PermissionError, OSError):
        if strict:
            raise
        pass
    
    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "file_count": file_count,
        "dir_count": dir_count
    }
