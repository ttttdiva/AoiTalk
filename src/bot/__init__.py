"""
Discord Bot package

This module provides Discord bot functionality.
Feature Flag: FEATURE_DISCORD_BOT controls whether these components are loaded.
"""
from src.features import Features

__all__ = ["run_bot"]

_LAZY_IMPORTS = {
    "run_bot": (".discord_bot", "run_bot"),
}
_DISCORD_BOT_ENABLED = Features.discord_bot()


def __getattr__(name: str):
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not _DISCORD_BOT_ENABLED:
        return None

    module_name, attr_name = _LAZY_IMPORTS[name]
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
