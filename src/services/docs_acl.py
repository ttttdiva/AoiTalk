"""Authorization helpers for the shared Docs graph.

Docs live in a Personal Docs Library.  A personal library is private by
default; a ``KnowledgeNodeShare`` row grants a user access to that node and
its entire descendant subtree.  Project membership is derived exclusively
from ``KnowledgeNode.project_id``; the retired project-library discriminator
is no longer consulted by this ACL boundary.

Imports of the share model are kept local to a few helpers so old deployments
can still import the application before the migration has been applied; such
deployments simply have no explicit user shares.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, false, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnElement

from ..memory.models import (
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    DocsLibrary,
    Project,
)
from ..memory.project_repository import ProjectRepository


# Keep expanded ``IN`` predicates well below PostgreSQL's bind-parameter
# ceiling.  A workspace listing still uses one query for ordinary scopes; only
# unusually large graphs are split into bounded chunks.
_ACL_IN_CHUNK_SIZE = 1000

# Python str.strip() also removes tabs/newlines and Unicode whitespace.
_SQL_STRIP_CHARACTERS = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _sql_stripped(value: ColumnElement) -> ColumnElement:
    return func.btrim(value, _SQL_STRIP_CHARACTERS)


def _acl_json_text(value: ColumnElement) -> ColumnElement:
    # Python accepts escaped NUL/lone surrogates, whereas JSONB rejects them
    # even in values discarded by last-wins duplicate-key decoding. Replace
    # these codeunits with harmless escapes: values remain strings (never
    # grants), and affected keys cannot become allow-listed keys.
    # Match only active escapes after an even number of literal backslashes.
    # This also preserves the outer encoding of legacy JSON strings until their
    # second decoding pass. NUL stays a control character, so a literal NUL in
    # legacy inner JSON remains invalid. Surrogates become a noncontrol sentinel,
    # since Python accepts literal surrogates inside legacy inner JSON strings.
    value = func.regexp_replace(
        value,
        r"(?<!\\)((?:\\\\)*)\\u0000",
        r"\1\\u0001",
        "g",
    )
    value = func.regexp_replace(
        value,
        r"(?<!\\)((?:\\\\)*)\\u[dD][89a-fA-F][0-9a-fA-F]{2}",
        r"\1\\ufffd",
        "g",
    )
    # Numeric values can never grant a boolean permission. Retain their type
    # while avoiding JSONB numeric overflow *before* duplicate keys are resolved.
    # Python's JSON decoder also accepts NaN/Infinity; these remain nonboolean.
    # Preserve whitespace/delimiters so invalid JSON is not repaired into a grant.
    return func.regexp_replace(
        value,
        r"([\[:,][ \t\r\n]*)(?:-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|NaN|-?Infinity)([ \t\r\n]*)(?=[,\]}])",
        r"\1 0\2",
        "g",
    )


def _project_permission_predicate(
    project_column: ColumnElement,
    *,
    user_id: UUID,
    required: str = "read",
) -> ColumnElement:
    """Return a SQL-native Project ACL predicate for a node expression.

    ``ProjectRepository.has_permission`` is the point-read authority and
    applies the same policy: a live project is visible to its owner, a global
    administrator, or a membership carrying an explicit boolean permission.
    Keeping this as a correlated ``EXISTS`` avoids fetching project IDs into
    Python for every Docs listing.
    """

    try:
        from ..memory.models import ProjectMember, User
        from .project_permissions import PROJECT_PERMISSION_KEYS

        if required not in PROJECT_PERMISSION_KEYS:
            return false()
        # Deployment/CI target PostgreSQL 16. CASE, not AND evaluation order,
        # protects casts of legacy JSON strings and non-JSONB-compatible data.
        # JSONB resolves duplicate keys as the Python JSON decoder does.
        empty_object = cast(literal("{}"), JSONB)
        raw_text = _acl_json_text(cast(ProjectMember.permissions, String))
        raw = case(
            (
                func.pg_input_is_valid(raw_text, "jsonb"),
                cast(raw_text, JSONB),
            ),
            else_=empty_object,
        )
        decoded_text = _acl_json_text(raw.op("#>>")(cast(literal("{}"), ARRAY(String))))
        normalized = case(
            (
                func.jsonb_typeof(raw) == "string",
                case(
                    (func.pg_input_is_valid(decoded_text, "jsonb"), cast(decoded_text, JSONB)),
                    else_=empty_object,
                ),
            ),
            else_=raw,
        )
        permission_object = case(
            (func.jsonb_typeof(normalized) == "object", normalized),
            else_=empty_object,
        )
        entries = func.jsonb_each(permission_object).table_valued("key", "value")
        invalid_entry = select(literal(1)).select_from(entries).where(
            or_(
                entries.c.key.not_in(sorted(PROJECT_PERMISSION_KEYS)),
                func.jsonb_typeof(entries.c.value) != "boolean",
            ),
        ).correlate(ProjectMember).exists()
        membership_grant = and_(
            ~invalid_entry,
            permission_object.op("->")(required) == cast(literal("true"), JSONB),
        )
        membership = ProjectMember.user_id == user_id
        return select(literal(1)).select_from(Project).join(
            User, User.id == user_id
        ).outerjoin(
            ProjectMember,
            and_(
                ProjectMember.project_id == Project.id,
                membership,
            ),
        ).where(
            Project.id == project_column,
            Project.deleted_at.is_(None),
            or_(
                Project.owner_id == user_id,
                func.lower(User.role) == "admin",
                membership_grant,
            ),
        ).exists()
    except Exception:
        # Keep imports/compilation working during rolling deploys before the
        # ProjectMember model is available.  Deny project-bound candidates.
        return false()


def _shared_nodes_cte(
    *,
    docs_library_id: UUID,
    user_id: UUID,
    required: str = "read",
    name: str = "docs_shared_nodes",
):
    """Build a recursive SQL relation of shared descendants.

    The CTE starts only at explicit share roots for ``user_id`` and follows
    parent links within the same Docs Library.  It never materializes the
    library's visible IDs in application memory, and a ``UNION`` (rather than
    ``UNION ALL``) makes malformed parent cycles finite.
    """

    try:
        from ..memory.models import KnowledgeNodeShare
    except ImportError:
        return None

    allowed_permissions = ("write",) if required == "write" else ("read", "write")
    roots = (
        select(
            KnowledgeNode.id.label("node_id"),
            KnowledgeNode.docs_library_id.label("docs_library_id"),
        )
        .select_from(KnowledgeNode)
        .join(
            KnowledgeNodeShare,
            KnowledgeNodeShare.node_id == KnowledgeNode.id,
        )
        .where(
            KnowledgeNode.docs_library_id == docs_library_id,
            KnowledgeNodeShare.user_id == user_id,
            KnowledgeNodeShare.permission.in_(allowed_permissions),
        )
    )
    shared = roots.cte(name, recursive=True)
    descendants = (
        select(
            KnowledgeNode.id.label("node_id"),
            KnowledgeNode.docs_library_id.label("docs_library_id"),
        )
        .select_from(KnowledgeNode)
        .join(
            shared,
            and_(
                KnowledgeNode.parent_id == shared.c.node_id,
                KnowledgeNode.docs_library_id == shared.c.docs_library_id,
            ),
        )
        .where(KnowledgeNode.docs_library_id == docs_library_id)
    )
    if required == "write":
        # A child-level read share is an explicit downgrade and must stop a
        # writable ancestor from flowing through.  Writable child roots are
        # emitted by ``roots`` and continue their own subtree recursion.
        descendants = descendants.where(
            ~select(literal(1))
            .select_from(KnowledgeNodeShare)
            .where(
                KnowledgeNodeShare.node_id == KnowledgeNode.id,
                KnowledgeNodeShare.user_id == user_id,
            )
            .exists()
        )
    return shared.union(descendants)


def docs_readable_node_predicate(
    node_model: Any = KnowledgeNode,
    *,
    docs_library_id: UUID | None,
    user_id: UUID | str | None,
    library_owner_id: UUID | str | None = None,
    required: str = "read",
    shared_nodes: Any | None = None,
) -> ColumnElement:
    """Return a SQLAlchemy visibility predicate for a Docs node expression.

    This is the SQL-native replacement for the old ``accessible_node_ids``
    listing contract.  The common Personal-owner and Project-membership paths
    are simple correlated predicates.  Only foreign Personal nodes consult a
    recursive share-descendant CTE.  Callers must compose this expression into
    their candidate query and apply ``LIMIT`` *after* authorization.

    ``shared_nodes`` may be supplied when a statement needs to authorize more
    than one node expression (for example query field reference targets), so a
    single recursive CTE is reused instead of emitting duplicate CTE names.
    """

    actor = _uuid(user_id)
    if actor is None or docs_library_id is None:
        return false()
    required = "write" if required == "write" else "read"

    known_owner = _uuid(library_owner_id)
    owner_match = known_owner is not None and known_owner == actor
    # The owner hint may come from an ORM row read before a concurrent transfer.
    # It can avoid building a share CTE, but cannot replace live authorization.
    # This uncorrelated lookup is constant for the library, not per-node I/O.
    library_owner = select(literal(1)).select_from(DocsLibrary).where(
        DocsLibrary.id == docs_library_id,
        DocsLibrary.owner_user_id == actor,
    ).exists()
    project_visible = _project_permission_predicate(
        node_model.project_id,
        user_id=actor,
        required=required,
    )

    if owner_match:
        # A known Personal owner needs no share graph at all.  Discard a
        # caller-provided CTE too, so owner search/query SQL stays on the
        # simple library/project predicates.
        shared_nodes = None
    if shared_nodes is None and not owner_match:
        shared_nodes = _shared_nodes_cte(
            docs_library_id=docs_library_id,
            user_id=actor,
            required=required,
            name=f"docs_shared_nodes_{abs(id(node_model))}",
        )
    if shared_nodes is None:
        shared_visible = false()
    else:
        shared_visible = select(literal(1)).select_from(shared_nodes).where(
            shared_nodes.c.node_id == node_model.id,
            shared_nodes.c.docs_library_id == node_model.docs_library_id,
        ).exists()

    # Project-bound nodes derive access directly from the Project ACL.  An
    # ordinary Personal node is owner-private or explicitly shared.  The
    # project information hub is navigation metadata: its read-only shell is
    # also exposed when at least one active direct project child is readable.
    child = aliased(KnowledgeNode)
    child_project_visible = _project_permission_predicate(
        child.project_id,
        user_id=actor,
        required="read",
    )
    hub_visible = select(literal(1)).select_from(child).where(
        child.docs_library_id == docs_library_id,
        child.parent_id == node_model.id,
        child.project_id.is_not(None),
        child.archived_at.is_(None),
        child_project_visible,
    ).exists()

    system_key = _sql_stripped(node_model.system_key)
    identity_prefix = system_key.startswith("project_information:", autoescape=True)
    canonical_hub = aliased(KnowledgeNode)
    pointer_project = aliased(Project)
    canonical_tag = select(literal(1)).select_from(KnowledgeNodeSupertag).join(
        KnowledgeSupertag,
        KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
    ).where(
        KnowledgeNodeSupertag.node_id == node_model.id,
        KnowledgeSupertag.docs_library_id == docs_library_id,
        KnowledgeSupertag.system_key == "project_info",
    ).correlate(node_model).exists()
    canonical_identity = select(literal(1)).select_from(Project).where(
        Project.id == node_model.project_id,
        Project.knowledge_node_id == node_model.id,
        select(func.count(pointer_project.id)).where(
            pointer_project.knowledge_node_id == node_model.id
        ).correlate(node_model).scalar_subquery() == 1,
        # The pointer alone is not sufficient proof: a stale row can retain a
        # live project_id while carrying another project's identity key.
        system_key == func.concat(literal("project_information:"), Project.id),
        Project.deleted_at.is_(None),
        Project.is_completed.is_(False),
        node_model.node_type == "node",
        node_model.is_explicit_blank.is_(False),
        select(literal(1)).select_from(DocsLibrary).where(
            DocsLibrary.id == docs_library_id,
            DocsLibrary.library_type == "personal",
            DocsLibrary.owner_user_id == Project.owner_id,
        ).exists(),
        node_model.parent_id.is_not(None),
        node_model.root_page_id == node_model.parent_id,
        select(literal(1)).select_from(canonical_hub).where(
            canonical_hub.id == node_model.parent_id,
            canonical_hub.docs_library_id == docs_library_id,
            _sql_stripped(canonical_hub.system_key) == "project_information_root",
            canonical_hub.parent_id.is_(None),
            or_(canonical_hub.root_page_id.is_(None), canonical_hub.root_page_id == canonical_hub.id),
            canonical_hub.archived_at.is_(None),
            _sql_stripped(canonical_hub.title) == "案件情報",
        ).correlate(node_model).exists(),
        canonical_tag,
    ).exists()
    project_nodes = and_(
        node_model.project_id.is_not(None),
        or_(
            func.coalesce(system_key, "") == "",
            and_(
                system_key != "project_information_root",
                ~identity_prefix,
            ),
            and_(identity_prefix, canonical_identity),
        ),
        project_visible,
    )
    personal_nodes = and_(
        node_model.project_id.is_(None),
        or_(
            func.coalesce(system_key, "") == "",
            and_(
                system_key != "project_information_root",
                ~identity_prefix,
            ),
        ),
        or_(library_owner, shared_visible),
    )
    personal_library = select(literal(1)).select_from(DocsLibrary).where(
        DocsLibrary.id == node_model.docs_library_id,
        DocsLibrary.library_type == "personal",
    ).correlate(node_model).exists()
    # Match the point-read cleanup shortcuts before ordinary project ACLs.
    # A malformed project-bound hub is owner-only; an unbound hub must have
    # the canonical navigation shape even for its owner.
    owner_identity = and_(identity_prefix, library_owner)
    hub_nodes = and_(
        system_key == "project_information_root",
        or_(
            and_(node_model.project_id.is_not(None), library_owner),
            and_(
                node_model.project_id.is_(None),
                personal_library,
                node_model.parent_id.is_(None),
                or_(node_model.root_page_id.is_(None), node_model.root_page_id == node_model.id),
                _sql_stripped(node_model.title) == "案件情報",
                or_(
                    library_owner,
                    and_(literal(required == "read"), or_(hub_visible, shared_visible)),
                ),
            ),
        ),
    )
    return and_(
        node_model.docs_library_id == docs_library_id,
        or_(project_nodes, personal_nodes, owner_identity, hub_nodes),
    )


def apply_docs_visibility(
    stmt: Any,
    *,
    docs_library_id: UUID,
    user_id: UUID | str | None,
    library_owner_id: UUID | str | None = None,
    node_model: Any = KnowledgeNode,
    required: str = "read",
    shared_nodes: Any | None = None,
) -> Any:
    """Compose :func:`docs_readable_node_predicate` into a Select."""

    return stmt.where(
        docs_readable_node_predicate(
            node_model,
            docs_library_id=docs_library_id,
            user_id=user_id,
            library_owner_id=library_owner_id,
            required=required,
            shared_nodes=shared_nodes,
        )
    )


def docs_node_renderable_predicate(node_model: Any = KnowledgeNode) -> ColumnElement:
    """Return the SQL projection guard for list/search/sync node rows.

    Empty titles are first-class only for ordinary explicit blank paragraph
    rows.  ``body_json`` is encrypted, so projections must rely on the
    non-sensitive discriminator added by the Docs lifecycle migration.  Keep
    identity-bearing/system rows visible even when malformed so their
    fail-closed lifecycle state can be repaired or cleaned up explicitly.
    """

    return or_(
        func.btrim(node_model.title) != "",
        func.coalesce(func.btrim(node_model.system_key), "") != "",
        and_(
            node_model.title == "",
            node_model.is_explicit_blank.is_(True),
            node_model.node_type == "node",
            func.coalesce(func.btrim(node_model.system_key), "") == "",
        ),
    )


def is_docs_node_renderable(row: Any) -> bool:
    """Application-side equivalent for point-read serializers.

    List/search paths use :func:`docs_node_renderable_predicate`; direct
    ``/nodes/{id}`` reads already have an ORM row and can apply the same
    fail-closed rule without issuing a second SQL query.
    """

    try:
        if str(getattr(row, "title", "") or "").strip():
            return True
        if str(getattr(row, "system_key", "") or "").strip():
            return True
        discriminator = object()
        marker = getattr(row, "is_explicit_blank", discriminator)
        if marker is not discriminator:
            # The SQL column is authoritative once present; a false marker
            # must not fall back to an encrypted body that still carries a
            # stale ``blank:true`` envelope.
            return (
                marker is True
                and getattr(row, "title", None) == ""
                and str(getattr(row, "node_type", "node") or "") == "node"
            )
        body = getattr(row, "body_json", None)
        return (
            str(getattr(row, "node_type", "node") or "") == "node"
            and isinstance(body, dict)
            and body.get("format") == "doc_block"
            and body.get("block_type") == "paragraph"
            and body.get("blank") is True
        )
    except Exception:
        return False


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _permission_allows(permission: Any, required: str) -> bool:
    value = str(permission or "").lower()
    if required == "read":
        return value in {"read", "write", "owner", "admin"}
    return value in {"write", "owner", "admin"}


async def _project_permission(
    session: AsyncSession,
    project_id: UUID | None,
    user_id: UUID,
    required: str,
) -> bool:
    if project_id is None:
        return True
    try:
        return await ProjectRepository.has_permission(
            session,
            project_id=project_id,
            user_id=user_id,
            permission=required,
        )
    except Exception:
        # A malformed/legacy project row must fail closed, not turn an ACL
        # check into a 500 from an otherwise valid Docs request.
        return False


async def _ancestor_ids(
    session: AsyncSession,
    node: KnowledgeNode,
    *,
    include_self: bool = True,
) -> list[UUID]:
    """Return ``node`` and its parents, bounded and cycle-safe."""

    result: list[UUID] = [node.id] if include_self else []
    seen: set[UUID] = {node.id}
    current = node
    # A sane Docs tree is shallow; the bound also protects a corrupt cycle.
    for _ in range(512):
        parent_id = _uuid(getattr(current, "parent_id", None))
        if parent_id is None or parent_id in seen:
            break
        parent = await session.get(KnowledgeNode, parent_id)
        if parent is None or parent.docs_library_id != node.docs_library_id:
            break
        seen.add(parent_id)
        result.append(parent_id)
        current = parent
    return result


async def _share_permission(
    session: AsyncSession,
    node_ids: Iterable[UUID],
    user_id: UUID,
) -> str | None:
    """Return the nearest explicit share over a node/ancestor set.

    ``node_ids`` is ordered from the target node toward its root.  The
    nearest ACL is authoritative: a child-level ``read`` share intentionally
    downgrades a parent-level ``write`` share for that subtree, matching the
    frontend policy.
    """

    ids = [item for item in node_ids if item is not None]
    if not ids:
        return None
    try:
        from ..memory.models import KnowledgeNodeShare
    except ImportError:  # migration not installed yet
        return None
    try:
        rows = await session.execute(
            select(
                KnowledgeNodeShare.node_id,
                KnowledgeNodeShare.permission,
            ).where(
                KnowledgeNodeShare.node_id.in_(ids),
                KnowledgeNodeShare.user_id == user_id,
            )
        )
    except Exception:
        # A rolling deploy may run code before the migration.  Do not grant
        # access on an unknown table; owner/project ACLs still work.
        return None
    permission_by_node = {
        node_id: str(permission or "").lower()
        for node_id, permission in rows.all()
    }
    for node_id in ids:
        permission = permission_by_node.get(node_id)
        if permission in {"read", "write"}:
            return permission
    return None


async def _batch_share_permissions(
    session: AsyncSession,
    node_ids: Iterable[UUID],
    user_id: UUID,
) -> dict[UUID, str]:
    """Load all explicit shares for ``user_id`` in bounded queries.

    ``_share_permission`` intentionally remains the single-node authority for
    mutation/read paths.  Listing paths use this helper so a library with
    many nodes does not execute one share lookup per candidate.  A missing
    migration or a database error is deny-by-default, matching
    ``_share_permission``'s fail-closed behavior.
    """

    ids = list(dict.fromkeys(item for item in node_ids if item is not None))
    if not ids:
        return {}
    try:
        from ..memory.models import KnowledgeNodeShare
    except ImportError:  # migration not installed yet
        return {}
    try:
        row_sets = []
        for offset in range(0, len(ids), _ACL_IN_CHUNK_SIZE):
            row_sets.append(
                await session.execute(
                    select(
                        KnowledgeNodeShare.node_id,
                        KnowledgeNodeShare.permission,
                    ).where(
                        KnowledgeNodeShare.node_id.in_(
                            ids[offset : offset + _ACL_IN_CHUNK_SIZE]
                        ),
                        KnowledgeNodeShare.user_id == user_id,
                    )
                )
            )
    except Exception:
        # A rolling deploy may run code before the migration.  Do not grant
        # access on an unknown table; owner/project ACLs still work.
        return {}
    permissions: dict[UUID, str] = {}
    try:
        for rows in row_sets:
            for node_id, permission in rows.all():
                normalized_id = _uuid(node_id)
                if normalized_id is not None:
                    permissions[normalized_id] = str(permission or "").lower()
    except Exception:
        # Keep malformed rows from turning a listing into an implicit grant.
        return {}
    return permissions


async def _batch_project_permissions(
    session: AsyncSession,
    project_ids: Iterable[UUID],
    user_id: UUID,
    required: str,
) -> dict[UUID, bool]:
    """Resolve project ACLs for all candidate project IDs in bounded queries.

    The query shape mirrors :meth:`ProjectRepository.has_permission` exactly:
    a live project row, the actor's role, and that actor's optional membership
    row are joined before the shared effective-permission policy is applied.
    Missing/malformed rows or a query failure are denied rather than granting
    access on a partial result.
    """

    ids = {_uuid(item) for item in project_ids}
    ids.discard(None)
    if not ids:
        return {}
    try:
        from ..memory.models import ProjectMember, User
        from .project_permissions import has_effective_project_permission

        row_sets = []
        ids_list = list(ids)
        for offset in range(0, len(ids_list), _ACL_IN_CHUNK_SIZE):
            row_sets.append(
                await session.execute(
                    select(
                        Project.id,
                        Project.owner_id,
                        User.role,
                        ProjectMember.permissions,
                    )
                    .select_from(Project)
                    .join(User, User.id == user_id)
                    .outerjoin(
                        ProjectMember,
                        and_(
                            ProjectMember.project_id == Project.id,
                            ProjectMember.user_id == user_id,
                        ),
                    )
                    .where(
                        Project.id.in_(
                            ids_list[offset : offset + _ACL_IN_CHUNK_SIZE]
                        ),
                        Project.deleted_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                )
            )
    except Exception:
        # Project ACL failures must not expose project-owned Docs content.
        return {}

    permissions: dict[UUID, bool] = {}
    try:
        for result in row_sets:
            for project_id, owner_id, user_role, member_permissions in result.all():
                normalized_id = _uuid(project_id)
                if normalized_id is None:
                    continue
                try:
                    permissions[normalized_id] = bool(
                        has_effective_project_permission(
                            user_id=user_id,
                            user_role=user_role,
                            project_owner_id=owner_id,
                            member_permissions=member_permissions,
                            permission=required,
                        )
                    )
                except Exception:
                    # A malformed ACL row is deny-by-default.
                    permissions[normalized_id] = False
    except Exception:
        return {}
    return permissions


def _nearest_batch_share_permission(
    node_id: UUID,
    *,
    nodes_by_id: dict[UUID, KnowledgeNode],
    share_permissions: dict[UUID, str],
    docs_library_id: UUID,
) -> str | None:
    """Resolve nearest ancestor share from an in-memory library graph.

    This is the set-based counterpart to ``_ancestor_ids`` +
    ``_share_permission``.  It intentionally preserves the 512-hop bound,
    cycle guard, and cross-library parent stop condition.
    """

    current_id = node_id
    seen: set[UUID] = {node_id}
    for depth in range(513):
        permission = share_permissions.get(current_id)
        if permission in {"read", "write"}:
            return permission
        # ``_ancestor_ids`` permits at most 512 parent hops (and therefore
        # checks depths 0..512).  Do not inspect a 513th ancestor.
        if depth == 512:
            break

        current = nodes_by_id.get(current_id)
        if current is None:
            break
        parent_id = _uuid(getattr(current, "parent_id", None))
        if parent_id is None or parent_id in seen:
            break
        parent = nodes_by_id.get(parent_id)
        if parent is None or _uuid(getattr(parent, "docs_library_id", None)) != docs_library_id:
            break
        seen.add(parent_id)
        current_id = parent_id
    return None


async def batch_sync_node_access(
    session: AsyncSession,
    nodes: Iterable[KnowledgeNode],
    *,
    library: DocsLibrary,
    user_id: UUID | str | None,
) -> dict[UUID, dict[str, Any]]:
    """Resolve sync ``source/access/read_only`` metadata in bounded batches.

    ``serialize_docs_node_for_sync`` historically called ``can_write_node``
    once per row.  That point-read authority is intentionally unchanged, but a
    paginated pull may contain thousands of rows and must not issue one ACL
    query per node.  Walk only the page's bounded ancestor closure, then reuse
    the existing batch share/project permission helpers while resolving nearest
    shares in memory.  The returned map has the exact metadata shape consumed
    by mobile.
    """

    page_nodes = list(nodes)
    actor = _uuid(user_id)
    library_id = _uuid(getattr(library, "id", None))
    if not page_nodes or actor is None or library_id is None:
        return {}

    # Ancestors may not be present on the current page.  Walk only the parent
    # closure needed by this page rather than loading the entire library on
    # every pagination request.  Each level is queried in bounded chunks and
    # the same cycle/depth guard as ``_ancestor_ids`` is retained.
    nodes_by_id: dict[UUID, KnowledgeNode] = {}
    frontier: set[UUID] = set()
    for row in page_nodes:
        row_id = _uuid(getattr(row, "id", None))
        row_library_id = _uuid(
            getattr(row, "docs_library_id", getattr(row, "workspace_id", None))
        )
        if row_id is not None and row_library_id == library_id:
            nodes_by_id[row_id] = row
            parent_id = _uuid(getattr(row, "parent_id", None))
            if parent_id is not None:
                frontier.add(parent_id)

    known_missing: set[UUID] = set()
    expanded: set[UUID] = set()
    for _depth in range(512):
        active_frontier = frontier - expanded - known_missing
        if not active_frontier:
            break
        # Mark IDs before querying so a malformed cycle cannot schedule the
        # same parent repeatedly.  IDs already present on the page still need
        # expansion: their own parents may live on an earlier page.
        expanded.update(active_frontier)
        frontier_ids = list(active_frontier - nodes_by_id.keys())
        for offset in range(0, len(frontier_ids), _ACL_IN_CHUNK_SIZE):
            chunk = frontier_ids[offset : offset + _ACL_IN_CHUNK_SIZE]
            try:
                fetched_rows = list(
                    (
                        await session.execute(
                            select(KnowledgeNode).where(
                                KnowledgeNode.docs_library_id == library_id,
                                KnowledgeNode.id.in_(chunk),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            except Exception:
                # A rolling deploy or transient ACL-table failure must not turn
                # a shared row into an implicit grant.  Owner/project metadata
                # can still be resolved from the page itself.
                known_missing.update(chunk)
                continue
            fetched_ids: set[UUID] = set()
            for fetched in fetched_rows:
                fetched_id = _uuid(getattr(fetched, "id", None))
                fetched_library_id = _uuid(
                    getattr(
                        fetched,
                        "docs_library_id",
                        getattr(fetched, "workspace_id", None),
                    )
                )
                if fetched_id is None or fetched_library_id != library_id:
                    continue
                nodes_by_id[fetched_id] = fetched
                fetched_ids.add(fetched_id)
            known_missing.update(set(chunk) - fetched_ids)

        next_frontier: set[UUID] = set()
        for current_id in active_frontier:
            current = nodes_by_id.get(current_id)
            if current is None:
                continue
            parent_id = _uuid(getattr(current, "parent_id", None))
            if (
                parent_id is not None
                and parent_id not in expanded
                and parent_id not in known_missing
            ):
                next_frontier.add(parent_id)
        frontier = next_frontier

    share_permissions = await _batch_share_permissions(
        session,
        nodes_by_id,
        actor,
    )
    project_ids = {
        project_id
        for row in page_nodes
        for project_id in (_uuid(getattr(row, "project_id", None)),)
        if project_id is not None
    }
    project_write = await _batch_project_permissions(
        session,
        project_ids,
        actor,
        "write",
    )
    owner_id = _uuid(getattr(library, "owner_user_id", None))
    metadata: dict[UUID, dict[str, Any]] = {}
    for row in page_nodes:
        row_id = _uuid(getattr(row, "id", None))
        if row_id is None:
            continue
        raw_project_id = getattr(row, "project_id", None)
        project_id = _uuid(raw_project_id)
        if raw_project_id is not None:
            # Project-bound rows remain project-sourced even if a malformed or
            # revoked project ACL makes them read-only in this response.
            writable = bool(
                project_id is not None
                and getattr(row, "archived_at", None) is None
                and project_write.get(project_id, False)
            )
            access = "write" if writable else "read"
            source = "project"
        elif owner_id == actor:
            # Preserve the serializer's owner shortcut, including archived
            # personal rows (the old path did not call can_write_node here).
            source = "personal"
            access = "owner"
        else:
            shared_permission = _nearest_batch_share_permission(
                row_id,
                nodes_by_id=nodes_by_id,
                share_permissions=share_permissions,
                docs_library_id=library_id,
            )
            writable = bool(
                getattr(row, "archived_at", None) is None
                and getattr(row, "system_key", None) != "project_information_root"
                and shared_permission == "write"
            )
            source = "shared"
            access = "write" if writable else "read"
        metadata[row_id] = {
            "source": source,
            "access": access,
            "read_only": access == "read",
        }
    return metadata


async def library_can_read(
    session: AsyncSession,
    library: DocsLibrary | None = None,
    user_id: UUID | None = None,
    *,
    workspace: DocsLibrary | None = None,
) -> bool:
    """Check access to a library without exposing whether it has nodes."""

    # ``workspace`` is the pre-rename Python keyword. Keep it explicit at this
    # boundary; filesystem workspace values are unrelated.
    if library is None:
        library = workspace
    if library is None:
        return False

    actor = _uuid(user_id)
    if actor is None:
        return False
    owner_id = _uuid(getattr(library, "owner_user_id", None))
    if owner_id == actor:
        return True
    # A personal library has no library-level share.  Access must be
    # granted on a node (or an ancestor) and is evaluated by can_read_node.
    return False


async def library_can_write(
    session: AsyncSession,
    library: DocsLibrary | None = None,
    user_id: UUID | None = None,
    *,
    workspace: DocsLibrary | None = None,
) -> bool:
    if library is None:
        library = workspace
    if library is None:
        return False
    actor = _uuid(user_id)
    if actor is None:
        return False
    return _uuid(getattr(library, "owner_user_id", None)) == actor


async def can_read_node(
    session: AsyncSession,
    node: KnowledgeNode | UUID | str | None,
    user_id: UUID | str | None,
    *,
    required: str = "read",
    library: DocsLibrary | None = None,
    workspace: DocsLibrary | None = None,
    include_archived: bool = False,
) -> bool:
    """Return whether ``user_id`` may read/write a Docs node.

    ``required='write'`` is accepted as a convenience and uses the same
    ancestor-share semantics as ``can_write_node``.
    """

    actor = _uuid(user_id)
    if actor is None:
        return False
    if isinstance(node, (UUID, str)):
        node_id = _uuid(node)
        if node_id is None:
            return False
        row = await session.get(KnowledgeNode, node_id)
    else:
        row = node
    if row is None:
        return False
    if not include_archived and getattr(row, "archived_at", None) is not None:
        return False
    if library is None:
        library = workspace
    if library is None:
        library = await session.get(DocsLibrary, row.docs_library_id)
    if library is None or library.id != row.docs_library_id:
        return False
    required_permission = "write" if required == "write" else "read"
    system_key = str(getattr(row, "system_key", "") or "").strip()
    # The 案件情報 hub is owner-private navigation metadata.  Check its
    # identity before the generic project_id branch so a malformed hub carrying
    # a Project id cannot leak to that project's members or grant writes.
    if system_key == "project_information_root" and getattr(row, "project_id", None) is not None:
        if _uuid(getattr(library, "owner_user_id", None)) == actor:
            return True
        return False
    if system_key.startswith("project_information:"):
        if _uuid(getattr(library, "owner_user_id", None)) == actor:
            return True
        identity_project_id = _uuid(getattr(row, "project_id", None))
        if identity_project_id is None:
            return False
        # The exact suffix is part of the canonical identity contract.  A
        # mismatched prefix must not fall through to ordinary project ACL.
        if system_key != f"project_information:{identity_project_id}":
            return False
        try:
            pointer_rows = (
                await session.execute(
                    select(Project.id)
                    .where(Project.knowledge_node_id == row.id)
                    .limit(2)
                )
            ).scalars().all()
        except Exception:
            return False
        if len(pointer_rows) != 1 or pointer_rows[0] != identity_project_id:
            return False
        identity_project = await session.get(Project, identity_project_id)
        if (
            identity_project is None
            or identity_project.knowledge_node_id != row.id
            or identity_project.deleted_at is not None
            or identity_project.is_completed
            or getattr(library, "library_type", None) != "personal"
            or _uuid(getattr(library, "owner_user_id", None))
            != _uuid(getattr(identity_project, "owner_id", None))
        ):
            return False
        if (
            str(getattr(row, "node_type", "node") or "") != "node"
            or getattr(row, "is_explicit_blank", False) is True
            or getattr(row, "parent_id", None) is None
            or getattr(row, "root_page_id", None) != getattr(row, "parent_id", None)
        ):
            return False
        hub = await session.get(KnowledgeNode, row.parent_id)
        if (
            hub is None
            or getattr(hub, "docs_library_id", None) != row.docs_library_id
            or str(getattr(hub, "system_key", "") or "").strip()
            != "project_information_root"
            or getattr(hub, "parent_id", None) is not None
            or getattr(hub, "root_page_id", None) not in (None, hub.id)
            or getattr(hub, "archived_at", None) is not None
            or str(getattr(hub, "title", "") or "").strip() != "案件情報"
        ):
            return False
        try:
            has_tag = await session.scalar(
                select(KnowledgeNodeSupertag.node_id)
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
                )
                .where(
                    KnowledgeNodeSupertag.node_id == row.id,
                    KnowledgeSupertag.docs_library_id == row.docs_library_id,
                    KnowledgeSupertag.system_key == "project_info",
                )
                .limit(1)
            )
        except Exception:
            return False
        if has_tag is None:
            return False
    # Project mapping on the node is authoritative.  An explicit user share
    # is still valid for an ordinary Personal node, but cannot bypass a
    # revoked project membership on project-owned content.
    node_project_raw = getattr(row, "project_id", None)
    node_project_id = _uuid(node_project_raw)
    if node_project_raw is not None and node_project_id is None:
        return False
    # Unified Personal Libraries may host project-bound roots/descendants.
    # Project membership is authoritative for those nodes; do not require an
    # owner/share grant from the Personal Library as a second gate.
    effective_project_id = node_project_id
    if effective_project_id is not None:
        if not await _project_permission(
            session, effective_project_id, actor, required_permission
        ):
            return False
        # Project-bound nodes derive access directly from Project membership.
        return True

    # The Personal ``案件情報`` hub itself is owner-private metadata, but a
    # project member must be able to navigate to it when at least one direct
    # project child is readable. Children are still filtered independently by
    # their own project ACL, so this never leaks sibling titles/content.
    if system_key == "project_information_root":
        # The hub is navigation metadata, not a Project node.  Project
        # membership may expose it for read-only navigation, but must never
        # grant hub writes; only the Personal Library owner may mutate it.
        if (
            getattr(library, "library_type", None) != "personal"
            or getattr(row, "parent_id", None) is not None
            or getattr(row, "root_page_id", None) not in (None, row.id)
            or str(getattr(row, "title", "") or "").strip() != "案件情報"
        ):
            return False
        if required_permission == "write":
            return _uuid(getattr(library, "owner_user_id", None)) == actor
        try:
            child_rows = await session.execute(
                select(KnowledgeNode.project_id).where(
                    KnowledgeNode.docs_library_id == row.docs_library_id,
                    KnowledgeNode.parent_id == row.id,
                    KnowledgeNode.project_id.is_not(None),
                    KnowledgeNode.archived_at.is_(None),
                )
            )
            for child_project_id in child_rows.scalars().all():
                if await _project_permission(
                    session, _uuid(child_project_id), actor, "read"
                ):
                    return True
        except Exception:
            return False

    if _uuid(getattr(library, "owner_user_id", None)) == actor:
        return True
    shared = await _share_permission(session, await _ancestor_ids(session, row), actor)
    return _permission_allows(shared, required_permission)


async def can_write_node(
    session: AsyncSession,
    node: KnowledgeNode | UUID | str | None,
    user_id: UUID | str | None,
    *,
    library: DocsLibrary | None = None,
    workspace: DocsLibrary | None = None,
    include_archived: bool = False,
) -> bool:
    return await can_read_node(
        session,
        node,
        user_id,
        required="write",
        library=library,
        workspace=workspace,
        include_archived=include_archived,
    )


# Short aliases used by integrations that share the generic ``can_read`` /
# ``can_write`` vocabulary with Project ACL helpers.  Keep the explicit node
# names as the primary API so call sites make it clear that ancestor shares
# and archived-node handling are being evaluated.
async def can_read(
    session: AsyncSession,
    node: KnowledgeNode | UUID | str | None,
    user_id: UUID | str | None,
    *,
    library: DocsLibrary | None = None,
    workspace: DocsLibrary | None = None,
    include_archived: bool = False,
) -> bool:
    return await can_read_node(
        session,
        node,
        user_id,
        library=library,
        workspace=workspace,
        include_archived=include_archived,
    )


async def can_write(
    session: AsyncSession,
    node: KnowledgeNode | UUID | str | None,
    user_id: UUID | str | None,
    *,
    library: DocsLibrary | None = None,
    workspace: DocsLibrary | None = None,
    include_archived: bool = False,
) -> bool:
    return await can_write_node(
        session,
        node,
        user_id,
        library=library,
        workspace=workspace,
        include_archived=include_archived,
    )


async def accessible_node_ids(
    session: AsyncSession,
    docs_library_id: UUID,
    user_id: UUID | str | None,
    *,
    include_archived: bool = False,
) -> list[UUID]:
    """Materialize visible node IDs with set-based ACL lookups.

    The single-node :func:`can_read_node` helper remains the authority for
    mutation and point reads.  Listing a library, however, first loads its
    graph, all actor shares, and all referenced project permissions in bounded
    queries, then applies the same nearest-share/project/owner policy in
    memory.  Ordinary scopes use a fixed number of ``SELECT`` statements; very
    large ACL ``IN`` predicates are split into bounded chunks (never one query
    per candidate node).
    """

    library = await session.get(DocsLibrary, docs_library_id)
    if library is None:
        return []
    actor = _uuid(user_id)
    if actor is None:
        return []

    # Load archived rows as well: an archived ancestor can still carry the
    # nearest share for an active descendant.  Archived targets are filtered
    # only after the graph and share maps have been materialized.
    stmt = select(KnowledgeNode).where(KnowledgeNode.docs_library_id == docs_library_id)
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return []

    nodes_by_id: dict[UUID, KnowledgeNode] = {}
    for row in rows:
        row_id = _uuid(getattr(row, "id", None))
        if row_id is not None:
            nodes_by_id[row_id] = row

    share_permissions = await _batch_share_permissions(
        session,
        nodes_by_id,
        actor,
    )

    # Project identity is carried by each node.  Do not derive it from a
    # library discriminator (the old project-library column is retired).
    project_ids: set[UUID] = set()
    project_ids.update(
        project_id
        for row in rows
        for project_id in (_uuid(getattr(row, "project_id", None)),)
        if project_id is not None
    )
    project_permissions = await _batch_project_permissions(
        session,
        project_ids,
        actor,
        "read",
    )

    # Resolve canonical identity proof once for the batch.  A member-visible
    # Project-information node must be the active Project's exact pointer in
    # its owner's Personal Library, under the canonical hub and tag.  Stale or
    # duplicate rows remain owner cleanup data and cannot fall through to the
    # ordinary project ACL branch.
    identity_node_ids = {
        row_id
        for row in rows
        for row_id in (_uuid(getattr(row, "id", None)),)
        if row_id is not None
        and str(getattr(row, "system_key", "") or "").strip().startswith(
            "project_information:"
        )
    }
    strict_identity_ids: set[UUID] = set()
    if identity_node_ids:
        identity_hub = aliased(KnowledgeNode)
        try:
            identity_result = await session.execute(
                select(KnowledgeNode.id)
                .join(
                    Project,
                    and_(
                        Project.id == KnowledgeNode.project_id,
                        Project.knowledge_node_id == KnowledgeNode.id,
                    ),
                )
                .join(DocsLibrary, DocsLibrary.id == KnowledgeNode.docs_library_id)
                .join(
                    identity_hub,
                    and_(
                        identity_hub.id == KnowledgeNode.parent_id,
                        identity_hub.docs_library_id == KnowledgeNode.docs_library_id,
                    ),
                )
                .join(
                    KnowledgeNodeSupertag,
                    KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                )
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
                )
                .where(
                    KnowledgeNode.id.in_(identity_node_ids),
                    KnowledgeNode.docs_library_id == docs_library_id,
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.is_explicit_blank.is_(False),
                    func.btrim(KnowledgeNode.system_key)
                    == func.concat(literal("project_information:"), Project.id),
                    KnowledgeNode.parent_id.is_not(None),
                    KnowledgeNode.root_page_id == KnowledgeNode.parent_id,
                    Project.deleted_at.is_(None),
                    Project.is_completed.is_(False),
                    DocsLibrary.library_type == "personal",
                    DocsLibrary.owner_user_id == Project.owner_id,
                    identity_hub.system_key == "project_information_root",
                    identity_hub.parent_id.is_(None),
                    or_(
                        identity_hub.root_page_id.is_(None),
                        identity_hub.root_page_id == identity_hub.id,
                    ),
                    identity_hub.title == "案件情報",
                    identity_hub.archived_at.is_(None),
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                    KnowledgeSupertag.system_key == "project_info",
                )
            )
            strict_identity_ids = {
                row_id
                for row_id in identity_result.scalars().all()
                if row_id is not None
            }
        except Exception:
            # A rolling deployment/malformed identity table must not expose
            # identity rows to non-owners.  The member branch below simply
            # fails closed when this proof is unavailable.
            strict_identity_ids = set()

    owner_id = _uuid(getattr(library, "owner_user_id", None))
    ids: list[UUID] = []
    for row in rows:
        if not include_archived and getattr(row, "archived_at", None) is not None:
            continue
        if not is_docs_node_renderable(row):
            continue
        row_id = _uuid(getattr(row, "id", None))
        if row_id is None:
            continue

        system_key = str(getattr(row, "system_key", "") or "").strip()
        if system_key == "project_information_root":
            if owner_id == actor:
                if getattr(library, "library_type", None) == "personal":
                    ids.append(row.id)
            elif (
                getattr(library, "library_type", None) == "personal"
                and
                getattr(row, "project_id", None) is None
                and getattr(row, "parent_id", None) is None
                and getattr(row, "root_page_id", None) in (None, row.id)
                and str(getattr(row, "title", "") or "").strip() == "案件情報"
                and any(
                    child.parent_id == row.id
                    and child.project_id is not None
                    and (include_archived or child.archived_at is None)
                    and project_permissions.get(_uuid(child.project_id), False)
                    for child in rows
                )
            ):
                ids.append(row.id)
            continue

        if system_key.startswith("project_information:"):
            row_project_id = _uuid(getattr(row, "project_id", None))
            identity_suffix = system_key[len("project_information:") :].strip()
            if owner_id != actor and (
                row_project_id is None
                or identity_suffix != str(row_project_id)
                or row_id not in strict_identity_ids
                or not project_permissions.get(row_project_id, False)
            ):
                continue

        node_project_raw = getattr(row, "project_id", None)
        node_project_id = _uuid(node_project_raw)
        if node_project_raw is not None and node_project_id is None:
            continue
        effective_project_id = node_project_id
        if effective_project_id is not None:
            if not project_permissions.get(effective_project_id, False):
                continue
            # Project-bound rows are directly granted by Project ACL.
            ids.append(row.id)
            continue

        if owner_id == actor:
            ids.append(row.id)
            continue

        shared = _nearest_batch_share_permission(
            row_id,
            nodes_by_id=nodes_by_id,
            share_permissions=share_permissions,
            docs_library_id=docs_library_id,
        )
        if _permission_allows(shared, "read"):
            ids.append(row.id)
    return ids


async def accessible_project_ids(
    session: AsyncSession,
    user_id: UUID | str | None,
) -> list[UUID]:
    """Return Project IDs for which the actor has read access."""

    actor = _uuid(user_id)
    if actor is None:
        return []
    result = await session.execute(select(Project.id).where(Project.deleted_at.is_(None)))
    ids: list[UUID] = []
    for project_id in result.scalars().all():
        if await _project_permission(session, project_id, actor, "read"):
            ids.append(project_id)
    return ids


__all__ = [
    "accessible_node_ids",
    "accessible_project_ids",
    "apply_docs_visibility",
    "batch_sync_node_access",
    "can_read",
    "can_read_node",
    "can_write",
    "can_write_node",
    "docs_readable_node_predicate",
    "library_can_read",
    "library_can_write",
    "workspace_can_read",
    "workspace_can_write",
]


# Deprecated names for integrations deployed before the Docs Library rename.
workspace_can_read = library_can_read
workspace_can_write = library_can_write
