"""Specialized agents used by the current runtime."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseAgent
    from .media_agent import MediaAgent
    from .project_management_agent import ProjectManagementAgent

__all__ = [
    "BaseAgent",
    "MediaAgent",
    "ProjectManagementAgent",
]

_AGENT_MODULES = {
    "BaseAgent": "base",
    "MediaAgent": "media_agent",
    "ProjectManagementAgent": "project_management_agent",
}


def __getattr__(name: str):
    module_name = _AGENT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
