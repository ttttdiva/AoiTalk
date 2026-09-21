"""Add durable Resolution Knowledge Capture workflow state.

This migration creates only the new setting/candidate/question tables.  It
does not backfill historical Tasks; the bounded recovery scan handles missed
new completion enqueue paths after rollout.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260909_0001"
down_revision = "20260908_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_knowledge_capture_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'suggest'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_project_knowledge_capture_settings_project",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="SET NULL",
            name="fk_project_knowledge_capture_settings_updated_by",
        ),
        sa.UniqueConstraint(
            "project_id",
            name="uq_project_knowledge_capture_settings_project",
        ),
        sa.CheckConstraint(
            "mode IN ('off', 'suggest', 'auto')",
            name="ck_project_knowledge_capture_settings_mode",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_project_knowledge_capture_settings_version",
        ),
    )
    op.create_index(
        "ix_project_knowledge_capture_settings_project_id",
        "project_knowledge_capture_settings",
        ["project_id"],
    )
    op.create_index(
        "ix_project_knowledge_capture_settings_mode",
        "project_knowledge_capture_settings",
        ["mode"],
    )
    op.create_index(
        "ix_project_knowledge_capture_settings_updated_by",
        "project_knowledge_capture_settings",
        ["updated_by"],
    )

    op.create_table(
        "knowledge_capture_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seed_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_activity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completion_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("knowledge_semantic_key", sa.String(length=255), nullable=True),
        sa.Column(
            "terminal_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'closed'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "mode_snapshot",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'suggest'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reuse_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("question_rounds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("user_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("research_json", sa.JSON(), nullable=True),
        sa.Column("draft_json", sa.JSON(), nullable=True),
        sa.Column("answers_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("published_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_knowledge_capture_candidates_project",
        ),
        sa.ForeignKeyConstraint(
            ["seed_task_id"], ["tasks.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_seed_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_activity_id"], ["task_activities.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_task_activity",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_trigger_user",
        ),
        sa.ForeignKeyConstraint(
            ["published_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_published_node",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_target_node",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"], ["knowledge_revisions.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_target_revision",
        ),
        sa.ForeignKeyConstraint(
            ["published_revision_id"], ["knowledge_revisions.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_published_revision",
        ),
        sa.ForeignKeyConstraint(
            ["dismissed_by"], ["users.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_candidates_dismissed_by",
        ),
        sa.UniqueConstraint(
            "completion_fingerprint",
            name="uq_knowledge_capture_candidates_completion_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'pending', 'researching', 'needs_user', 'draft_ready', 'approved', 'published', 'dismissed', 'discarded', 'superseded', 'retry_wait', 'failed')",
            name="ck_knowledge_capture_candidates_status",
        ),
        sa.CheckConstraint(
            "terminal_status IN ('closed', 'cancelled')",
            name="ck_knowledge_capture_candidates_terminal_status",
        ),
        sa.CheckConstraint(
            "mode_snapshot IN ('off', 'suggest', 'auto')",
            name="ck_knowledge_capture_candidates_mode_snapshot",
        ),
        sa.CheckConstraint(
            "version >= 1 AND attempt_count >= 0 AND max_attempts BETWEEN 1 AND 20 AND question_rounds BETWEEN 0 AND 2",
            name="ck_knowledge_capture_candidates_counters",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_knowledge_capture_candidates_lease_triplet",
        ),
    )
    for name, columns in (
        ("ix_knowledge_capture_candidates_project_id", ["project_id"]),
        ("ix_knowledge_capture_candidates_seed_task_id", ["seed_task_id"]),
        ("ix_knowledge_capture_candidates_task_activity_id", ["task_activity_id"]),
        ("ix_knowledge_capture_candidates_trigger_user_id", ["trigger_user_id"]),
        ("ix_knowledge_capture_candidates_completion_fingerprint", ["completion_fingerprint"]),
        ("ix_knowledge_capture_candidates_knowledge_semantic_key", ["knowledge_semantic_key"]),
        ("ix_knowledge_capture_candidates_status", ["status"]),
        ("ix_knowledge_capture_candidates_mode_snapshot", ["mode_snapshot"]),
        ("ix_knowledge_capture_candidates_evidence_digest", ["evidence_digest"]),
        ("ix_knowledge_capture_candidates_published_node_id", ["published_node_id"]),
        ("ix_knowledge_capture_candidates_target_node_id", ["target_node_id"]),
        ("ix_knowledge_capture_candidates_target_revision_id", ["target_revision_id"]),
        ("ix_knowledge_capture_candidates_published_revision_id", ["published_revision_id"]),
        ("ix_knowledge_capture_candidates_lease_owner", ["lease_owner"]),
        ("ix_knowledge_capture_candidates_lease_expires_at", ["lease_expires_at"]),
        ("ix_knowledge_capture_candidates_next_retry_at", ["next_retry_at"]),
        ("ix_knowledge_capture_candidates_completed_at", ["completed_at"]),
        ("ix_knowledge_capture_candidates_dismissed_by", ["dismissed_by"]),
        ("ix_knowledge_capture_candidates_created_at", ["created_at"]),
        ("ix_knowledge_capture_candidates_updated_at", ["updated_at"]),
        (
            "ix_knowledge_capture_candidates_project_status_updated",
            ["project_id", "status", "updated_at"],
        ),
        (
            "ix_knowledge_capture_candidates_claimable",
            ["status", "next_retry_at", "lease_expires_at"],
        ),
    ):
        op.create_index(name, "knowledge_capture_candidates", columns)

    op.create_table(
        "knowledge_capture_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("options_json", sa.JSON(), nullable=True, server_default=sa.text("'[]'")),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answer_source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("candidate_version", sa.Integer(), nullable=True),
        sa.Column("asked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("answered_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("asked_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["knowledge_capture_candidates.id"], ondelete="CASCADE",
            name="fk_knowledge_capture_questions_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_knowledge_capture_questions_project",
        ),
        sa.ForeignKeyConstraint(
            ["asked_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_questions_asked_by",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_knowledge_capture_questions_answered_by",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "round_number",
            name="uq_knowledge_capture_questions_candidate_round",
        ),
        sa.CheckConstraint(
            "round_number BETWEEN 1 AND 2",
            name="ck_knowledge_capture_questions_round",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'dismissed')",
            name="ck_knowledge_capture_questions_status",
        ),
        sa.CheckConstraint(
            "version >= 1 AND (candidate_version IS NULL OR candidate_version >= 1)",
            name="ck_knowledge_capture_questions_version",
        ),
    )
    for name, columns in (
        ("ix_knowledge_capture_questions_candidate_id", ["candidate_id"]),
        ("ix_knowledge_capture_questions_project_id", ["project_id"]),
        ("ix_knowledge_capture_questions_status", ["status"]),
        ("ix_knowledge_capture_questions_evidence_digest", ["evidence_digest"]),
        ("ix_knowledge_capture_questions_candidate_version", ["candidate_version"]),
        ("ix_knowledge_capture_questions_asked_by_user_id", ["asked_by_user_id"]),
        ("ix_knowledge_capture_questions_answered_by_user_id", ["answered_by_user_id"]),
        ("ix_knowledge_capture_questions_project_status", ["project_id", "status"]),
    ):
        op.create_index(name, "knowledge_capture_questions", columns)
    op.create_index(
        "uq_knowledge_capture_questions_one_pending",
        "knowledge_capture_questions",
        ["candidate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade():
    op.drop_index(
        "uq_knowledge_capture_questions_one_pending",
        table_name="knowledge_capture_questions",
    )
    for name in (
        "ix_knowledge_capture_questions_project_status",
        "ix_knowledge_capture_questions_status",
        "ix_knowledge_capture_questions_candidate_version",
        "ix_knowledge_capture_questions_evidence_digest",
        "ix_knowledge_capture_questions_answered_by_user_id",
        "ix_knowledge_capture_questions_asked_by_user_id",
        "ix_knowledge_capture_questions_project_id",
        "ix_knowledge_capture_questions_candidate_id",
    ):
        op.drop_index(name, table_name="knowledge_capture_questions")
    op.drop_table("knowledge_capture_questions")

    for name in (
        "ix_knowledge_capture_candidates_claimable",
        "ix_knowledge_capture_candidates_project_status_updated",
        "ix_knowledge_capture_candidates_updated_at",
        "ix_knowledge_capture_candidates_created_at",
        "ix_knowledge_capture_candidates_dismissed_by",
        "ix_knowledge_capture_candidates_completed_at",
        "ix_knowledge_capture_candidates_next_retry_at",
        "ix_knowledge_capture_candidates_lease_expires_at",
        "ix_knowledge_capture_candidates_lease_owner",
        "ix_knowledge_capture_candidates_published_revision_id",
        "ix_knowledge_capture_candidates_target_revision_id",
        "ix_knowledge_capture_candidates_target_node_id",
        "ix_knowledge_capture_candidates_published_node_id",
        "ix_knowledge_capture_candidates_evidence_digest",
        "ix_knowledge_capture_candidates_mode_snapshot",
        "ix_knowledge_capture_candidates_status",
        "ix_knowledge_capture_candidates_knowledge_semantic_key",
        "ix_knowledge_capture_candidates_completion_fingerprint",
        "ix_knowledge_capture_candidates_trigger_user_id",
        "ix_knowledge_capture_candidates_task_activity_id",
        "ix_knowledge_capture_candidates_seed_task_id",
        "ix_knowledge_capture_candidates_project_id",
    ):
        op.drop_index(name, table_name="knowledge_capture_candidates")
    op.drop_table("knowledge_capture_candidates")

    for name in (
        "ix_project_knowledge_capture_settings_updated_by",
        "ix_project_knowledge_capture_settings_mode",
        "ix_project_knowledge_capture_settings_project_id",
    ):
        op.drop_index(name, table_name="project_knowledge_capture_settings")
    op.drop_table("project_knowledge_capture_settings")
