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
from .project_context import (
    ProjectContextResolver,
    build_project_context,
    format_project_context_for_chat_prompt,
    format_project_context_for_prompt,
    get_runtime_project_context,
    merge_project_metadata,
    normalize_project_metadata,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)

__all__ = [
    "GitService",
    "GitServiceError",
    "ensure_user_git_repository",
    "ensure_project_git_repository",
    "get_user_directory",
    "get_project_directory",
    "ProjectContextResolver",
    "build_project_context",
    "format_project_context_for_chat_prompt",
    "format_project_context_for_prompt",
    "get_runtime_project_context",
    "merge_project_metadata",
    "normalize_project_metadata",
    "reset_runtime_project_context",
    "sanitize_project_context_for_chat",
    "set_runtime_project_context",
]
