"""Memory management module for AoiTalk.

Keep package imports light. Several memory services depend on embedding
libraries, and importing them from package initialization makes unrelated
modules such as ``src.memory.history`` load those heavy dependencies.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ConversationMemoryManager": (".manager", "ConversationMemoryManager"),
    "ConversationSession": (".models", "ConversationSession"),
    "ConversationMessage": (".models", "ConversationMessage"),
    "ConversationArchive": (".models", "ConversationArchive"),
    "SummarizationService": (".services", "SummarizationService"),
    "MemorySearchService": (".services", "MemorySearchService"),
    "CrossSessionMemoryService": (".cross_session_memory", "CrossSessionMemoryService"),
    "get_cross_session_memory": (".cross_session_memory", "get_cross_session_memory"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
