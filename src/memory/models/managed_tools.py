"""Durable lineage and promotion records for agent-created managed tools.

The managed-tool tables deliberately keep *identity and semantic evidence* only.
Raw command output belongs to :class:`AgentRunToolCall` and is never copied here.
This lets the promotion policy answer "was this useful in independent runs?" while
keeping one canonical execution telemetry source.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: Any) -> str | None:
    return str(value) if value else None


MANAGED_TOOL_RUNTIMES = {"python", "powershell", "shell", "node"}
MANAGED_TOOL_STATUSES = {"active", "promoted", "archived"}


class ManagedToolLineage(Base):
    """Stable identity for one agent-created script/tool family.

    ``id`` is minted by the server and remains stable as revisions change.  A
    caller can only create this row through ``ManagedToolService``; the service
    chooses ``canonical_path`` below the authenticated user's managed-tools
    root, so an arbitrary existing file cannot be enrolled.
    """

    __tablename__ = "managed_tool_lineages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    app_id = Column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    runtime = Column(String(32), nullable=False)
    # Absolute canonical path is intentionally server-minted.  ``entrypoint``
    # is the stable relative path inside the user managed-tools namespace.
    canonical_path = Column(Text, nullable=False)
    entrypoint = Column(String(255), nullable=False)
    source_kind = Column(String(32), nullable=False, default="agent_generated")
    source_agent_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_root_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_revision_id = Column(
        UUID(as_uuid=True),
        # ``managed_tool_lineages`` and ``managed_tool_revisions`` reference
        # one another. ``use_alter`` lets PostgreSQL create the FK after both
        # tables exist; SQLite test schemas still retain the nullable pointer.
        ForeignKey(
            "managed_tool_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_managed_tool_lineages_current_revision",
        ),
        nullable=True,
        index=True,
    )
    current_sha256 = Column(String(64), nullable=True, index=True)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    discovery_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="active", index=True)
    promoted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_user_id], passive_deletes=True)
    project = relationship("Project", foreign_keys=[project_id])
    app = relationship("App", foreign_keys=[app_id])
    source_agent_run = relationship("AgentRun", foreign_keys=[source_agent_run_id])
    source_root_run = relationship("AgentRun", foreign_keys=[source_root_run_id])
    current_revision = relationship("ManagedToolRevision", foreign_keys=[current_revision_id], post_update=True)
    revisions = relationship(
        "ManagedToolRevision",
        back_populates="lineage",
        cascade="all, delete-orphan",
        passive_deletes=True,
        primaryjoin="ManagedToolLineage.id == ManagedToolRevision.lineage_id",
        foreign_keys="ManagedToolRevision.lineage_id",
        order_by="ManagedToolRevision.created_at",
    )
    observations = relationship(
        "ManagedToolObservation",
        back_populates="lineage",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ManagedToolObservation.created_at",
    )
    promotion_audits = relationship(
        "ManagedToolPromotionAudit",
        back_populates="lineage",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ManagedToolPromotionAudit.created_at",
    )

    __table_args__ = (
        CheckConstraint("runtime IN ('python','powershell','shell','node')", name="ck_managed_tool_lineages_runtime"),
        CheckConstraint("source_kind = 'agent_generated'", name="ck_managed_tool_lineages_source_kind"),
        CheckConstraint("status IN ('active','promoted','archived')", name="ck_managed_tool_lineages_status"),
        UniqueConstraint("owner_user_id", "canonical_path", name="uq_managed_tool_lineages_owner_path"),
        Index("ix_managed_tool_lineages_owner_status", "owner_user_id", "status"),
    )

    def to_dict(self, *, include_evidence: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "app_id": _uuid(self.app_id),
            "name": self.name,
            "description": self.description,
            "runtime": self.runtime,
            "canonical_path": self.canonical_path,
            "entrypoint": self.entrypoint,
            "source_kind": self.source_kind,
            "source_agent_run_id": _uuid(self.source_agent_run_id),
            "source_root_run_id": _uuid(self.source_root_run_id),
            "current_revision_id": _uuid(self.current_revision_id),
            "current_sha256": self.current_sha256,
            "policy_snapshot": self.policy_snapshot or {},
            "discovery": self.discovery_json or {},
            "status": self.status,
            "promoted_at": _dt(self.promoted_at),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }
        if include_evidence:
            # Avoid implicit async lazy-loads from a synchronous serializer.
            # Service list/get methods query these collections explicitly;
            # direct callers receive an empty collection when relationships
            # were not eagerly loaded.
            payload["revisions"] = [item.to_dict() for item in self.__dict__.get("revisions", ()) or ()]
            payload["observations"] = [item.to_dict() for item in self.__dict__.get("observations", ()) or ()]
            payload["promotion_audits"] = [item.to_dict() for item in self.__dict__.get("promotion_audits", ()) or ()]
        return payload


class ManagedToolRevision(Base):
    """One immutable SHA revision belonging to a managed-tool lineage."""

    __tablename__ = "managed_tool_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lineage_id = Column(
        UUID(as_uuid=True), ForeignKey("managed_tool_lineages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sha256 = Column(String(64), nullable=False, index=True)
    runtime = Column(String(32), nullable=False)
    entrypoint = Column(String(255), nullable=False)
    agent_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    root_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    # Metadata is semantic only (for example source intent and line count),
    # never stdout/stderr/result payloads.
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    lineage = relationship(
        "ManagedToolLineage",
        back_populates="revisions",
        foreign_keys=[lineage_id],
    )
    agent_run = relationship("AgentRun", foreign_keys=[agent_run_id])
    root_run = relationship("AgentRun", foreign_keys=[root_run_id])

    __table_args__ = (
        CheckConstraint("runtime IN ('python','powershell','shell','node')", name="ck_managed_tool_revisions_runtime"),
        UniqueConstraint("lineage_id", "sha256", name="uq_managed_tool_revisions_lineage_sha"),
        Index("ix_managed_tool_revisions_lineage_created", "lineage_id", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "lineage_id": _uuid(self.lineage_id),
            "sha256": self.sha256,
            "runtime": self.runtime,
            "entrypoint": self.entrypoint,
            "agent_run_id": _uuid(self.agent_run_id),
            "root_run_id": _uuid(self.root_run_id),
            "metadata": self.metadata_json or {},
            "created_at": _dt(self.created_at),
        }


class ManagedToolObservation(Base):
    """Semantic execution evidence linked back to existing AgentRun telemetry."""

    __tablename__ = "managed_tool_observations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lineage_id = Column(
        UUID(as_uuid=True), ForeignKey("managed_tool_lineages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id = Column(
        UUID(as_uuid=True), ForeignKey("managed_tool_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    root_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    observation_kind = Column(String(64), nullable=False, default="execution")
    semantic_key = Column(String(160), nullable=True)
    success = Column(Boolean, nullable=False, default=False, index=True)
    exit_code = Column(Integer, nullable=True)
    # Do not store stdout/stderr or the full AgentRun result here.  Consumers
    # can join ``agent_run_id`` to AgentRunToolCall for the canonical telemetry.
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    lineage = relationship("ManagedToolLineage", back_populates="observations")
    revision = relationship("ManagedToolRevision", foreign_keys=[revision_id])
    agent_run = relationship("AgentRun", foreign_keys=[agent_run_id])
    root_run = relationship("AgentRun", foreign_keys=[root_run_id])

    __table_args__ = (
        Index("ix_managed_tool_observations_lineage_success", "lineage_id", "success"),
        Index("ix_managed_tool_observations_lineage_root", "lineage_id", "root_run_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "lineage_id": _uuid(self.lineage_id),
            "revision_id": _uuid(self.revision_id),
            "agent_run_id": _uuid(self.agent_run_id),
            "root_run_id": _uuid(self.root_run_id),
            "observation_kind": self.observation_kind,
            "semantic_key": self.semantic_key,
            "success": bool(self.success),
            "exit_code": self.exit_code,
            "metadata": self.metadata_json or {},
            "created_at": _dt(self.created_at),
        }


class ManagedToolPromotionAudit(Base):
    """Immutable audit/evidence snapshot for each promotion or revision update."""

    __tablename__ = "managed_tool_promotion_audits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lineage_id = Column(
        UUID(as_uuid=True), ForeignKey("managed_tool_lineages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    app_id = Column(UUID(as_uuid=True), ForeignKey("apps.id", ondelete="SET NULL"), nullable=True, index=True)
    revision_id = Column(UUID(as_uuid=True), ForeignKey("managed_tool_revisions.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(32), nullable=False, default="promoted")
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    evidence_json = Column(JSON, nullable=False, default=dict)
    policy_snapshot = Column(JSON, nullable=False, default=dict)
    discovery_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    lineage = relationship("ManagedToolLineage", back_populates="promotion_audits")
    app = relationship("App", foreign_keys=[app_id])
    revision = relationship("ManagedToolRevision", foreign_keys=[revision_id])
    actor = relationship("User", foreign_keys=[actor_user_id])

    __table_args__ = (
        CheckConstraint("action IN ('promoted','updated','skipped')", name="ck_managed_tool_promotion_audits_action"),
        Index("ix_managed_tool_promotion_audits_lineage_created", "lineage_id", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "lineage_id": _uuid(self.lineage_id),
            "app_id": _uuid(self.app_id),
            "revision_id": _uuid(self.revision_id),
            "action": self.action,
            "actor_user_id": _uuid(self.actor_user_id),
            "evidence": self.evidence_json or {},
            "policy_snapshot": self.policy_snapshot or {},
            "discovery": self.discovery_json or {},
            "created_at": _dt(self.created_at),
        }


# Compatibility aliases make the domain discoverable to callers that use the
# shorter "managed tool" terminology while retaining one mapped class/table.
ManagedScriptLineage = ManagedToolLineage
ManagedScriptRevision = ManagedToolRevision
ManagedScriptObservation = ManagedToolObservation
ManagedScriptPromotionAudit = ManagedToolPromotionAudit
ManagedTool = ManagedToolLineage


__all__ = [
    "MANAGED_TOOL_RUNTIMES",
    "MANAGED_TOOL_STATUSES",
    "ManagedToolLineage",
    "ManagedToolRevision",
    "ManagedToolObservation",
    "ManagedToolPromotionAudit",
    "ManagedScriptLineage",
    "ManagedScriptRevision",
    "ManagedScriptObservation",
    "ManagedScriptPromotionAudit",
    "ManagedTool",
]
