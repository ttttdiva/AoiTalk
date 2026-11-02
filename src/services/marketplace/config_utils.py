"""Small config readers shared by marketplace integrations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read one dotted setting without depending on retired Team services."""

    if config is None:
        return default
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return default if value is None else value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return default


__all__ = ["config_get"]
