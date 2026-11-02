"""Add the default-off external DOTS dialogue ledger.

Revision ID: 20261024_0001
Revises: 20261023_0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20261024_0001"
down_revision = "20261023_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dots_dialogue_bindings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("intended_chat_label", sa.String(120), nullable=False, server_default=""),
        sa.Column("owner_confirmed_at", sa.DateTime(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cursor_position", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("generation > 0", name="ck_dots_dialogue_binding_generation"),
        sa.CheckConstraint("state IN ('active', 'awaiting_confirmation', 'revoked')", name="ck_dots_dialogue_binding_state"),
        sa.CheckConstraint("next_sequence >= 0 AND cursor_position >= 0", name="ck_dots_dialogue_binding_cursor"),
    )
    op.create_index("ix_dots_dialogue_binding_owner_state", "dots_dialogue_bindings", ["owner_user_id", "state"])

    op.create_table(
        "dots_dialogue_subscriptions",
        sa.Column("id", sa.String(68), primary_key=True, nullable=False),
        sa.Column("binding_id", UUID(as_uuid=True), sa.ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_fingerprint", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("event_name", sa.String(100), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("callback_hash", sa.String(64), nullable=False),
        sa.Column("callback_url", sa.Text(), nullable=False),
        sa.Column("signing_secret", sa.Text(), nullable=False),
        sa.Column("previous_signing_secret", sa.Text(), nullable=True),
        sa.Column("secret_rotation_until", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("generation > 0", name="ck_dots_dialogue_subscription_generation"),
        sa.CheckConstraint("state IN ('active', 'revoked', 'expired')", name="ck_dots_dialogue_subscription_state"),
        sa.UniqueConstraint("binding_id", "generation", name="uq_dots_dialogue_subscription_generation"),
    )
    op.create_index("ix_dots_dialogue_subscription_principal", "dots_dialogue_subscriptions", ["principal_fingerprint", "state", "expires_at"])

    op.create_table(
        "dots_dialogue_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("binding_id", UUID(as_uuid=True), sa.ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.String(68), sa.ForeignKey("dots_dialogue_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversation_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivery_token_hash", sa.String(64), nullable=False),
        sa.Column("delivery_token", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("delivery_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("binding_id", "generation", "idempotency_hash", name="uq_dots_dialogue_request_idempotency"),
        sa.UniqueConstraint("binding_id", "generation", "sequence", name="uq_dots_dialogue_request_sequence"),
        sa.CheckConstraint("generation > 0 AND sequence > 0", name="ck_dots_dialogue_request_route"),
        sa.CheckConstraint("state IN ('pending', 'delivery_accepted', 'read', 'replied', 'cancelled', 'expired', 'delivery_failed')", name="ck_dots_dialogue_request_state"),
    )
    op.create_index("ix_dots_dialogue_request_owner_state", "dots_dialogue_requests", ["owner_user_id", "state", "created_at"])
    op.create_index("ix_dots_dialogue_request_expiry", "dots_dialogue_requests", ["expires_at", "state"])

    op.create_table(
        "dots_dialogue_outbox",
        sa.Column("request_id", UUID(as_uuid=True), sa.ForeignKey("dots_dialogue_requests.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("subscription_id", sa.String(68), sa.ForeignKey("dots_dialogue_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("cursor_after", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("lease_id", UUID(as_uuid=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("subscription_id", "generation", "sequence", name="uq_dots_dialogue_outbox_sequence"),
        sa.CheckConstraint("generation > 0 AND sequence > 0 AND attempt_count >= 0", name="ck_dots_dialogue_outbox_attempt"),
        sa.CheckConstraint("state IN ('pending', 'leased', 'acked', 'failed', 'cancelled')", name="ck_dots_dialogue_outbox_state"),
    )
    op.create_index("ix_dots_dialogue_outbox_due", "dots_dialogue_outbox", ["subscription_id", "generation", "state", "next_attempt_at", "sequence"])

    op.create_table(
        "dots_dialogue_operation_receipts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("binding_id", UUID(as_uuid=True), sa.ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), sa.ForeignKey("dots_dialogue_requests.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("operation_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="accepted"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("binding_id", "generation", "operation_hash", name="uq_dots_dialogue_operation_key"),
        sa.CheckConstraint("generation > 0", name="ck_dots_dialogue_operation_generation"),
        sa.CheckConstraint("state = 'accepted'", name="ck_dots_dialogue_operation_state"),
    )
    op.create_index("ix_dots_dialogue_operation_owner", "dots_dialogue_operation_receipts", ["owner_user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dots_dialogue_operation_owner", table_name="dots_dialogue_operation_receipts")
    op.drop_table("dots_dialogue_operation_receipts")
    op.drop_index("ix_dots_dialogue_outbox_due", table_name="dots_dialogue_outbox")
    op.drop_table("dots_dialogue_outbox")
    op.drop_index("ix_dots_dialogue_request_expiry", table_name="dots_dialogue_requests")
    op.drop_index("ix_dots_dialogue_request_owner_state", table_name="dots_dialogue_requests")
    op.drop_table("dots_dialogue_requests")
    op.drop_index("ix_dots_dialogue_subscription_principal", table_name="dots_dialogue_subscriptions")
    op.drop_table("dots_dialogue_subscriptions")
    op.drop_index("ix_dots_dialogue_binding_owner_state", table_name="dots_dialogue_bindings")
    op.drop_table("dots_dialogue_bindings")
