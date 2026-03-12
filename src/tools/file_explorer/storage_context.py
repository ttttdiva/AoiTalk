"""
Storage Context Service - Manages user/project storage paths.

Provides context-aware storage root resolution for file explorer.
"""

import os
from pathlib import Path
from typing import Optional, Tuple
from uuid import UUID
from enum import Enum


class StorageContextType(str, Enum):
    """Storage context types"""
    PERSONAL = "personal"   # User's personal storage
    PROJECT = "project"     # Project shared storage
    LEGACY = "legacy"       # Legacy user_files root (backward compat)


def get_base_storage_dir() -> Path:
    """Get the base storage directory (user_files)"""
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    path = Path(files_dir)
    path.mkdir(parents=True, exist_ok=True)
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
    user_dir.mkdir(parents=True, exist_ok=True)
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
    project_dir.mkdir(parents=True, exist_ok=True)
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
    user_id: Optional[UUID] = None
) -> Tuple[Path, bool]:
    """
    Get the root directory for a storage context.
    
    Args:
        context_type: Type of storage context
        context_id: Project ID (for PROJECT context)
        user_id: User ID (for PERSONAL context)
        
    Returns:
        Tuple of (root_path, is_valid)
    """
    base = get_base_storage_dir()
    
    if context_type == StorageContextType.LEGACY:
        return base, True
    
    elif context_type == StorageContextType.PERSONAL:
        if not user_id:
            return base, False
        user_dir = ensure_user_storage(user_id)
        return user_dir, True
    
    elif context_type == StorageContextType.PROJECT:
        if not context_id:
            return base, False
        project_dir = ensure_project_storage(context_id)
        return project_dir, True
    
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


def calculate_storage_usage(root_path: Path) -> dict:
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
        for item in root_path.rglob("*"):
            if item.is_file():
                total_bytes += item.stat().st_size
                file_count += 1
            elif item.is_dir():
                dir_count += 1
    except (PermissionError, OSError):
        pass
    
    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "file_count": file_count,
        "dir_count": dir_count
    }
