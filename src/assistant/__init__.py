"""Assistant package with compatibility-preserving lazy public exports.

The concrete assistant modes pull in the LLM, memory, and optional audio
subsystems.  Importing the package itself should not eagerly initialize every
mode when a caller only needs a lower-level assistant helper.  Keep the
historic ``from src.assistant import ...`` API through PEP 562 attribute
resolution while leaving direct submodule imports unchanged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Final


__all__ = [
    "BaseAssistant",
    "VoiceHandler",
    "ResponseHandler",
    "TerminalMode",
    "VoiceChatMode",
]

_LAZY_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "BaseAssistant": (".base", "BaseAssistant"),
    "VoiceHandler": (".voice_handler", "VoiceHandler"),
    "ResponseHandler": (".response_handler", "ResponseHandler"),
    "TerminalMode": (".modes.terminal_mode", "TerminalMode"),
    "VoiceChatMode": (".modes.voice_chat_mode", "VoiceChatMode"),
}


def __getattr__(name: str):
    """Resolve historic package exports on first access and cache them."""

    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy compatibility names to introspection without importing."""

    return sorted(set(globals()) | set(__all__))
