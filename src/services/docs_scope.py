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

from sqlalchemy import String, cast, literal, select
from sqlalchemy.orm import aliased, load_only

from ..memory.models import DocsLibrary, KnowledgeNode, Project, ProjectKnowledgeRef
from ..memory.project_repository import ProjectRepository
from .docs_acl import can_read_node, docs_readable_node_predicate
from .docs_workspace import get_canonical_project_information_node


class DocsScopeMode(str, Enum):
    """Supported request-time Docs visibility modes."""

    CURRENT_PROJECT = "current_project"
    PROJECT_PLUS_PERSONAL = "project_plus_personal"
    PERSONAL_ONLY = "personal_only"
    ACCESSIBLE = "accessible"


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
    # Point reads use the authorized root and these membership anchors as the
    # stable scope identity.  The ordinary resolver leaves this empty and
    # continues to return the complete corpus identifier set.
    anchor_node_ids: tuple[UUID, ...] = ()
    # A Personal root in a project-plus-personal request still follows the
    # historical personal-node boundary: project-bound descendants are not
    # part of that lane unless a ProjectKnowledgeRef proves membership.
    personal_projection_only: bool = False
    # A Personal project-information hub may expose the requested Project's
    # canonical subtree as a compact point-read allow-set.  ``None`` means
    # that the projection can rely on root containment and ACL predicates.
    point_allowed_node_ids: tuple[UUID, ...] | None = None
    point_canonical_seed_ids: tuple[UUID, ...] = ()
    point_related_seed_ids: tuple[UUID, ...] = ()


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


async def _resolve_point_ancestors(
    session: Any,
    *,
    root_id: UUID,
    library_id: UUID,
) -> set[UUID]:
    """Return only the same-library ancestor chain for one requested root.

    The protocol read path needs to prove that a root is below a canonical or
    explicitly referenced anchor.  Walking the whole library would recreate the
    expensive corpus scan this helper is meant to avoid, so the recursive query
    follows parent pointers upward and carries a text path to terminate cycles.
    """

    path = literal("|") + cast(KnowledgeNode.id, String) + literal("|")
    ancestors = select(
        KnowledgeNode.id.label("id"),
        KnowledgeNode.parent_id.label("parent_id"),
        KnowledgeNode.docs_library_id.label("docs_library_id"),
        path.label("path"),
    ).where(
        KnowledgeNode.id == root_id,
        KnowledgeNode.docs_library_id == library_id,
    ).cte("docs_point_ancestors", recursive=True)
    parent = aliased(KnowledgeNode)
    parent_path = ancestors.c.path + cast(parent.id, String) + literal("|")
    ancestors = ancestors.union_all(
        select(
            parent.id,
            parent.parent_id,
            parent.docs_library_id,
            parent_path,
        )
        .join(ancestors, parent.id == ancestors.c.parent_id)
        .where(parent.docs_library_id == ancestors.c.docs_library_id)
        .where(
            ~ancestors.c.path.like(
                literal("%|") + cast(parent.id, String) + literal("|%")
            )
        )
    )
    rows = await _scalars(session, select(ancestors.c.id))
    return {
        node_id
        for node in rows
        if (node_id := _as_uuid(node)) is not None
    }


