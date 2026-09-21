"""Merge the current conversation/docs heads and add Skill learning storage.

``20260831_0008`` was deployed as the convergence point for the independent
conversation client-message-id branch and the Docs lifecycle branch.  Keep
both parents explicit so databases already stamped at this revision retain
their deployed meaning and fresh databases apply both prerequisite schema
changes before the strict Skill-learning DDL below.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260831_0008"
down_revision = ("20260831_0001", "20260831_0007")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_usage_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_call_id", sa.String(length=160), nullable=True),
        sa.Column("invocation_path", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("skill_name", sa.String(length=160), nullable=False),
        sa.Column("skill_scope", sa.String(length=16), nullable=False),
        sa.Column("skill_path", sa.String(length=512), nullable=False),
        sa.Column("skill_hash", sa.String(length=64), nullable=False),
        sa.Column("skill_version", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["conversation_sessions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["conversation_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "skill_scope IN ('global', 'project')",
            name="ck_skill_usage_receipts_scope",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'error')",
            name="ck_skill_usage_receipts_outcome",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_skill_usage_receipts_idempotency_key",
        ),
    )
    for column in (
        "user_id",
        "project_id",
        "session_id",
        "message_id",
        "agent_run_id",
        "tool_call_id",
        "invocation_path",
        "outcome",
        "skill_name",
        "skill_scope",
        "skill_hash",
        "idempotency_key",
        "created_at",
    ):
        op.create_index(
            f"ix_skill_usage_receipts_{column}",
            "skill_usage_receipts",
            [column],
        )
    op.create_index(
        "ix_skill_usage_receipts_skill_created",
        "skill_usage_receipts",
        ["skill_scope", "skill_name", "created_at"],
    )

    op.create_table(
        "skill_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("reason_type", sa.String(length=24), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("target_name", sa.String(length=160), nullable=False),
        sa.Column("target_path", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("proposed_content", sa.JSON(), nullable=False),
        sa.Column("base_snapshot", sa.JSON(), nullable=True),
        sa.Column("base_text", sa.Text(), nullable=True),
        sa.Column("base_hash", sa.String(length=64), nullable=True),
        sa.Column("base_version", sa.String(length=80), nullable=True),
        sa.Column("applied_hash", sa.String(length=64), nullable=True),
        sa.Column("applied_version", sa.String(length=80), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("stale_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["skill_usage_receipts.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'update')",
            name="ck_skill_proposals_operation",
        ),
        sa.CheckConstraint(
            "reason_type IN ('manual', 'correction', 'procedure')",
            name="ck_skill_proposals_reason_type",
        ),
        sa.CheckConstraint(
            "target_scope IN ('global', 'project')",
            name="ck_skill_proposals_scope",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'stale')",
            name="ck_skill_proposals_status",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_skill_proposals_idempotency_key",
        ),
    )
    for column in (
        "user_id",
        "project_id",
        "receipt_id",
        "operation",
        "reason_type",
        "target_scope",
        "target_name",
        "status",
        "base_hash",
        "applied_hash",
        "idempotency_key",
        "created_at",
    ):
        op.create_index(
            f"ix_skill_proposals_{column}",
            "skill_proposals",
            [column],
        )
    op.create_index(
        "ix_skill_proposals_target_status",
        "skill_proposals",
        ["target_scope", "project_id", "target_name", "status"],
    )

    op.create_table(
        "skill_proposal_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=24), nullable=False),
        sa.Column("actor_user_id", sa.String(length=200), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("observed_hash", sa.String(length=64), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["skill_proposals.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "proposal_id",
            "sequence",
            name="uq_skill_proposal_history_sequence",
        ),
    )
    op.create_index(
        "ix_skill_proposal_history_proposal_id",
        "skill_proposal_history",
        ["proposal_id"],
    )
    op.create_index(
        "ix_skill_proposal_history_event",
        "skill_proposal_history",
        ["event"],
    )
    op.create_index(
        "ix_skill_proposal_history_created_at",
        "skill_proposal_history",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_proposal_history_created_at", table_name="skill_proposal_history")
    op.drop_index("ix_skill_proposal_history_event", table_name="skill_proposal_history")
    op.drop_index("ix_skill_proposal_history_proposal_id", table_name="skill_proposal_history")
    op.drop_table("skill_proposal_history")

    op.drop_index("ix_skill_proposals_target_status", table_name="skill_proposals")
    for column in reversed(
        (
            "user_id",
            "project_id",
            "receipt_id",
            "operation",
            "reason_type",
            "target_scope",
            "target_name",
            "status",
            "base_hash",
            "applied_hash",
            "idempotency_key",
            "created_at",
        )
    ):
        op.drop_index(f"ix_skill_proposals_{column}", table_name="skill_proposals")
    op.drop_table("skill_proposals")

    op.drop_index("ix_skill_usage_receipts_skill_created", table_name="skill_usage_receipts")
    for column in reversed(
        (
            "user_id",
            "project_id",
            "session_id",
            "message_id",
            "agent_run_id",
            "tool_call_id",
            "invocation_path",
            "outcome",
            "skill_name",
            "skill_scope",
            "skill_hash",
            "idempotency_key",
            "created_at",
        )
    ):
        op.drop_index(
            f"ix_skill_usage_receipts_{column}",
            table_name="skill_usage_receipts",
        )
    op.drop_table("skill_usage_receipts")
