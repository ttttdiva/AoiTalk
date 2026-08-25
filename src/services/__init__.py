"""
Services package for AoiTalk.

Contains service modules for various functionalities.
"""

from .project_context import (
    ProjectContextResolver,
    build_project_context,
    format_minimal_project_context_for_chat_prompt,
    format_project_context_for_chat_prompt,
    format_project_context_for_prompt,
    get_runtime_project_context,
    merge_project_metadata,
    normalize_project_metadata,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)
from .mention_resolver import (
    CanonicalMention,
    MentionResolution,
    MentionResolver,
    normalize_mentions,
    resolve_mentions,
)

__all__ = [
    "ProjectContextResolver",
    "build_project_context",
    "format_minimal_project_context_for_chat_prompt",
    "format_project_context_for_chat_prompt",
    "format_project_context_for_prompt",
    "get_runtime_project_context",
    "merge_project_metadata",
    "normalize_project_metadata",
    "reset_runtime_project_context",
    "sanitize_project_context_for_chat",
    "set_runtime_project_context",
    "CanonicalMention",
    "MentionResolution",
    "MentionResolver",
    "normalize_mentions",
    "resolve_mentions",
]
