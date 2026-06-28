"""Specialized agents used by the current runtime."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseAgent
    from .import_agent import ImportAgent
    from .media_agent import MediaAgent
    from .project_management_agent import ProjectManagementAgent
    from .spotify_agent import SpotifyAgent
    from .utility_agent import UtilityAgent
    from .writing_agent import WritingAgent

__all__ = [
    "BaseAgent",
    "SpotifyAgent",
    "UtilityAgent",
    "MediaAgent",
    "ProjectManagementAgent",
    "WritingAgent",
    "ImportAgent",
]

_AGENT_MODULES = {
    "BaseAgent": "base",
    "SpotifyAgent": "spotify_agent",
    "UtilityAgent": "utility_agent",
    "MediaAgent": "media_agent",
    "ProjectManagementAgent": "project_management_agent",
    "WritingAgent": "writing_agent",
    "ImportAgent": "import_agent",
}


def __getattr__(name: str):
    module_name = _AGENT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
