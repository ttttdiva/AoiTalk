"""Add append-only ContentItem stock adoption decisions.

Revision ID: 20261027_0001
Revises: 20261026_0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20261027_0001"
down_revision = "20261026_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_content_item_stock_decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "content_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("media_content_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=False),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("content_item_version", sa.Integer(), nullable=False),
        sa.Column("content_item_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("prev_event_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_media_content_item_stock_sequence"),
        sa.CheckConstraint(
            "event_type IN ('adopt', 'withdraw')",
            name="ck_media_content_item_stock_event_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'adopt' AND from_status = 'draft' AND to_status = 'ready') OR "
            "(event_type = 'withdraw' AND from_status = 'ready' AND to_status = 'draft')",
            name="ck_media_content_item_stock_event_target",
        ),
        sa.CheckConstraint("content_item_version > 1", name="ck_media_content_item_stock_version"),
        sa.CheckConstraint("length(content_item_hash) = 64", name="ck_media_content_item_stock_content_hash"),
        sa.CheckConstraint("actor_type IN ('human', 'admin')", name="ck_media_content_item_stock_actor_type"),
        sa.CheckConstraint("length(trim(idempotency_key)) BETWEEN 1 AND 255", name="ck_media_content_item_stock_idempotency_key"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_media_content_item_stock_request_hash"),
        sa.CheckConstraint("length(decision_hash) = 64", name="ck_media_content_item_stock_decision_hash"),
        sa.CheckConstraint(
            "prev_event_hash IS NULL OR length(prev_event_hash) = 64",
            name="ck_media_content_item_stock_prev_event_hash",
        ),
        sa.CheckConstraint("length(event_hash) = 64", name="ck_media_content_item_stock_event_hash"),
        sa.UniqueConstraint(
            "content_item_id",
            "sequence",
            name="uq_media_content_item_stock_sequence",
        ),
        sa.UniqueConstraint(
            "content_item_id",
            "idempotency_key",
            name="uq_media_content_item_stock_idempotency",
        ),
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_content_item_id",
        "media_content_item_stock_decisions",
        ["content_item_id"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_owner_user_id",
        "media_content_item_stock_decisions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_project_id",
        "media_content_item_stock_decisions",
        ["project_id"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_content_item_hash",
        "media_content_item_stock_decisions",
        ["content_item_hash"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_decision_hash",
        "media_content_item_stock_decisions",
        ["decision_hash"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_event_hash",
        "media_content_item_stock_decisions",
        ["event_hash"],
    )
    op.create_index(
        "ix_media_content_item_stock_decisions_created_at",
        "media_content_item_stock_decisions",
        ["created_at"],
    )
    op.create_index(
        "ix_media_content_item_stock_owner_project",
        "media_content_item_stock_decisions",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "ix_media_content_item_stock_item_created",
        "media_content_item_stock_decisions",
        ["content_item_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_content_item_stock_item_created",
        table_name="media_content_item_stock_decisions",
    )
    op.drop_index(
        "ix_media_content_item_stock_owner_project",
        table_name="media_content_item_stock_decisions",
    )
    for name in (
        "ix_media_content_item_stock_decisions_created_at",
        "ix_media_content_item_stock_decisions_event_hash",
        "ix_media_content_item_stock_decisions_decision_hash",
        "ix_media_content_item_stock_decisions_content_item_hash",
        "ix_media_content_item_stock_decisions_project_id",
        "ix_media_content_item_stock_decisions_owner_user_id",
        "ix_media_content_item_stock_decisions_content_item_id",
    ):
        op.drop_index(name, table_name="media_content_item_stock_decisions")
    op.drop_table("media_content_item_stock_decisions")
