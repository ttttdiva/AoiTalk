"""Create the trusted operations-kernel persistence tables.

The operations models keep external-account bindings, source evidence,
immutable proposal versions, human approvals, execution attempts/receipts and
the append-only operation timeline.  JSON columns intentionally use the same
server defaults as the ORM models while credential references and raw source
fields remain server-side data.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260830_0002"
down_revision = "20260830_0001"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "external_connections",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("provider_key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("remote_account_ref", sa.String(length=255), nullable=True),
        sa.Column("credential_ref", sa.Text(), nullable=True),
        sa.Column("auth_status", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_external_connections_version_positive"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "provider_key",
            "remote_account_ref",
            name="uq_external_connections_account",
        ),
    )
    op.create_index("ix_external_connections_owner_user_id", "external_connections", ["owner_user_id"])
    op.create_index("ix_external_connections_project_id", "external_connections", ["project_id"])
    op.create_index(
        "ix_external_connections_owner_project",
        "external_connections",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "artifact_versions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_versions_size_nonnegative"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_artifact_versions_sha256_length"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifact_versions_owner_user_id", "artifact_versions", ["owner_user_id"])
    op.create_index("ix_artifact_versions_project_id", "artifact_versions", ["project_id"])
    op.create_index("ix_artifact_versions_sha256", "artifact_versions", ["sha256"])
    op.create_index("ix_artifact_versions_created_at", "artifact_versions", ["created_at"])
    op.create_index(
        "ix_artifact_versions_owner_project",
        "artifact_versions",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "opportunities",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("connection_id", _uuid(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("source_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64 OR source_snapshot_hash IS NULL",
            name="ck_opportunities_snapshot_hash_length",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["external_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_opportunities_owner_user_id", "opportunities", ["owner_user_id"])
    op.create_index("ix_opportunities_project_id", "opportunities", ["project_id"])
    op.create_index("ix_opportunities_connection_id", "opportunities", ["connection_id"])
    op.create_index("ix_opportunities_source_snapshot_hash", "opportunities", ["source_snapshot_hash"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])
    op.create_index("ix_opportunities_created_at", "opportunities", ["created_at"])
    op.create_index(
        "ix_opportunities_owner_project",
        "opportunities",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "opportunity_evaluations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("opportunity_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("estimated_effort_hours", sa.Float(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("estimated_revenue", sa.Float(), nullable=True),
        sa.Column("fit", sa.String(length=32), nullable=True),
        sa.Column("risks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("missing_requirements", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_opportunity_evaluations_version_positive"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opportunity_id", "version", name="uq_opportunity_evaluations_version"),
    )
    op.create_index("ix_opportunity_evaluations_opportunity_id", "opportunity_evaluations", ["opportunity_id"])
    op.create_index("ix_opportunity_evaluations_owner_user_id", "opportunity_evaluations", ["owner_user_id"])
    op.create_index("ix_opportunity_evaluations_project_id", "opportunity_evaluations", ["project_id"])

    op.create_table(
        "application_drafts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("opportunity_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("offered_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("delivery_estimate", sa.String(length=255), nullable=True),
        sa.Column("artifact_version_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_application_drafts_version_positive"),
        sa.CheckConstraint("length(draft_hash) = 64", name="ck_application_drafts_hash_length"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opportunity_id", "version", name="uq_application_drafts_version"),
    )
    op.create_index("ix_application_drafts_opportunity_id", "application_drafts", ["opportunity_id"])
    op.create_index("ix_application_drafts_owner_user_id", "application_drafts", ["owner_user_id"])
    op.create_index("ix_application_drafts_project_id", "application_drafts", ["project_id"])
    op.create_index("ix_application_drafts_draft_hash", "application_drafts", ["draft_hash"])

    op.create_table(
        "external_actions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("opportunity_id", _uuid(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("connection_id", _uuid(), nullable=False),
        sa.Column("application_draft_id", _uuid(), nullable=False),
        sa.Column("application_draft_version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
        sa.Column(
            "action_type",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'engagement.submit_application'"),
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_hashes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("action_version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("action_type = 'engagement.submit_application'", name="ck_external_actions_type"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_external_actions_payload_hash_length"),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64 OR source_snapshot_hash IS NULL",
            name="ck_external_actions_source_hash_length",
        ),
        sa.CheckConstraint(
            "action_version > 0 AND version > 0 AND application_draft_version > 0",
            name="ck_external_actions_versions_positive",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["external_connections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["application_draft_id"], ["application_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_actions_owner_user_id", "external_actions", ["owner_user_id"])
    op.create_index("ix_external_actions_project_id", "external_actions", ["project_id"])
    op.create_index("ix_external_actions_opportunity_id", "external_actions", ["opportunity_id"])
    op.create_index("ix_external_actions_connection_id", "external_actions", ["connection_id"])
    op.create_index("ix_external_actions_application_draft_id", "external_actions", ["application_draft_id"])
    op.create_index("ix_external_actions_payload_hash", "external_actions", ["payload_hash"])
    op.create_index("ix_external_actions_status", "external_actions", ["status"])
    op.create_index("ix_external_actions_created_at", "external_actions", ["created_at"])
    op.create_index(
        "ix_external_actions_owner_project",
        "external_actions",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_external_actions_personal_idempotency",
        "external_actions",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_external_actions_project_idempotency",
        "external_actions",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "external_action_approvals",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("action_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_hashes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved','rejected','invalidated')",
            name="ck_external_action_approvals_decision",
        ),
        sa.CheckConstraint("action_version > 0", name="ck_external_action_approvals_version_positive"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_external_action_approvals_hash_length"),
        sa.ForeignKeyConstraint(["action_id"], ["external_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_action_approvals_action_id", "external_action_approvals", ["action_id"])
    op.create_index("ix_external_action_approvals_owner_user_id", "external_action_approvals", ["owner_user_id"])
    op.create_index(
        "ix_external_action_approvals_action_version",
        "external_action_approvals",
        ["action_id", "action_version"],
    )

    op.create_table(
        "external_action_attempts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("action_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("executor_type", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("provider_attempt_ref", sa.String(length=255), nullable=True),
        sa.Column("evidence_artifact_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.CheckConstraint("executor_type = 'manual'", name="ck_external_action_attempts_executor_type"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','uncertain')",
            name="ck_external_action_attempts_status",
        ),
        sa.CheckConstraint("action_version > 0", name="ck_external_action_attempts_version_positive"),
        sa.ForeignKeyConstraint(["action_id"], ["external_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_action_attempts_action_id", "external_action_attempts", ["action_id"])
    op.create_index("ix_external_action_attempts_owner_user_id", "external_action_attempts", ["owner_user_id"])
    op.create_index("ix_external_action_attempts_status", "external_action_attempts", ["status"])

    op.create_table(
        "external_action_receipts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("action_id", _uuid(), nullable=False),
        sa.Column("attempt_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("provider_receipt_ref", sa.String(length=255), nullable=True),
        sa.Column("remote_resource_id", sa.String(length=255), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("remote_status", sa.String(length=64), nullable=True),
        sa.Column("provider_observed_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column(
            "confirmation_level",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'human_confirmed'"),
        ),
        sa.Column("evidence_artifact_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "confirmation_level IN ('human_confirmed','provider_confirmed','reconciled')",
            name="ck_external_action_receipts_confirmation_level",
        ),
        sa.CheckConstraint("action_version > 0", name="ck_external_action_receipts_version_positive"),
        sa.ForeignKeyConstraint(["action_id"], ["external_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["external_action_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_external_action_receipts_attempt"),
    )
    op.create_index("ix_external_action_receipts_action_id", "external_action_receipts", ["action_id"])
    op.create_index("ix_external_action_receipts_owner_user_id", "external_action_receipts", ["owner_user_id"])

    op.create_table(
        "operation_events",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", _uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_id", _uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False, server_default=sa.text("'human'")),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('human','system','agent')",
            name="ck_operation_events_actor_type",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operation_events_owner_user_id", "operation_events", ["owner_user_id"])
    op.create_index("ix_operation_events_project_id", "operation_events", ["project_id"])
    op.create_index("ix_operation_events_entity_id", "operation_events", ["entity_id"])
    op.create_index("ix_operation_events_created_at", "operation_events", ["created_at"])
    op.create_index(
        "ix_operation_events_entity_created",
        "operation_events",
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_operation_events_project_created",
        "operation_events",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    # Drop dependants first so PostgreSQL can remove each FK without CASCADE.
    op.drop_table("operation_events")
    op.drop_table("external_action_receipts")
    op.drop_table("external_action_attempts")
    op.drop_table("external_action_approvals")
    op.drop_table("external_actions")
    op.drop_table("application_drafts")
    op.drop_table("opportunity_evaluations")
    op.drop_table("opportunities")
    op.drop_table("artifact_versions")
    op.drop_table("external_connections")