async def _resolve_personal_project_point_scope(
    *,
    session: Any,
    actor_user_id: UUID,
    project: Project,
    project_id: UUID,
    root: KnowledgeNode,
    library: DocsLibrary,
    library_id: UUID,
) -> DocsScope:
    """Resolve Project seeds below one Personal point-read root.

    PROJECT_PLUS_PERSONAL historically admits Personal rows and the direct
    Project canonical/reference closures in the same library.  Keep that union
    exact for one requested root by retaining only direct seeds whose ancestor
    chain contains the root.  No subtree or Personal corpus is materialized.
    """

    try:
        root_readable = await can_read_node(
            session,
            root,
            actor_user_id,
            library=library,
        )
    except Exception:
        root_readable = False
    if not root_readable:
        return _empty_scope(
            DocsScopeMode.PROJECT_PLUS_PERSONAL,
            reason="root_out_of_scope",
            project_id=project_id,
        )

    canonical, related, _library_ids = await _resolve_project_nodes(
        session=session,
        actor_user_id=actor_user_id,
        project=project,
        expand_subtrees=False,
    )

    root_ancestors = await _resolve_point_ancestors(
        session,
        root_id=root.id,
        library_id=library_id,
    )

    async def seed_below_root(node: KnowledgeNode) -> bool:
        if _as_uuid(getattr(node, "docs_library_id", None)) != library_id:
            return False
        ancestors = await _resolve_point_ancestors(
            session,
            root_id=node.id,
            library_id=library_id,
        )
        return root.id in ancestors

    canonical_seeds: list[UUID] = []
    canonical_anchors: list[UUID] = []
    for node in canonical:
        node_id = _as_uuid(getattr(node, "id", None))
        if node_id is None:
            continue
        if node_id in root_ancestors:
            # The projection starts at the requested Personal root, so retain
            # the original canonical seed as proof but normalize the effective
            # lane to that root for descendants below a hidden Personal node.
            canonical_anchors.append(node_id)
            canonical_seeds.append(root.id)
        elif await seed_below_root(node):
            canonical_anchors.append(node_id)
            canonical_seeds.append(node_id)
    related_seeds: list[UUID] = []
    related_anchors: list[UUID] = []
    for node in related:
        node_id = _as_uuid(getattr(node, "id", None))
        if node_id is None:
            continue
        if node_id in root_ancestors:
            related_anchors.append(node_id)
            related_seeds.append(root.id)
        elif await seed_below_root(node):
            related_anchors.append(node_id)
            related_seeds.append(node_id)

    anchors = tuple(
        dict.fromkeys(
            (root.id, *canonical_anchors, *related_anchors, *canonical_seeds, *related_seeds)
        )
    )
    return DocsScope(
        mode=DocsScopeMode.PROJECT_PLUS_PERSONAL,
        project_id=project_id,
        project_ids=(project_id,),
        allowed_library_ids=(library_id,),
        canonical_node_ids=tuple(dict.fromkeys(canonical_seeds)),
        related_node_ids=tuple(dict.fromkeys((root.id, *related_seeds))),
        personal_allowed=True,
        anchor_node_ids=anchors,
        personal_projection_only=True,
        point_canonical_seed_ids=tuple(dict.fromkeys(canonical_seeds)),
        point_related_seed_ids=tuple(dict.fromkeys(related_seeds)),
    )


