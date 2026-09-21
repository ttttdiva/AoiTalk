"""Cross-writer revision and edit-lease boundary for canonical Docs."""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import any_, bindparam, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID

from ..memory.models.docs_agent import DocsAuthorityState, DocsLibraryRevision, DocsReadLease

DOCS_LOCK_NAMESPACE = 1146045267


class DocsConflict(ValueError):
    """Optimistic/concurrency conflict surfaced by a Docs protocol boundary."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = bool(retryable)


class DocsContractUnavailable(ValueError):
    pass


async def require_active_actor(session, actor_id):
    from ..memory.models import User
    active = await session.scalar(select(User.id).where(User.id == actor_id, User.is_active.is_(True)))
    if active is None:
        raise PermissionError("Authenticated Docs actor is inactive or unavailable")


def is_postgres(session) -> bool:
    try:
        candidate = session if hasattr(session, "get_bind") else session.session
        if inspect.iscoroutinefunction(candidate.get_bind):
            return False
        return candidate.get_bind().dialect.name == "postgresql"
    except (AttributeError, TypeError):
        return False


def docs_id_predicate(column, values, session):
    """Large resolved scopes use one UUID-array bind, not 150k parameters."""
    values = list(values)
    if is_postgres(session):
        return column == any_(bindparam(None, values, type_=ARRAY(PG_UUID(as_uuid=True))))
    return column.in_(values)


async def contract_available(session) -> bool:
    if not is_postgres(session):
        return False
    return bool(await session.scalar(text("""
      SELECT to_regclass(current_schema() || '.docs_authority_state') IS NOT NULL
        AND to_regclass(current_schema() || '.docs_edit_read_sessions') IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM pg_proc p
          JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname=current_schema()
            AND p.proname='docs_write_guard'
            AND pg_get_functiondef(p.oid) NOT ILIKE '%pg_try_advisory_xact_lock%'
        )
        AND NOT EXISTS (
           SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=current_schema() AND c.relname IN (
             'docs_libraries','knowledge_nodes','knowledge_fields','knowledge_field_values',
             'knowledge_supertags','knowledge_node_supertags','knowledge_supertag_fields',
             'knowledge_node_placements','knowledge_edges','knowledge_attachments','tasks',
             'project_qa_entries','users','projects','project_members','knowledge_node_shares','project_knowledge_refs')
           AND (NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='docs_record_change' AND t.tgenabled='O')
             OR NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='docs_record_truncate' AND t.tgenabled='O')))
    """)))


async def lock_docs_writes(session):
    """Acquire the Agent-only Docs mutation serialization boundary.

    The follow-on writer-boundary migration makes ``docs_write_guard`` a
    compatibility no-op.  This advisory lock is consequently acquired only by
    Agent protocol paths (lease issuance and mutation) and never by ordinary
    Task/Project/User/raw Docs DML.  Agent mutations still take their concrete
    canonical/domain row locks with ``NOWAIT`` after this short critical
    section; ordinary writers therefore cannot be failed by Agent contention.
    """
    if not await contract_available(session):
        raise DocsContractUnavailable("Docs Agent consistency migration is required")
    await session.execute(text("SELECT pg_advisory_xact_lock(:namespace,1)"), {"namespace": DOCS_LOCK_NAMESPACE})


def sqlstate(error: BaseException) -> str | None:
    """Return a PostgreSQL SQLSTATE from SQLAlchemy/driver exceptions."""
    original = getattr(error, "orig", error)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def is_retryable_lock_error(error: BaseException) -> bool:
    """Whether ``error`` means an Agent row/advisory lock must be retried."""
    return sqlstate(error) in {"40001", "40P01", "55P03"}


async def execute_nowait(session, statement):
    """Execute a row-locking statement and translate contention for Agents.

    ``NOWAIT`` is deliberately scoped to Agent preflight.  A lock conflict is
    surfaced as ``DocsConflict`` so the caller rolls back only its transaction;
    ordinary writers never execute this helper.
    """
    try:
        return await session.execute(statement)
    except DBAPIError as error:
        if is_retryable_lock_error(error):
            raise DocsConflict(
                "Concurrent Docs/domain writer is busy; retry the Agent operation",
                retryable=True,
            ) from error
        raise


async def lock_docs_index(session):
    if is_postgres(session):
        await session.execute(text("SELECT pg_advisory_xact_lock(:namespace,2)"), {"namespace": DOCS_LOCK_NAMESPACE})


async def revision(session, library_id: UUID) -> tuple[int, int]:
    content = await session.scalar(select(DocsLibraryRevision.revision).where(DocsLibraryRevision.library_id == library_id))
    policy = await session.scalar(select(DocsAuthorityState.policy_revision).where(DocsAuthorityState.id == 1))
    if content is None or policy is None:
        raise DocsContractUnavailable("Docs revision state is unavailable; complete the migration")
    return int(content), int(policy)


def scope_binding(*, actor_id, project_id, include_project_context, strict_scope=False):
    payload = [str(actor_id), str(project_id or ""), bool(include_project_context), bool(strict_scope)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


async def issue_edit_lease(session, *, root, actor_id, before_revision, projection, binding):
    if not projection["coverage_complete"]:
        raise DocsConflict("Edit scope is incomplete; read a smaller section before mutation")
    await lock_docs_writes(session)
    await require_active_actor(session, actor_id)
    from .managed_docs_policy import assert_managed_docs_tree_mutation_allowed
    await assert_managed_docs_tree_mutation_allowed(session, root, tool_name="docs_mutate")
    from ..memory.models import DocsLibrary, KnowledgeNode
    from .docs_acl import docs_readable_node_predicate
    owner = await session.scalar(select(DocsLibrary.owner_user_id).where(DocsLibrary.id == root.docs_library_id))
    writable = await session.scalar(select(KnowledgeNode.id).where(KnowledgeNode.id == root.id,
        docs_readable_node_predicate(KnowledgeNode, docs_library_id=root.docs_library_id,
            user_id=actor_id, library_owner_id=owner, required="write")))
    if writable is None:
        raise PermissionError("Docs edit view requires write permission")
    current = await revision(session, root.docs_library_id)
    if current != before_revision:
        raise DocsConflict("Docs changed while preparing the edit; read again")
    now = datetime.utcnow()
    await session.execute(delete(DocsReadLease).where(DocsReadLease.expires_at < now))
    lease = DocsReadLease(
        id=uuid4(), actor_id=actor_id, root_id=root.id, library_id=root.docs_library_id,
        revision=current[0], policy_revision=current[1],
        node_ids=projection.pop("node_ids"), scope_binding=binding,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(lease)
    await session.flush()
    return lease
