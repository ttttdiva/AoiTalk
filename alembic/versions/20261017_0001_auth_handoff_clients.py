"""Add persistent state for registered browser handoff clients."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261017_0001"
down_revision = "20261016_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_handoff_client_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("recipient_state_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("registration_hash", sa.String(length=64), nullable=False),
        sa.Column("source_origin", sa.String(length=255), nullable=False),
        sa.Column("destination_origin", sa.String(length=255), nullable=False),
        sa.Column("callback_path", sa.String(length=512), nullable=False),
        sa.Column("audience", sa.String(length=255), nullable=False),
        sa.Column("pkce_challenge", sa.String(length=43), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("source_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_session_version", sa.Integer(), nullable=True),
        sa.Column("source_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_auth_handoff_client_transactions_request_hash",
        ),
        sa.CheckConstraint(
            "length(recipient_state_hash) = 64",
            name="ck_auth_handoff_client_transactions_recipient_state_hash",
        ),
        sa.CheckConstraint(
            "length(registration_hash) = 64",
            name="ck_auth_handoff_client_transactions_registration_hash",
        ),
        sa.CheckConstraint(
            "code_hash IS NULL OR length(code_hash) = 64",
            name="ck_auth_handoff_client_transactions_code_hash",
        ),
        sa.CheckConstraint(
            "length(pkce_challenge) = 43",
            name="ck_auth_handoff_client_transactions_pkce_challenge",
        ),
        sa.CheckConstraint(
            "source_session_version IS NULL OR source_session_version > 0",
            name="ck_auth_handoff_client_transactions_source_session_version",
        ),
        sa.ForeignKeyConstraint(
            ["source_user_id"],
            ["users.id"],
            name="fk_auth_handoff_client_transactions_source_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_hash",
            name="uq_auth_handoff_client_transactions_request_hash",
        ),
        sa.UniqueConstraint(
            "code_hash",
            name="uq_auth_handoff_client_transactions_code_hash",
        ),
    )
    op.create_index(
        "ix_auth_handoff_client_transactions_expires_at",
        "auth_handoff_client_transactions",
        ["expires_at"],
    )
    op.create_index(
        "ix_auth_handoff_client_transactions_source_user_id",
        "auth_handoff_client_transactions",
        ["source_user_id"],
    )
    op.create_index(
        "ix_auth_handoff_client_transactions_client_id_created_at",
        "auth_handoff_client_transactions",
        ["client_id", "created_at"],
    )

    op.create_table(
        "auth_handoff_rate_limits",
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "attempts > 0",
            name="ck_auth_handoff_rate_limits_attempts",
        ),
        sa.PrimaryKeyConstraint("bucket"),
    )


def downgrade() -> None:
    op.drop_table("auth_handoff_rate_limits")
    op.drop_index(
        "ix_auth_handoff_client_transactions_client_id_created_at",
        table_name="auth_handoff_client_transactions",
    )
    op.drop_index(
        "ix_auth_handoff_client_transactions_source_user_id",
        table_name="auth_handoff_client_transactions",
    )
    op.drop_index(
        "ix_auth_handoff_client_transactions_expires_at",
        table_name="auth_handoff_client_transactions",
    )
    op.drop_table("auth_handoff_client_transactions")
