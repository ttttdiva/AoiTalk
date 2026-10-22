"""Add the persistent, single-use browser authentication handoff store."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261016_0001"
down_revision = "20261015_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_handoff_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("binding_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("pkce_challenge", sa.String(length=64), nullable=False),
        sa.Column("pkce_verifier", sa.String(length=128), nullable=False),
        sa.Column("source_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_session_version", sa.Integer(), nullable=True),
        sa.Column("source_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_origin", sa.String(length=255), nullable=False),
        sa.Column("destination_origin", sa.String(length=255), nullable=False),
        sa.Column("callback_path", sa.String(length=512), nullable=False),
        sa.Column("return_path", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(state_hash) = 64",
            name="ck_auth_handoff_transactions_state_hash",
        ),
        sa.CheckConstraint(
            "length(binding_hash) = 64",
            name="ck_auth_handoff_transactions_binding_hash",
        ),
        sa.CheckConstraint(
            "code_hash IS NULL OR length(code_hash) = 64",
            name="ck_auth_handoff_transactions_code_hash",
        ),
        sa.CheckConstraint(
            "length(pkce_challenge) = 43",
            name="ck_auth_handoff_transactions_pkce_challenge",
        ),
        sa.CheckConstraint(
            "source_session_version IS NULL OR source_session_version > 0",
            name="ck_auth_handoff_transactions_source_session_version",
        ),
        sa.CheckConstraint(
            "return_path LIKE '/%' AND return_path NOT LIKE '//%'",
            name="ck_auth_handoff_transactions_return_path",
        ),
        sa.ForeignKeyConstraint(
            ["source_user_id"],
            ["users.id"],
            name="fk_auth_handoff_transactions_source_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "state_hash", name="uq_auth_handoff_transactions_state_hash"
        ),
        sa.UniqueConstraint(
            "code_hash", name="uq_auth_handoff_transactions_code_hash"
        ),
    )
    op.create_index(
        "ix_auth_handoff_transactions_expires_at",
        "auth_handoff_transactions",
        ["expires_at"],
    )
    op.create_index(
        "ix_auth_handoff_transactions_source_user_id",
        "auth_handoff_transactions",
        ["source_user_id"],
    )
    op.create_index(
        "ix_auth_handoff_transactions_binding_hash",
        "auth_handoff_transactions",
        ["binding_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_handoff_transactions_binding_hash",
        table_name="auth_handoff_transactions",
    )
    op.drop_index(
        "ix_auth_handoff_transactions_source_user_id",
        table_name="auth_handoff_transactions",
    )
    op.drop_index(
        "ix_auth_handoff_transactions_expires_at",
        table_name="auth_handoff_transactions",
    )
    op.drop_table("auth_handoff_transactions")