async def _resolve_protocol_point_scope(
    *,
    session: Any,
    actor_user_id: UUID,
    project: Project | None,
    project_id: UUID | None,
    mode: DocsScopeMode,
    root_id: UUID,
) -> DocsScope:
    """Authorize one protocol root without resolving the surrounding corpus."""

    root = await session.get(KnowledgeNode, root_id)
    if root is None or getattr(root, "archived_at", None) is not None:
        return _empty_scope(mode, reason="root_not_found", project_id=project_id)
    library_id = _as_uuid(getattr(root, "docs_library_id", None))
    if library_id is None:
        return _empty_scope(mode, reason="root_not_found", project_id=project_id)
    library = await session.get(DocsLibrary, library_id)
    if library is None:
        return _empty_scope(mode, reason="root_not_found", project_id=project_id)

    if mode is DocsScopeMode.PERSONAL_ONLY:
        # PERSONAL_ONLY has historically excluded project-bound rows even when
        # they happen to live in a Personal Docs library.
        if getattr(root, "project_id", None) is not None:
            return _empty_scope(mode, reason="root_out_of_scope")
        if str(getattr(library, "library_type", "") or "").casefold() != "personal":
            return _empty_scope(mode, reason="root_out_of_scope")
        try:
            readable = await can_read_node(
                session,
                root,
                actor_user_id,
                library=library,
            )
        except Exception:
            readable = False
        if not readable:
            return _empty_scope(mode, reason="root_access_denied")
        return DocsScope(
            mode=mode,
            project_id=None,
            project_ids=(),
            allowed_library_ids=(library_id,),
            canonical_node_ids=(),
            related_node_ids=(root.id,),
            personal_allowed=True,
            anchor_node_ids=(root.id,),
            personal_projection_only=True,
        )

    if project is None or project_id is None:
        return _empty_scope(mode, reason="project_required")

    if (
        mode is DocsScopeMode.PROJECT_PLUS_PERSONAL
        and getattr(root, "project_id", None) is None
        and str(getattr(library, "library_type", "") or "").casefold()
        == "personal"
    ):
        return await _resolve_personal_project_point_scope(
            session=session,
            actor_user_id=actor_user_id,
            project=project,
            project_id=project_id,
            root=root,
            library=library,
            library_id=library_id,
        )

    # A root and its proof anchors must remain in one Docs library.  The
    # recursive CTE follows only same-library parent edges, so a malformed
    # cross-library parent cannot satisfy this membership test.
    ancestor_ids = await _resolve_point_ancestors(
        session,
        root_id=root.id,
        library_id=library_id,
    )
    if root.id not in ancestor_ids:
        return _empty_scope(mode, reason="root_not_found", project_id=project_id)

    anchors: list[UUID] = []
    canonical_root = await get_canonical_project_information_node(
        session,
        project_id=project.id,
        actor_user_id=actor_user_id,
    )
    canonical_id = _as_uuid(getattr(canonical_root, "id", None))
    if canonical_root is not None and canonical_id in ancestor_ids:
        # Canonical containment retains its strict project_id boundary.
        root_project_id = _as_uuid(getattr(root, "project_id", None))
        if root_project_id == project_id:
            try:
                readable = await can_read_node(
                    session,
                    canonical_root,
                    actor_user_id,
                )
            except Exception:
                readable = False
            if readable:
                anchors.append(canonical_id)

    # Fetch only references whose target is on this root's ancestor chain.  A
    # whole-project ref listing is still cheap compared with a Personal corpus
    # scan, but this predicate also keeps unrelated references out of the read
    # authorization and edit-session binding.
    ref_rows = await _scalars(
        session,
        select(ProjectKnowledgeRef).where(
            ProjectKnowledgeRef.project_id == project_id,
            ProjectKnowledgeRef.knowledge_node_id.in_(ancestor_ids),
        ),
    )
    for ref in ref_rows:
        relation = str(getattr(ref, "relation_type", "related") or "related").casefold()
        ref_node_id = _as_uuid(getattr(ref, "knowledge_node_id", None))
        if ref_node_id is None or ref_node_id not in ancestor_ids:
            continue
        # A relationship label cannot make a different node canonical.
        if relation == "canonical" and (
            canonical_id is None
            or ref_node_id != canonical_id
            or _as_uuid(getattr(root, "project_id", None)) != project_id
        ):
            continue
        node = await session.get(KnowledgeNode, ref_node_id)
        if (
            node is None
            or _as_uuid(getattr(node, "docs_library_id", None)) != library_id
            or getattr(node, "archived_at", None) is not None
        ):
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
        if readable:
            anchors.append(ref_node_id)

    try:
        root_readable = await can_read_node(
            session,
            root,
            actor_user_id,
            library=library,
        )
    except Exception:
        root_readable = False
    if not root_readable:
        return _empty_scope(mode, reason="root_out_of_scope", project_id=project_id)

    if not anchors:
        return _empty_scope(mode, reason="root_out_of_scope", project_id=project_id)

    unique_anchors = tuple(dict.fromkeys(anchors))
    return DocsScope(
        mode=mode,
        project_id=project_id,
        project_ids=(project_id,),
        allowed_library_ids=(library_id,),
        canonical_node_ids=(),
        related_node_ids=(root.id,),
        personal_allowed=mode is DocsScopeMode.PROJECT_PLUS_PERSONAL,
        anchor_node_ids=unique_anchors,
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
    expand_subtrees: bool = True,
) -> tuple[list[KnowledgeNode], list[KnowledgeNode], set[UUID]]:
    """Resolve canonical and explicitly referenced nodes for an authorized Project."""

    canonical: list[KnowledgeNode] = []
    related: list[KnowledgeNode] = []
    seen_canonical: set[UUID] = set()
    seen_related: set[UUID] = set()
    library_ids: set[UUID] = set()

    # ``Project.knowledge_node_id`` is only a denormalized pointer.  Resolve
    # it through the strict canonical contract before adding the node to a
    # request's visibility scope; a stale pointer must fail closed even when
    # the target happens to be ACL-readable.
    canonical_root = await get_canonical_project_information_node(
        session,
        project_id=project.id,
        actor_user_id=actor_user_id,
    )
    node = canonical_root
    if node is not None and await can_read_node(session, node, actor_user_id):
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
        if str(getattr(ref, "relation_type", "related")).casefold() == "canonical":
            # A relationship label is not canonical identity authority.
            if canonical_root is None or ref.knowledge_node_id != canonical_root.id:
                continue
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
    if not expand_subtrees:
        return canonical, related, library_ids
    canonical = await _expand_readable_subtrees(
        session, canonical, actor_user_id, required_project_id=project.id
    )
    canonical_ids = {node.id for node in canonical}
    related = [
        node for node in await _expand_readable_subtrees(session, related, actor_user_id)
        if node.id not in canonical_ids
    ]
    library_ids = {node.docs_library_id for node in [*canonical, *related]}
    return canonical, related, library_ids


