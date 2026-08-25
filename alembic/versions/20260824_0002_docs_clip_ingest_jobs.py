"""Create the durable Docs ClipIngest job ledger."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260824_0002"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None


_TABLE = "docs_clip_ingest_jobs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("docs_library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retry_of_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        # These columns contain field-crypto ciphertext when assigned by the
        # ORM.  Defaults only make legacy/direct inserts structurally valid.
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "upload_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "request_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "result_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "error_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_docs_clip_ingest_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name="fk_docs_clip_ingest_jobs_actor_user", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["docs_library_id"], ["docs_libraries.id"],
            name="fk_docs_clip_ingest_jobs_library", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["conversation_sessions.id"],
            name="fk_docs_clip_ingest_jobs_session", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_docs_clip_ingest_jobs_project", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"], ["knowledge_nodes.id"],
            name="fk_docs_clip_ingest_jobs_target_node", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_job_id"], ["docs_clip_ingest_jobs.id"],
            name="fk_docs_clip_ingest_jobs_retry_of", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["docs_clip_ingest_receipts.id"],
            name="fk_docs_clip_ingest_jobs_receipt", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id", "idempotency_key",
            name="uq_docs_clip_ingest_jobs_actor_idempotency",
        ),
    )
    for name, columns in (
        ("ix_docs_clip_ingest_jobs_actor_user_id", ["actor_user_id"]),
        ("ix_docs_clip_ingest_jobs_docs_library_id", ["docs_library_id"]),
        ("ix_docs_clip_ingest_jobs_session_id", ["session_id"]),
        ("ix_docs_clip_ingest_jobs_project_id", ["project_id"]),
        ("ix_docs_clip_ingest_jobs_target_node_id", ["target_node_id"]),
        ("ix_docs_clip_ingest_jobs_retry_of_job_id", ["retry_of_job_id"]),
        ("ix_docs_clip_ingest_jobs_receipt_id", ["receipt_id"]),
        ("ix_docs_clip_ingest_jobs_source_sha256", ["source_sha256"]),
        ("ix_docs_clip_ingest_jobs_claim", ["status", "lease_expires_at", "created_at"]),
    ):
        op.create_index(name, _TABLE, columns, unique=False)


def downgrade() -> None:
    op.drop_table(_TABLE)
