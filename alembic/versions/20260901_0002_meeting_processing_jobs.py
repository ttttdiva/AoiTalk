"""Create durable meeting-processing job ledger."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0002"
down_revision = "20260901_0001"
branch_labels = None
depends_on = None

_TABLE = "meeting_processing_jobs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "request_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "request_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "audio_upload_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "audio_file_name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "audio_mime_type",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column(
            "audio_size_bytes",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column(
            "audio_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "stage",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "retry_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "retryable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("lease_owner", sa.String(length=160)),
        sa.Column("lease_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("heartbeat_at", sa.DateTime()),
        sa.Column("next_attempt_at", sa.DateTime()),
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
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_meeting_processing_jobs_status",
        ),
        sa.CheckConstraint(
            "stage IN ("
            "'queued',"
            "'transcribing',"
            "'generating_minutes',"
            "'generating_memo',"
            "'persisting_minutes',"
            "'persisting_memo',"
            "'complete'"
            ")",
            name="ck_meeting_processing_jobs_stage",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_meeting_processing_jobs_actor_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_meeting_processing_jobs_actor_idempotency",
        ),
    )

    for name, columns in (
        (
            "ix_meeting_processing_jobs_actor_user_id",
            ["actor_user_id"],
        ),
        (
            "ix_meeting_processing_jobs_request_sha256",
            ["request_sha256"],
        ),
        (
            "ix_meeting_processing_jobs_audio_sha256",
            ["audio_sha256"],
        ),
        (
            "ix_meeting_processing_jobs_created_at",
            ["created_at"],
        ),
        (
            "ix_meeting_processing_jobs_claim",
            [
                "status",
                "next_attempt_at",
                "lease_expires_at",
                "created_at",
            ],
        ),
    ):
        op.create_index(name, _TABLE, columns, unique=False)


def downgrade() -> None:
    op.drop_table(_TABLE)
