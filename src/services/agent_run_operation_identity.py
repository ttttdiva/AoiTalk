"""Canonical correlation identity for AgentRun lifecycle events.

The producer and read-only projections must agree on one operation identity.
This module intentionally contains only closed key precedence and string
normalisation; it never returns event payloads or tool arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EVENT_OPERATION_KEYS = (
    "operation_id",
    "tool_call_id",
    "call_id",
    "agent_instance_key",
    "actor_instance_key",
)
EVENT_OPERATION_CONTAINERS = ("tool_result", "tool_call", "call")


def canonical_event_operation_key(payload: Mapping[str, Any] | Any) -> str:
    """Return the same bounded operation identity used by AgentRun producers."""

    if not isinstance(payload, Mapping):
        return ""
    sources: list[tuple[Mapping[str, Any], bool]] = [(payload, False)]
    for container in EVENT_OPERATION_CONTAINERS:
        value = payload.get(container)
        if isinstance(value, Mapping):
            sources.append((value, container in {"tool_call", "call"}))
    for source, include_generic_id in sources:
        keys = EVENT_OPERATION_KEYS + (("id",) if include_generic_id else ())
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""
