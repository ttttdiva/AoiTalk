"""Canonical inventory rows for User Files / Project Files semantic indexing.

One row is the stable identity of one regular file below
``workspaces/_users/user_<uuid>`` or ``workspaces/_projects/project_<uuid>``.
The row id is the media-index ``workspace_file`` source id, so a detected
move/rename keeps its id and the existing vectors are reused.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base

WORKSPACE_FILE_SCOPES = ("user", "project")
WORKSPACE_FILE_STATUSES = ("pending_hash", "indexable", "excluded", "unsupported", "deleted")


class WorkspaceFileEntry(Base):
    __tablename__ = "workspace_file_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope = Column(String(16), nullable=False)
    # User UUID for ``scope='user'`` or Project UUID for ``scope='project'``.
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    # POSIX path relative to the user/project storage root.
    rel_path = Column(Text, nullable=False)
    name = Column(String(512), nullable=False)
    ext = Column(String(64), nullable=False, default="")
    mime = Column(String(255), nullable=True)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    mtime_ns = Column(BigInteger, nullable=False, default=0)
    content_hash = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="pending_hash")
    excluded_reason = Column(String(128), nullable=True)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_scanned_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("scope IN ('user','project')", name="ck_workspace_file_entries_scope"),
        CheckConstraint(
            "status IN ('pending_hash','indexable','excluded','unsupported','deleted')",
            name="ck_workspace_file_entries_status",
        ),
        Index(
            "uq_workspace_file_entries_live_path",
            "scope",
            "owner_id",
            "rel_path",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_workspace_file_entries_owner_status", "scope", "owner_id", "status"),
        Index("ix_workspace_file_entries_content_hash", "scope", "owner_id", "content_hash"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "scope": self.scope,
            "owner_id": str(self.owner_id),
            "rel_path": self.rel_path,
            "name": self.name,
            "ext": self.ext,
            "mime": self.mime,
            "size_bytes": int(self.size_bytes or 0),
            "status": self.status,
            "excluded_reason": self.excluded_reason,
            "last_scanned_at": self.last_scanned_at.isoformat() if self.last_scanned_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


class WorkspaceFileRootObservation(Base):
    """Scan evidence for FilesRoot only; unrelated to the StorageRoot registry.

    A baseline is established only by a complete nonempty scan accounting for
    every existing live path. Empty scans can confirm it, never replace it.
    """

    __tablename__ = "workspace_file_root_observations"

    scope = Column(String(16), primary_key=True)
    owner_id = Column(UUID(as_uuid=True), primary_key=True)
    directory_identity = Column(String(64), nullable=False)
    rules_fingerprint = Column(String(64), nullable=False)
    healthy_at = Column(DateTime, nullable=False)
    empty_first_seen_at = Column(DateTime, nullable=True)
    empty_last_seen_at = Column(DateTime, nullable=True)
    empty_observations = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("scope IN ('user','project')", name="ck_workspace_file_root_observations_scope"),
    )
