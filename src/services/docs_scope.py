"""Resolve the Docs visibility boundary for one request.

This module deliberately returns identifiers only.  It is the single place where
the actor, Project ACL, KnowledgeNode ACL, and the requested scope mode are
combined before a Docs search/read path is allowed to inspect content.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select

from ..memory.models import DocsLibrary, KnowledgeNode, Project, ProjectKnowledgeRef
from ..memory.project_repository import ProjectRepository
from .docs_acl import can_read_node


class DocsScopeMode(str, Enum):
    """Supported request-time Docs visibility modes."""

    CURRENT_PROJECT = "current_project"
    PROJECT_PLUS_PERSONAL = "project_plus_personal"
    PERSONAL_ONLY = "personal_only"


@dataclass(frozen=True)
class DocsScope:
    """Compact, immutable result used by downstream Docs/retrieval paths."""

    mode: DocsScopeMode
    project_id: UUID | None
    project_ids: tuple[UUID, ...]
    allowed_library_ids: tuple[UUID, ...]
    canonical_node_ids: tuple[UUID, ...]
    related_node_ids: tuple[UUID, ...]
    personal_allowed: bool
    reason: str | None = None


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _empty_scope(
    mode: DocsScopeMode,
    *,
    reason: str,
    project_id: UUID | None = None,
) -> DocsScope:
    return DocsScope(
        mode=mode,
        project_id=project_id,
        project_ids=(),
        allowed_library_ids=(),
        canonical_node_ids=(),
        related_node_ids=(),
        personal_allowed=False,
        reason=reason,
    )


async def _scalars(session: Any, statement: Any) -> list[Any]:
    """Read ORM scalar rows while remaining friendly to small test fakes."""

    result = await session.execute(statement)
    scalars = result.scalars() if hasattr(result, "scalars") else result
    if hasattr(scalars, "all"):
        return list(scalars.all())
    if isinstance(scalars, Iterable):
        return list(scalars)
    return []


def _append_unique(values: list[UUID], seen: set[UUID], value: Any) -> UUID | None:
    normalized = _as_uuid(value)
    if normalized is None or normalized in seen:
        return normalized
    seen.add(normalized)
    values.append(normalized)
    return normalized


async def _resolve_project_nodes(
    *,
    session: Any,
    actor_user_id: UUID,
    project: Project,
) -> tuple[list[KnowledgeNode], list[KnowledgeNode], set[UUID]]:
    """Resolve canonical and explicitly referenced nodes for an authorized Project."""

    canonical: list[KnowledgeNode] = []
    related: list[KnowledgeNode] = []
    seen_canonical: set[UUID] = set()
    seen_related: set[UUID] = set()
    library_ids: set[UUID] = set()

    if project.knowledge_node_id:
        node = await session.get(KnowledgeNode, project.knowledge_node_id)
        if node is not None and await can_read_node(
            session, node, actor_user_id
        ):
            node_id = _as_uuid(getattr(node, "id", None))
            if node_id is not None:
                canonical.append(node)
                seen_canonical.add(node_id)
                library_id = _as_uuid(getattr(node, "docs_library_id", None))
                if library_id is not None:
                    library_ids.add(library_id)

    # The service contract has no soft-delete column on ProjectKnowledgeRef;
    # a stale/archived target is therefore omitted at resolution time.
    refs = await _scalars(
        session,
        select(ProjectKnowledgeRef)
        .where(ProjectKnowledgeRef.project_id == project.id)
        .order_by(
            ProjectKnowledgeRef.priority,
            ProjectKnowledgeRef.updated_at,
            ProjectKnowledgeRef.id,
        ),
    )
    for ref in refs:
        node = await session.get(KnowledgeNode, ref.knowledge_node_id)
        if node is None or getattr(node, "archived_at", None) is not None:
            continue
        if not await can_read_node(session, node, actor_user_id):
            continue
        node_id = _as_uuid(getattr(node, "id", None))
        library_id = _as_uuid(getattr(node, "docs_library_id", None))
        if node_id is None:
            continue
        if library_id is not None:
            library_ids.add(library_id)
        if str(getattr(ref, "relation_type", "related")).casefold() == "canonical":
            if node_id not in seen_canonical:
                canonical.append(node)
                seen_canonical.add(node_id)
            continue
        if node_id not in seen_related and node_id not in seen_canonical:
            related.append(node)
            seen_related.add(node_id)
    return canonical, related, library_ids


async def _resolve_personal_nodes(
    *,
    session: Any,
    actor_user_id: UUID,
    max_nodes: int | None = None,
) -> tuple[list[KnowledgeNode], set[UUID]]:
    """Return visible non-project nodes from owner/shared Personal libraries."""

    libraries = await _scalars(
        session,
        select(DocsLibrary).where(DocsLibrary.library_type == "personal"),
    )
    owner_library_ids = {
        library_id
        for library in libraries
        if (library_id := _as_uuid(getattr(library, "id", None))) is not None
        and _as_uuid(getattr(library, "owner_user_id", None)) == actor_user_id
    }
    library_by_id = {
        library_id: library
        for library in libraries
        if (library_id := _as_uuid(getattr(library, "id", None))) is not None
    }

    # Normal Docs scope resolution intentionally keeps the historical complete
    # ACL scan.  The bounded branch is opt-in and is used only by compact
    # context/index callers that do not need an exhaustive Personal Docs scope.
    if max_nodes is not None:
        try:
            limit = max(0, min(int(max_nodes), 48))
        except (TypeError, ValueError):
            limit = 24

        visible_library_ids: set[UUID] = set(owner_library_ids)
        if limit <= 0:
            return [], visible_library_ids

        def eligible_node(
            node: KnowledgeNode,
            *,
            allowed_library_ids: set[UUID],
        ) -> tuple[bool, UUID | None]:
            library_id = _as_uuid(getattr(node, "docs_library_id", None))
            if library_id is None or library_id not in allowed_library_ids:
                return False, library_id
            if getattr(node, "project_id", None) is not None:
                return False, library_id
            if getattr(node, "archived_at", None) is not None:
                return False, library_id
            return True, library_id

        visible: list[KnowledgeNode] = []

        # Nodes in the actor's own Personal library are directly readable by
        # the library owner.  Push that ownership predicate and LIMIT into SQL
        # rather than selecting every project_id IS NULL node and running an
        # ACL coroutine for each one.
        if owner_library_ids:
            owner_nodes = await _scalars(
                session,
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.project_id.is_(None),
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.docs_library_id.in_(
                        tuple(owner_library_ids)
                    ),
                )
                .order_by(KnowledgeNode.id)
                .limit(limit),
            )
            # Test doubles may ignore SQL LIMIT/predicates, so enforce the
            # same boundary in Python before any further work.
            for node in owner_nodes:
                eligible, _library_id = eligible_node(
                    node,
                    allowed_library_ids=owner_library_ids,
                )
                if not eligible:
                    continue
                visible.append(node)
                if len(visible) >= limit:
                    return visible, visible_library_ids

        # Shared Personal libraries still require the full node ACL check, but
        # only for a bounded candidate set.  An ACL lookup failure is
        # fail-closed for that candidate and must not expose the node.
        shared_library_ids = set(library_by_id) - owner_library_ids
        if shared_library_ids and len(visible) < limit:
            shared_nodes = await _scalars(
                session,
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.project_id.is_(None),
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.docs_library_id.in_(
                        tuple(shared_library_ids)
                    ),
                )
                .order_by(KnowledgeNode.id)
                .limit(limit),
            )
            checked_candidates = 0
            for node in shared_nodes:
                eligible, node_library_id = eligible_node(
                    node,
                    allowed_library_ids=shared_library_ids,
                )
                if not eligible or node_library_id is None:
                    continue
                # Protect the bounded contract even when a lightweight fake
                # session ignores the SQL LIMIT.
                if checked_candidates >= limit:
                    break
                checked_candidates += 1
                library = library_by_id.get(node_library_id)
                if library is None:
                    continue
                try:
                    readable = await can_read_node(
                        session,
                        node,
                        actor_user_id,
                        library=library,
                    )
                except Exception:
                    readable = False
                if not readable:
                    continue
                visible.append(node)
                visible_library_ids.add(node_library_id)
                if len(visible) >= limit:
                    break

        return visible, visible_library_ids

    # Exhaustive path used by ordinary Docs scope/search callers.  Do not add
    # a LIMIT here: the public Docs ACL semantics remain unchanged.

    nodes = await _scalars(
        session,
        select(KnowledgeNode).where(
            KnowledgeNode.project_id.is_(None),
            KnowledgeNode.archived_at.is_(None),
        ),
    )
    visible: list[KnowledgeNode] = []
    visible_library_ids: set[UUID] = set(owner_library_ids)
    for node in nodes:
        node_library_id = _as_uuid(getattr(node, "docs_library_id", None))
        if node_library_id is None or node_library_id not in library_by_id:
            continue
        library = library_by_id[node_library_id]
        if await can_read_node(
            session,
            node,
            actor_user_id,
            library=library,
        ):
            visible.append(node)
            visible_library_ids.add(node_library_id)
    return visible, visible_library_ids


async def resolve_docs_scope(
    *,
    session: Any,
    actor_user_id: UUID,
    project_id: UUID | None,
    mode: DocsScopeMode,
    max_personal_nodes: int | None = None,
) -> DocsScope:
    """Resolve the actor's compact Docs scope for one request.

    Unknown projects, invalid actors, and denied Project ACLs return an empty
    scope.  Returning a non-throwing empty result prevents callers from turning
    an authorization miss into a resource-existence oracle.
    """

    try:
        normalized_mode = mode if isinstance(mode, DocsScopeMode) else DocsScopeMode(mode)
    except (TypeError, ValueError):
        # Preserve a stable enum value in the result even for an invalid input.
        return _empty_scope(DocsScopeMode.CURRENT_PROJECT, reason="invalid_mode")

    actor = _as_uuid(actor_user_id)
    if actor is None:
        return _empty_scope(normalized_mode, reason="invalid_actor")

    if normalized_mode is DocsScopeMode.PERSONAL_ONLY:
        personal_nodes, personal_libraries = await _resolve_personal_nodes(
            session=session,
            actor_user_id=actor,
            max_nodes=max_personal_nodes,
        )
        personal_ids = tuple(
            node_id
            for node in personal_nodes
            if (node_id := _as_uuid(getattr(node, "id", None))) is not None
        )
        return DocsScope(
            mode=normalized_mode,
            project_id=None,
            project_ids=(),
            allowed_library_ids=tuple(sorted(personal_libraries, key=str)),
            canonical_node_ids=(),
            related_node_ids=personal_ids,
            personal_allowed=True,
        )

    project = _as_uuid(project_id)
    if project is None:
        return _empty_scope(normalized_mode, reason="project_required")
    try:
        project_row = await session.get(Project, project)
    except Exception:
        project_row = None
    if project_row is None or getattr(project_row, "deleted_at", None) is not None:
        return _empty_scope(normalized_mode, reason="project_not_found")
    try:
        allowed = await ProjectRepository.has_permission(
            session,
            project_id=project,
            user_id=actor,
            permission="read",
        )
    except Exception:
        allowed = False
    if not allowed:
        return _empty_scope(normalized_mode, reason="project_access_denied")

    canonical, related, project_library_ids = await _resolve_project_nodes(
        session=session,
        actor_user_id=actor,
        project=project_row,
    )
    personal_allowed = normalized_mode is DocsScopeMode.PROJECT_PLUS_PERSONAL
    personal_library_ids: set[UUID] = set()
    personal_nodes: list[KnowledgeNode] = []
    if personal_allowed:
        personal_nodes, personal_library_ids = await _resolve_personal_nodes(
            session=session,
            actor_user_id=actor,
            max_nodes=max_personal_nodes,
        )

    canonical_ids = tuple(
        node_id
        for node in canonical
        if (node_id := _as_uuid(getattr(node, "id", None))) is not None
    )
    related_ids = [
        node_id
        for node in related
        if (node_id := _as_uuid(getattr(node, "id", None))) is not None
    ]
    if personal_allowed:
        related_ids.extend(
            node_id
            for node in personal_nodes
            if (node_id := _as_uuid(getattr(node, "id", None))) is not None
            and node_id not in canonical_ids
            and node_id not in related_ids
        )
    project_ids: set[UUID] = {project}
    for node in [*canonical, *related]:
        node_project_id = _as_uuid(getattr(node, "project_id", None))
        if node_project_id is not None:
            project_ids.add(node_project_id)
    return DocsScope(
        mode=normalized_mode,
        project_id=project,
        project_ids=tuple(sorted(project_ids, key=str)),
        allowed_library_ids=tuple(
            sorted(project_library_ids | personal_library_ids, key=str)
        ),
        canonical_node_ids=canonical_ids,
        related_node_ids=tuple(related_ids),
        personal_allowed=personal_allowed,
    )


__all__ = ["DocsScope", "DocsScopeMode", "resolve_docs_scope"]
