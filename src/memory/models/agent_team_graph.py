"""Persistent headers and immutable revision records for outer Agent Team graphs."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class AgentTeamGraph(Base):
    __tablename__ = "agent_team_graphs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(160), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    create_hash = Column(String(64), nullable=False)
    current_revision_id = Column(UUID(as_uuid=True), ForeignKey(
        "agent_team_graph_revisions.id", name="fk_agent_team_graphs_current_revision", use_alter=True
    ), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    archived_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("created_by", "idempotency_key", name="uq_agent_team_graphs_creator_idempotency"),
        CheckConstraint("length(title) BETWEEN 1 AND 160", name="ck_agent_team_graphs_title"),
        CheckConstraint("length(create_hash) = 64", name="ck_agent_team_graphs_create_hash"),
    )


class AgentTeamGraphRevision(Base):
    __tablename__ = "agent_team_graph_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    graph_id = Column(UUID(as_uuid=True), ForeignKey("agent_team_graphs.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    title = Column(String(160), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    content_hash = Column(String(64), nullable=False)
    definition_encrypted = Column(Text, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("graph_id", "version", name="uq_agent_team_graph_revisions_version"),
        CheckConstraint("version > 0", name="ck_agent_team_graph_revisions_version"),
        CheckConstraint("length(title) BETWEEN 1 AND 160", name="ck_agent_team_graph_revisions_title"),
        CheckConstraint("length(content_hash) = 64", name="ck_agent_team_graph_revisions_hash"),
    )
