"""
Services package for AoiTalk.

Contains service modules for various functionalities.
"""

from .git_service import (
    GitService,
    GitServiceError,
    ensure_user_git_repository,
    ensure_project_git_repository,
    get_user_directory,
    get_project_directory,
)

__all__ = [
    "GitService",
    "GitServiceError",
    "ensure_user_git_repository",
    "ensure_project_git_repository",
    "get_user_directory",
    "get_project_directory",
]
