"""Shared Docs protocol calls for direct tools and authenticated HTTP APIs.

Callers resolve the authenticated actor, root and allowed node scope before
entering this module.  Keeping that authorization boundary outside the common
service calls lets the direct tool retain its TurnContext semantics while the
HTTP API uses its independently verified Project scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from .docs_edit_read_session import (
    DocsEditReadSessionService,
    edit_read_scope_binding,
)
from .docs_mutation_service import DocsMutationService
from .docs_read_projection import build_docs_read_projection


def http_docs_mutation_binding(*, actor_id: UUID, verified_project_id: UUID | None) -> str:
    """Create a domain-separated binding for the external Docs HTTP surface."""

    from .docs_consistency import scope_binding

    base = scope_binding(
        actor_id=actor_id,
        project_id=verified_project_id,
        include_project_context=False,
        strict_scope=True,
    )
    return hashlib.sha256(b"aoitalk_plugin:v1\0" + base.encode("utf-8")).hexdigest()


async def read_docs_projection(
    *,
    graph: Any,
    root: Any,
    actor_id: UUID,
    allowed_node_ids: set[UUID] | None,
    turn_project_id: UUID | None,
    view: str,
    cursor: str,
    depth: int,
    page_chars: int,
    wire_view: bool = False,
) -> dict[str, Any]:
    """Read one bounded document, record or outline projection page."""

    if view not in {"document", "record", "outline"}:
        raise ValueError("view must be document, record or outline")
    result = await build_docs_read_projection(
        graph,
        root,
        actor_id,
        allowed_node_ids=allowed_node_ids,
        turn_project_id=turn_project_id,
        depth=depth,
        cursor=cursor,
        page_chars=page_chars,
        include_manifest=False,
        record_only=view == "record",
        outline_only=view == "outline",
    )
    if wire_view:
        result["view"] = view
    return result


async def read_docs_edit_page(
    *,
    session: Any,
    graph: Any,
    root: Any,
    actor_id: UUID,
    allowed_node_ids: set[UUID] | None,
    turn_project_id: UUID | None,
    cursor: str,
    depth: int,
    page_chars: int,
    mutation_binding: str,
) -> dict[str, Any]:
    """Read/replay one edit page, keeping session and write bindings distinct."""

    session_binding = edit_read_scope_binding(
        actor_id=actor_id,
        root_id=root.id,
        library_id=root.docs_library_id,
        depth=depth,
        page_chars=page_chars,
        turn_project_id=turn_project_id,
        allowed_node_ids=allowed_node_ids,
        context_binding=mutation_binding,
    )
    return await DocsEditReadSessionService(session).read_page(
        graph=graph,
        root=root,
        actor_id=actor_id,
        allowed_node_ids=allowed_node_ids,
        turn_project_id=turn_project_id,
        depth=depth,
        page_chars=page_chars,
        cursor=cursor,
        scope_binding=session_binding,
        mutation_binding=mutation_binding,
    )


async def apply_docs_changeset(
    *,
    session: Any,
    actor_id: UUID,
    root_id: UUID,
    write_token: UUID,
    operation_id: UUID,
    changeset: dict[str, Any],
    binding: str,
    before_commit: Callable[[], None],
) -> dict[str, Any]:
    """Apply a typed changeset without taking ownership of the transaction."""

    return await DocsMutationService(session).apply(
        actor_id=actor_id,
        root_id=root_id,
        write_token=write_token,
        operation_id=operation_id,
        changeset=changeset,
        binding=binding,
        before_commit=before_commit,
    )


__all__ = [
    "apply_docs_changeset",
    "http_docs_mutation_binding",
    "read_docs_edit_page",
    "read_docs_projection",
]
