"""Compatibility helpers for the Docs Library rename.

The Docs graph used ``workspace``/``workspace_id`` in the first mobile and
Python APIs.  The canonical internal vocabulary is now ``library`` and
``docs_library_id``.  Keep the old keys at the wire boundary while ensuring
new code has one explicit place to perform the dual-read/dual-write mapping.
Filesystem/worktree ``workspace`` values are intentionally not handled here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


LEGACY_DOCS_LIBRARY_KEYS = ("docs_library_id", "docsLibraryId", "workspace_id", "workspaceId")


def read_docs_library_id(payload: Mapping[str, Any] | None, default: Any = None) -> Any:
    """Read the canonical scope key, accepting legacy snake/camel keys.

    Canonical keys win when a malformed client sends both values.  A caller
    can pass ``default`` to preserve its existing missing-value behaviour.
    """

    if payload is None:
        return default
    for key in LEGACY_DOCS_LIBRARY_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def read_docs_library_id_from_object(value: Any, default: Any = None) -> Any:
    """Read a scope id from an ORM row or a legacy test double."""

    if value is None:
        return default
    for key in ("docs_library_id", "workspace_id", "docsLibraryId", "workspaceId"):
        candidate = getattr(value, key, None)
        if candidate not in (None, ""):
            return candidate
    return default


def with_legacy_docs_library_aliases(
    payload: dict[str, Any],
    docs_library_id: Any,
    *,
    include_camel: bool = True,
) -> dict[str, Any]:
    """Add legacy aliases to a canonical payload for old mobile clients.

    The function mutates and returns ``payload`` to make serializer call sites
    concise.  New clients should consume ``docs_library_id``; aliases are
    intentionally marked in code rather than hidden in generic serializers.
    """

    payload["docs_library_id"] = str(docs_library_id) if docs_library_id is not None else None
    # Legacy mobile sync still reads workspace_id/workspaceId.  Keep both forms
    # during the rolling migration; this can be removed after all clients are
    # upgraded.
    payload["workspace_id"] = payload["docs_library_id"]
    if include_camel:
        payload["docsLibraryId"] = payload["docs_library_id"]
        payload["workspaceId"] = payload["docs_library_id"]
    return payload


__all__ = [
    "LEGACY_DOCS_LIBRARY_KEYS",
    "read_docs_library_id",
    "read_docs_library_id_from_object",
    "with_legacy_docs_library_aliases",
]