async def _expand_readable_subtrees(
    session: Any,
    roots: list[KnowledgeNode],
    actor_user_id: UUID,
    *,
    required_project_id: UUID | None = None,
) -> list[KnowledgeNode]:
    """Expand containment, not references or library membership, then authorize.

    Traversal crosses hidden/archived intermediates: a descendant may have an
    independent grant. Only readable active rows are emitted. No implicit
    scope cap is allowed to turn a partial corpus into an apparently exact one.
    """
    visible: list[KnowledgeNode] = []
    emitted: set[UUID] = set()
    for library_id in dict.fromkeys(node.docs_library_id for node in roots):
        library = await session.get(DocsLibrary, library_id)
        frontier = [node.id for node in roots if node.docs_library_id == library_id]
        seen = set(frontier)
        for node in roots:
            if node.docs_library_id == library_id and node.id not in emitted:
                visible.append(node)
                emitted.add(node.id)
        while frontier:
            following: list[UUID] = []
            for offset in range(0, len(frontier), 500):
                parent_ids = set(frontier[offset:offset + 500])
                rows = await _scalars(
                    session,
                    select(KnowledgeNode).options(load_only(
                        KnowledgeNode.id, KnowledgeNode.docs_library_id,
                        KnowledgeNode.project_id, KnowledgeNode.parent_id,
                        KnowledgeNode.archived_at,
                    )).where(
                        KnowledgeNode.docs_library_id == library_id,
                        KnowledgeNode.parent_id.in_(parent_ids),
                    ).order_by(KnowledgeNode.sort_order, KnowledgeNode.id),
                )
                candidates = []
                for node in rows:
                    if (node.id in seen or node.docs_library_id != library_id
                            or getattr(node, "parent_id", None) not in parent_ids):
                        continue
                    seen.add(node.id)
                    following.append(node.id)
                    if required_project_id is not None and node.project_id != required_project_id:
                        continue
                    if node.archived_at is not None:
                        continue
                    candidates.append(node)
                if not candidates:
                    continue
                # Evaluate the shared SQL/point-ACL contract as a set, rather
                # than issuing one Project/share query per descendant.
                readable_ids = set(await _scalars(session, select(KnowledgeNode.id).where(
                    KnowledgeNode.id.in_([node.id for node in candidates]),
                    KnowledgeNode.docs_library_id == library_id,
                    KnowledgeNode.archived_at.is_(None),
                    docs_readable_node_predicate(
                        KnowledgeNode, docs_library_id=library_id, user_id=actor_user_id,
                        library_owner_id=getattr(library, "owner_user_id", None),
                    ),
                )))
                for node in candidates:
                    if node.id not in readable_ids:
                        continue
                    if node.id not in emitted:
                        emitted.add(node.id)
                        visible.append(node)
            frontier = following
    return visible


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
                    select(DocsLibrary.id).where(
                        DocsLibrary.id == KnowledgeNode.docs_library_id,
                        DocsLibrary.owner_user_id == actor_user_id,
                    ).exists(),
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

    nodes = []
    for library_id, library in library_by_id.items():
        nodes.extend(node for node in await _scalars(session, select(KnowledgeNode).where(
            KnowledgeNode.docs_library_id == library_id,
            KnowledgeNode.project_id.is_(None), KnowledgeNode.archived_at.is_(None),
            docs_readable_node_predicate(KnowledgeNode, docs_library_id=library_id,
                user_id=actor_user_id, library_owner_id=getattr(library, "owner_user_id", None)),
        )) if node.docs_library_id == library_id)
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
    expand_project_subtrees: bool = True,
    target_node_id: UUID | None = None,
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

    if target_node_id is not None and normalized_mode is DocsScopeMode.PERSONAL_ONLY:
        target = _as_uuid(target_node_id)
        if target is None:
            return _empty_scope(normalized_mode, reason="root_not_found")
        return await _resolve_protocol_point_scope(
            session=session,
            actor_user_id=actor,
            project=None,
            project_id=None,
            mode=normalized_mode,
            root_id=target,
        )

    if normalized_mode is DocsScopeMode.ACCESSIBLE:
        if project_id is not None:
            return _empty_scope(normalized_mode, reason="global_scope_requires_no_project")
        libraries = await _scalars(session, select(DocsLibrary).order_by(DocsLibrary.id))
        visible = []
        for library in libraries:
            visible.extend(await _scalars(session, select(KnowledgeNode).options(load_only(
                KnowledgeNode.id, KnowledgeNode.docs_library_id, KnowledgeNode.project_id,
            )).where(
                KnowledgeNode.docs_library_id == library.id, KnowledgeNode.archived_at.is_(None),
                docs_readable_node_predicate(KnowledgeNode, docs_library_id=library.id,
                    user_id=actor, library_owner_id=library.owner_user_id),
            ).order_by(KnowledgeNode.id)))
        return DocsScope(mode=normalized_mode, project_id=None,
            project_ids=tuple(sorted({node.project_id for node in visible if node.project_id}, key=str)),
            allowed_library_ids=tuple(sorted({node.docs_library_id for node in visible}, key=str)),
            canonical_node_ids=(), related_node_ids=tuple(node.id for node in visible), personal_allowed=True)

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

    if target_node_id is not None:
        target = _as_uuid(target_node_id)
        if target is None:
            return _empty_scope(
                normalized_mode,
                reason="root_not_found",
                project_id=project,
            )
        return await _resolve_protocol_point_scope(
            session=session,
            actor_user_id=actor,
            project=project_row,
            project_id=project,
            mode=normalized_mode,
            root_id=target,
        )

    canonical, related, project_library_ids = await _resolve_project_nodes(
        session=session,
        actor_user_id=actor,
        project=project_row,
        expand_subtrees=expand_project_subtrees,
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
