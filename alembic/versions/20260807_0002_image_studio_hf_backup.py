"""Add Image Studio external backup request state."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260807_0002"
down_revision = "20260807_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_studio_outputs",
        sa.Column(
            "backup_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("backup_destination", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column(
            "backup_include_metadata",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("backup_event_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column(
            "backup_request_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_requested",
        ),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("backup_last_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("backup_requested_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("backup_updated_at", sa.DateTime(), nullable=True),
    )
    op.create_check_constraint(
        "ck_image_studio_outputs_backup_request_status",
        "image_studio_outputs",
        "backup_request_status IN "
        "('not_requested','queued','enqueue_failed','cancel_requested')",
    )
    op.create_index(
        "ix_image_studio_outputs_backup_request_status",
        "image_studio_outputs",
        ["backup_request_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_outputs_backup_request_status",
        table_name="image_studio_outputs",
    )
    op.drop_constraint(
        "ck_image_studio_outputs_backup_request_status",
        "image_studio_outputs",
        type_="check",
    )
    op.drop_column("image_studio_outputs", "backup_updated_at")
    op.drop_column("image_studio_outputs", "backup_requested_at")
    op.drop_column("image_studio_outputs", "backup_last_error")
    op.drop_column("image_studio_outputs", "backup_request_status")
    op.drop_column("image_studio_outputs", "backup_event_id")
    op.drop_column("image_studio_outputs", "backup_include_metadata")
    op.drop_column("image_studio_outputs", "backup_destination")
    op.drop_column("image_studio_outputs", "backup_requested")
