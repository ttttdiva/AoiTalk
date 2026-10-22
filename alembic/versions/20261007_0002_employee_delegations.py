"""Durable employee orders and encrypted, attempt-bound outputs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261007_0002"
down_revision = "20261007_0001"
branch_labels = None
depends_on = None


def upgrade():
    def ref(name, table, *, nullable=False, ondelete="RESTRICT", primary_key=False):
        return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(table + ".id", ondelete=ondelete), nullable=nullable, primary_key=primary_key)

    op.create_table("agent_delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        ref("delegator_agent_id", "agents"), ref("delegator_revision_id", "agent_revisions"),
        ref("worker_agent_id", "agents"), ref("worker_revision_id", "agent_revisions"),
        ref("reviewer_agent_id", "agents"), ref("reviewer_revision_id", "agent_revisions"),
        ref("project_id", "projects", ondelete="CASCADE"), ref("persona_id", "media_personas", nullable=True, ondelete="CASCADE"),
        ref("parent_delegation_id", "agent_delegations", nullable=True), ref("linked_work_item_id", "agent_work_items", nullable=True),
        sa.Column("linked_work_binding", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False), sa.Column("work_role", sa.String(32), nullable=False),
        sa.Column("instruction_ciphertext", sa.Text(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False), ref("created_by", "users"),
        sa.Column("round", sa.Integer(), nullable=False), ref("rework_from_work_item_id", "agent_work_items", nullable=True),
        sa.Column("last_discovered_at", sa.DateTime(), nullable=True), sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_delegations_idempotency_key"),
        sa.CheckConstraint("delegator_agent_id != worker_agent_id AND delegator_agent_id != reviewer_agent_id AND worker_agent_id != reviewer_agent_id", name="ck_agent_delegation_distinct_employees"),
        sa.CheckConstraint("round BETWEEN 1 AND 16", name="ck_agent_delegation_round"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_agent_delegation_hash"),
        sa.CheckConstraint("work_role IN ('researcher','writer','data_worker','general_worker','code_worker')", name="ck_agent_delegation_role"),
        sa.CheckConstraint("(work_role = 'code_worker' AND linked_work_item_id IS NOT NULL) OR (work_role != 'code_worker' AND linked_work_item_id IS NULL)", name="ck_agent_delegation_linked_work"),
    )
    for name in ("delegator_agent_id", "worker_agent_id", "project_id", "persona_id"):
        op.create_index("ix_agent_delegations_" + name, "agent_delegations", [name])
    op.create_table("agent_delegation_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        ref("delegation_id", "agent_delegations", ondelete="CASCADE"), ref("work_item_id", "agent_work_items"), ref("agent_run_id", "agent_runs"),
        sa.Column("phase", sa.String(16), nullable=False), sa.Column("body_ciphertext", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False), sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("work_item_id", "agent_run_id", name="uq_agent_delegation_artifact_attempt"),
        sa.CheckConstraint("phase IN ('work','review','return')", name="ck_agent_delegation_artifact_phase"),
        sa.CheckConstraint("length(sha256) = 64 AND size_bytes BETWEEN 1 AND 65536", name="ck_agent_delegation_artifact_content"),
        sa.CheckConstraint("(phase = 'review' AND accepted IN (0,1)) OR (phase != 'review' AND accepted IS NULL)", name="ck_agent_delegation_artifact_verdict"),
    )
    for name in ("delegation_id", "work_item_id"):
        op.create_index("ix_agent_delegation_artifacts_" + name, "agent_delegation_artifacts", [name])


def downgrade():
    raise RuntimeError("Employee delegation evidence is forward-only; restore a verified backup")
