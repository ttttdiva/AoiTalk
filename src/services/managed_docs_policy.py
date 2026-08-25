"""Central mutation policy for system-managed Docs nodes.

The policy is deliberately based on stable metadata (``system_key`` and
``display_props.managed_domain``), never on localized titles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


LEGACY_AGENT_MEMORY_DOMAIN = "legacy_agent_memory"


@dataclass(frozen=True)
class ManagedDocsPolicy:
    managed_domain: str
    allowed_tools: frozenset[str]
    hidden_from_sidebar: bool = True
    allow_revival: bool = False
    source_refs_required: bool = True


_PREFIX_POLICIES: tuple[tuple[str, ManagedDocsPolicy], ...] = (
    (
        "agent_memory",
        ManagedDocsPolicy(
            managed_domain=LEGACY_AGENT_MEMORY_DOMAIN,
            allowed_tools=frozenset({"legacy_agent_memory_migration"}),
        ),
    ),
    (
        "project_inbox_item:",
        ManagedDocsPolicy(
            managed_domain="project_inbox",
            allowed_tools=frozenset({"inbox_update_item"}),
            hidden_from_sidebar=False,
            allow_revival=False,
        ),
    ),
    (
        "project_inbox:",
        ManagedDocsPolicy(
            managed_domain="project_inbox",
            allowed_tools=frozenset({"docs_ensure_inbox", "inbox_update_item"}),
            hidden_from_sidebar=False,
        ),
    ),
    (
        "project_mail",
        ManagedDocsPolicy(
            managed_domain="project_mail",
            allowed_tools=frozenset({"project_mail_sync"}),
        ),
    ),
    (
        "workspace_file_reference:",
        ManagedDocsPolicy(
            managed_domain="workspace_file_reference",
            allowed_tools=frozenset({"docs_attach_workspace_file"}),
            hidden_from_sidebar=False,
        ),
    ),
)


def policy_for_node(node: Any) -> ManagedDocsPolicy | None:
    # Stable system keys are authoritative. Display metadata may add policy to
    # other nodes, but it must never loosen a known managed domain.
    system_key = str(getattr(node, "system_key", None) or "")
    for prefix, policy in _PREFIX_POLICIES:
        if system_key == prefix or system_key.startswith(prefix):
            return policy

    props = node.display_props if isinstance(getattr(node, "display_props", None), dict) else {}
    domain = str(props.get("managed_domain") or "").strip()
    if props.get("system_managed") is True and domain:
        allowed = props.get("managed_allowed_tools")
        return ManagedDocsPolicy(
            managed_domain=domain,
            allowed_tools=frozenset(
                str(item) for item in allowed if str(item).strip()
            )
            if isinstance(allowed, list)
            else frozenset(),
            hidden_from_sidebar=props.get("hidden_from_sidebar") is True,
            allow_revival=props.get("managed_allow_revival") is True,
            source_refs_required=props.get("managed_source_refs_required") is not False,
        )

    return None


def managed_display_props(
    policy: ManagedDocsPolicy,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(existing or {}),
        "system_managed": True,
        "managed_domain": policy.managed_domain,
        "managed_allowed_tools": sorted(policy.allowed_tools),
        "managed_allow_revival": policy.allow_revival,
        "managed_source_refs_required": policy.source_refs_required,
        "hidden_from_sidebar": policy.hidden_from_sidebar,
    }


def assert_managed_docs_mutation_allowed(
    node: Any,
    *,
    tool_name: str,
    source_refs: Iterable[dict[str, Any]] | None = None,
    revival: bool = False,
) -> None:
    policy = policy_for_node(node)
    if policy is None:
        return
    if tool_name not in policy.allowed_tools:
        raise PermissionError(
            f"{policy.managed_domain} is system-managed and cannot be changed by {tool_name}"
        )
    if revival and not policy.allow_revival:
        raise PermissionError(f"{policy.managed_domain} archived nodes cannot be revived")
    if policy.source_refs_required and not list(source_refs or []):
        raise PermissionError(f"{policy.managed_domain} mutations require source_refs")


async def assert_managed_docs_tree_mutation_allowed(
    session: Any,
    node: Any,
    *,
    tool_name: str,
    source_refs: Iterable[dict[str, Any]] | None = None,
    revival: bool = False,
) -> None:
    """Apply the managed policy to a node and its stable parent chain."""
    from ..memory.models import KnowledgeNode

    current = node
    visited: set[Any] = set()
    docs_library_id = getattr(node, "docs_library_id", None)
    while current is not None and getattr(current, "id", None) not in visited:
        visited.add(getattr(current, "id", None))
        assert_managed_docs_mutation_allowed(
            current,
            tool_name=tool_name,
            source_refs=source_refs,
            revival=revival,
        )
        parent_id = getattr(current, "parent_id", None)
        if parent_id is None:
            break
        current = await session.get(KnowledgeNode, parent_id)
        if current is None:
            raise PermissionError("managed Docs ancestor could not be resolved")
        if current is not None and getattr(current, "docs_library_id", None) != docs_library_id:
            raise PermissionError("managed Docs ancestor escaped its library")


__all__ = [
    "LEGACY_AGENT_MEMORY_DOMAIN",
    "ManagedDocsPolicy",
    "assert_managed_docs_mutation_allowed",
    "assert_managed_docs_tree_mutation_allowed",
    "managed_display_props",
    "policy_for_node",
]
