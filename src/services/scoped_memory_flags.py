"""Feature flags for the Scoped Memory v2 rollout."""

from __future__ import annotations

import os


def _enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def scoped_memory_v2_enabled() -> bool:
    return _enabled("SCOPED_MEMORY_V2_ENABLED", True)


def legacy_agent_memory_read_enabled() -> bool:
    return _enabled("LEGACY_AGENT_MEMORY_READ_ENABLED", True)


def legacy_agent_memory_write_enabled() -> bool:
    return _enabled("LEGACY_AGENT_MEMORY_WRITE_ENABLED", False)


__all__ = [
    "legacy_agent_memory_read_enabled",
    "legacy_agent_memory_write_enabled",
    "scoped_memory_v2_enabled",
]
