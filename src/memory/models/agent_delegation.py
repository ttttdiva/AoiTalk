"""Employee-to-employee orders; execution remains in the shared Work queue."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class AgentDelegation(Base):
    __tablename__ = "agent_delegations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delegator_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True)
    delegator_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=False)
    worker_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True)
    worker_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=False)
    reviewer_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False)
    reviewer_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    persona_id = Column(UUID(as_uuid=True), ForeignKey("media_personas.id", ondelete="CASCADE"), nullable=True, index=True)
    parent_delegation_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=True)
    linked_work_item_id = Column(UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="RESTRICT"), nullable=True)
    linked_work_binding = Column(JSON, nullable=False, default=dict)
    title = Column(String(160), nullable=False)
    work_role = Column(String(32), nullable=False)
    instruction_ciphertext = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    round = Column(Integer, nullable=False, default=1)
    rework_from_work_item_id = Column(UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="RESTRICT"), nullable=True)
    planning_policy_json = Column(JSON, nullable=False, default=dict, server_default="{}")
    planning_input_pins_json = Column(JSON, nullable=False, default=dict, server_default="{}")
    plan_round = Column(Integer, nullable=False, default=1, server_default="1")
    planning_root_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=True)
    adopted_plan_artifact_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegation_artifacts.id", ondelete="RESTRICT"), nullable=True)
    adopted_plan_sha256 = Column(String(64), nullable=True)
    parent_plan_artifact_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegation_artifacts.id", ondelete="RESTRICT"), nullable=True)
    parent_plan_sha256 = Column(String(64), nullable=True)
    parent_round = Column(Integer, nullable=True)
    planning_error_code = Column(String(80), nullable=True)
    planning_reserved_output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    planning_created_children = Column(Integer, nullable=False, default=0, server_default="0")
    max_output_tokens = Column(Integer, nullable=False, default=8192, server_default="8192")
    max_attempts_per_phase = Column(Integer, nullable=False, default=3, server_default="3")
    last_discovered_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("delegator_agent_id != worker_agent_id AND delegator_agent_id != reviewer_agent_id AND worker_agent_id != reviewer_agent_id", name="ck_agent_delegation_distinct_employees"),
        CheckConstraint("round BETWEEN 1 AND 16", name="ck_agent_delegation_round"),
        CheckConstraint("length(content_hash) = 64", name="ck_agent_delegation_hash"),
        CheckConstraint("work_role IN ('researcher','writer','data_worker','general_worker','code_worker')", name="ck_agent_delegation_role"),
        CheckConstraint("(work_role = 'code_worker' AND linked_work_item_id IS NOT NULL) OR (work_role != 'code_worker' AND linked_work_item_id IS NULL)", name="ck_agent_delegation_linked_work"),
    )

    def to_safe_dict(self):
        fields = ("id", "delegator_agent_id", "delegator_revision_id", "worker_agent_id", "worker_revision_id", "reviewer_agent_id", "reviewer_revision_id", "project_id", "persona_id", "parent_delegation_id", "linked_work_item_id", "created_by")
        result = {key: str(getattr(self, key)) if getattr(self, key) is not None else None for key in fields}
        result.update(title=self.title, work_role=self.work_role, round=self.round,
                      content_hash=self.content_hash,
                      cancelled_at=self.cancelled_at.isoformat() if self.cancelled_at else None,
                      created_at=self.created_at.isoformat() if self.created_at else None)
        return result


class AgentDelegationArtifact(Base):
    """Encrypted attempt output, adopted only after token-fenced settlement."""

    __tablename__ = "agent_delegation_artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delegation_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegations.id", ondelete="CASCADE"), nullable=False, index=True)
    work_item_id = Column(UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    agent_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False)
    phase = Column(String(16), nullable=False)
    body_ciphertext = Column(Text, nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    accepted = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("work_item_id", "agent_run_id", name="uq_agent_delegation_artifact_attempt"),
        CheckConstraint("phase IN ('plan','work','review','return')", name="ck_agent_delegation_artifact_phase"),
        CheckConstraint("length(sha256) = 64 AND size_bytes BETWEEN 1 AND 65536", name="ck_agent_delegation_artifact_content"),
        CheckConstraint("(phase = 'review' AND accepted IN (0,1)) OR (phase != 'review' AND accepted IS NULL)", name="ck_agent_delegation_artifact_verdict"),
    )

    def to_safe_dict(self):
        return {"id": str(self.id), "work_item_id": str(self.work_item_id),
                "agent_run_id": str(self.agent_run_id), "phase": self.phase,
                "sha256": self.sha256, "size_bytes": self.size_bytes,
                "accepted": bool(self.accepted) if self.accepted is not None else None}


class AgentDelegationBudgetReservation(Base):
    """Conservative, one-time output allowance consumed per durable attempt."""

    __tablename__ = "agent_delegation_budget_reservations"
    agent_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), primary_key=True)
    planning_root_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False, index=True)
    delegation_id = Column(UUID(as_uuid=True), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False)
    work_item_id = Column(UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="RESTRICT"), nullable=False)
    output_tokens = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("output_tokens BETWEEN 256 AND 8192", name="ck_delegation_budget_output_tokens"),)
