"""Revision-pinned policy helpers for ordinary Agent-to-Agent delegation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .agent_revision_v2 import (
    revision_delegate_policy as _revision_delegate_policy,
    revision_model_overrides as _revision_model_overrides,
    revision_tool_ids as _revision_tool_ids,
)


def revision_has_new_delegate_policy(revision: Any) -> bool:
    """Return whether the immutable revision has both P2 policy projections."""

    return (
        _revision_tool_ids(revision) is not None
        and _revision_delegate_policy(revision) is not None
    )


def intersect_revision_tool_ids(
    caller_revision: Any,
    worker_revision: Any,
) -> list[str] | None:
    """Return the caller/worker tool-ID intersection for migrated revisions.

    ``None`` preserves the legacy capability-only surface when either pinned
    revision predates the additive P2 fields.
    """

    if not (
        revision_has_new_delegate_policy(caller_revision)
        and revision_has_new_delegate_policy(worker_revision)
    ):
        return None
    caller_tools = _revision_tool_ids(caller_revision) or []
    worker_tools = _revision_tool_ids(worker_revision) or []
    caller_allowed = {
        item.strip().casefold()
        for item in caller_tools
        if isinstance(item, str) and item.strip()
    }
    seen: set[str] = set()
    result: list[str] = []
    for item in worker_tools:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = item.strip().casefold()
        if normalized in caller_allowed and normalized not in seen:
            result.append(item.strip())
            seen.add(normalized)
    return result


def revision_allows_agent_delegate(
    caller_revision: Any,
    *,
    tool_name: str,
    target_agent_id: Any,
    target_agent_owner_id: Any,
    caller_agent_owner_id: Any,
    owner_user_id: Any,
) -> bool:
    """Check the exact invoked tool ID and the caller revision's target policy."""

    normalized_tool = str(tool_name or "").strip().casefold()
    if normalized_tool not in {"delegate", "delegate_agent_work", "delegate_plan", "delegate_media"}:
        return False

    tool_ids = _revision_tool_ids(caller_revision)
    policy = _revision_delegate_policy(caller_revision)
    has_any_p2_projection = (
        tool_ids is not None
        or policy is not None
        or _revision_model_overrides(caller_revision) is not None
    )
    if not has_any_p2_projection:
        # Historical all-NULL revisions keep their pre-P2 capability-only
        # behavior, including the existing alias.
        return normalized_tool not in {"delegate_plan", "delegate_media"}
    if tool_ids is None or policy is None:
        # A partially projected revision cannot establish exact tool or target
        # authority, so it must not fall back to the historical behavior.
        return False
    pinned_tool_ids = {
        item.strip().casefold()
        for item in tool_ids
        if isinstance(item, str) and item.strip()
    }
    if normalized_tool == "delegate":
        # The canonical ID is not borrowed from the alias alone.
        if "delegate" not in pinned_tool_ids:
            return False
    elif normalized_tool == "delegate_agent_work":
        # Adopted synonymous-alias compatibility: a pinned canonical
        # ``delegate`` also permits the alias, and a pinned alias permits
        # only itself.
        if not ({"delegate", "delegate_agent_work"} & pinned_tool_ids):
            return False
    elif normalized_tool in {"delegate_plan", "delegate_media"}:
        if normalized_tool not in pinned_tool_ids:
            return False
    else:
        return False

    agents = policy.get("agents")
    if agents == "any_owned":
        owner = str(owner_user_id or "").strip()
        return bool(owner) and (
            str(caller_agent_owner_id or "").strip() == owner
            and str(target_agent_owner_id or "").strip() == owner
        )
    if not isinstance(agents, list):
        return False
    target_id = str(target_agent_id or "").strip().casefold()
    return bool(target_id) and target_id in {
        str(item).strip().casefold()
        for item in agents
        if isinstance(item, str) and item.strip()
    }


