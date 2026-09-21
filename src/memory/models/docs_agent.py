"""Protocol state for Docs consistency, indexing and Agent operations.

These rows never replace canonical Docs content. Revision/dirty rows are
maintained by PostgreSQL triggers so every writer participates.
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class DocsAuthorityState(Base):
    __tablename__ = "docs_authority_state"
    id = Column(Integer, primary_key=True)
    policy_revision = Column(BigInteger, nullable=False, default=0)


class DocsLibraryRevision(Base):
    __tablename__ = "docs_library_revisions"
    library_id = Column(UUID(as_uuid=True), primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0)


class DocsIndexQueue(Base):
    __tablename__ = "docs_index_queue"
    library_id = Column(UUID(as_uuid=True), primary_key=True)
    requested_revision = Column(BigInteger, nullable=False, default=0)
    applied_revision = Column(BigInteger, nullable=False, default=-1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DocsReadLease(Base):
    __tablename__ = "docs_read_leases"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    root_id = Column(UUID(as_uuid=True), nullable=False)
    library_id = Column(UUID(as_uuid=True), nullable=False)
    revision = Column(BigInteger, nullable=False)
    policy_revision = Column(BigInteger, nullable=False)
    node_ids = Column(JSON, nullable=False)
    scope_binding = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class DocsEditReadSession(Base):
    """Durable server-side checkpoint for ``docs_read(view='edit')``.

    This row intentionally contains protocol state only.  It never stores the
    projected segments, canonical body, or a model-facing response.  A page is
    rebuilt from canonical rows when it is delivered or replayed; the revision,
    policy revision, and read fingerprint make that rebuild fail closed when
    the source or authorization state changed.
    """

    __tablename__ = "docs_edit_read_sessions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    root_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    library_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    revision = Column(BigInteger, nullable=False)
    policy_revision = Column(BigInteger, nullable=False)
    scope_binding = Column(String(64), nullable=False, index=True)
    read_fingerprint = Column(String(64), nullable=True)
    depth = Column(Integer, nullable=False)
    page_chars = Column(Integer, nullable=False)
    turn_project_id = Column(UUID(as_uuid=True), nullable=True)
    next_offset = Column(Integer, nullable=False, default=0)
    last_page_start = Column(Integer, nullable=True)
    last_page_end = Column(Integer, nullable=True)
    # ``last_cursor`` is the exact request token whose response was delivered;
    # the empty string is the first-page request and is intentionally distinct
    # from NULL (no response has been committed yet).
    last_cursor = Column(String(128), nullable=True)
    next_cursor = Column(String(128), nullable=True)
    has_more = Column(Boolean, nullable=False, default=False)
    finished = Column(Boolean, nullable=False, default=False)
    lease_id = Column(UUID(as_uuid=True), nullable=True)
    terminal_status = Column(String(64), nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class DocsMutationReceipt(Base):
    __tablename__ = "docs_mutation_receipts"
    operation_id = Column(UUID(as_uuid=True), primary_key=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    root_id = Column(UUID(as_uuid=True), nullable=False)
    request_hash = Column(String(64), nullable=False)
    _result = Column("result_json", JSON, nullable=False)
    result = _encrypted_json_property("_result", "docs_mutation_receipts.result_json")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DocsCoverageRun(Base):
    __tablename__ = "docs_coverage_runs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    scope_binding = Column(String(64), nullable=False)
    _state = Column("state_json", JSON, nullable=False)
    state = _encrypted_json_property("_state", "docs_coverage_runs.state_json")
    expires_at = Column(DateTime, nullable=False, index=True)