def worker_delegate_required_capabilities(
    kind: Any,
    workspace_mode: Any,
) -> frozenset[str]:
    """Return the shared capability ceiling for one external worker scope."""

    worker_kind = str(kind or "").strip().casefold()
    mode = str(workspace_mode or "").strip().casefold()
    capability_by_kind = {
        "codex": "command_execute",
        "antigravity": "command_execute",
        "claude": "command_execute",
        "grok": "command_execute",
        "muse": "remote_agent_delegate",
    }
    base = capability_by_kind.get(worker_kind)
    if base is None:
        return frozenset()
    if mode == "read":
        return frozenset({base, "workspace_read"})
    if mode == "write":
        return frozenset({base, "workspace_write", "command_execute"})
    if not mode:
        return frozenset({base})
    return frozenset()


def revision_allows_worker_delegate(caller_revision: Any, kind: str) -> bool:
    """Check the optional P2 worker tool id and worker kind allowlist."""

    worker_kind = str(kind or "").strip().casefold()
    if worker_kind not in {
        "codex",
        "antigravity",
        "claude",
        "grok",
        "muse",
    }:
        return False
    tool_ids = _revision_tool_ids(caller_revision)
    policy = _revision_delegate_policy(caller_revision)
    has_any_p2_projection = (
        tool_ids is not None
        or policy is not None
        or _revision_model_overrides(caller_revision) is not None
    )
    if not has_any_p2_projection:
        return True
    if tool_ids is None or policy is None:
        return False
    tool_id = f"delegate_worker:{worker_kind}"
    if tool_id not in {
        item.strip().casefold()
        for item in tool_ids
        if isinstance(item, str) and item.strip()
    }:
        return False
    workers = policy.get("workers")
    return isinstance(workers, list) and worker_kind in {
        str(item).strip().casefold()
        for item in workers
        if isinstance(item, str) and item.strip()
    }


def dialogue_worker_tool_ids(
    dialogue: Any,
    caller_revision: Any,
    worker_revision: Any,
) -> tuple[str, ...] | None:
    """Read a hash-bound worker tool intersection from a Dialogue.

    ``None`` preserves historical capability-only execution. An invalid or
    incomplete modern projection returns an explicit empty tuple so callers
    cannot fall back to the worker's wider standalone revision.
    """

    authorization = getattr(dialogue, "authorization_json", None)
    authorization = authorization if isinstance(authorization, Mapping) else {}
    tool_policy = authorization.get("tool_policy")
    raw_tool_ids = (
        tool_policy.get("agent_dialogue_tool_ids")
        if isinstance(tool_policy, Mapping)
        else None
    )
    caller_is_new = revision_has_new_delegate_policy(caller_revision)
    expected_tool_ids = intersect_revision_tool_ids(
        caller_revision,
        worker_revision,
    )
    expected_hash = hashlib.sha256(
        json.dumps(
            authorization,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    projection_valid = (
        isinstance(tool_policy, Mapping)
        and expected_hash == str(getattr(dialogue, "authorization_hash", "") or "")
        and str(tool_policy.get("source_agent_revision_id") or "")
        == str(getattr(dialogue, "root_agent_revision_id", "") or "")
        and str(tool_policy.get("worker_agent_revision_id") or "")
        == str(getattr(dialogue, "worker_agent_revision_id", "") or "")
        and (
            not caller_is_new
            or (expected_tool_ids is not None and isinstance(raw_tool_ids, list))
        )
        and (
            raw_tool_ids is None
            or (
                isinstance(raw_tool_ids, list)
                and all(isinstance(item, str) and item.strip() for item in raw_tool_ids)
                and raw_tool_ids == expected_tool_ids
            )
        )
    )
    if projection_valid and isinstance(raw_tool_ids, list):
        return tuple(raw_tool_ids)
    if caller_is_new or isinstance(tool_policy, Mapping):
        return ()
    return None


__all__ = [
    "dialogue_worker_tool_ids",
    "intersect_revision_tool_ids",
    "revision_allows_agent_delegate",
    "revision_allows_worker_delegate",
    "revision_has_new_delegate_policy",
    "worker_delegate_required_capabilities",
]
