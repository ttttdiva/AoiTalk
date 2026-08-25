"""Add the content-deletion audit ledger and task deletion batch markers."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0006"
down_revision = "20260823_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the append-only deletion ledger and batch marker columns."""

    op.create_table(
        "content_deletion_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Subject IDs intentionally have no FK: the subject may be purged
        # immediately after this event is appended (and may be a file path).
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=512), nullable=False),
        sa.Column("root_entity_id", sa.String(length=512), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "event_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # Keep audit metadata, but never a content/body column.
        sa.Column(
            "metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.CheckConstraint(
            "action IN ('deleted', 'restored', 'purged', 'permanent_deleted')",
            name="ck_content_deletion_events_action",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_content_deletion_events_project",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_content_deletion_events_actor_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_deletion_events_entity",
        "content_deletion_events",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_deletion_events_root_event_at",
        "content_deletion_events",
        ["root_entity_id", "event_at"],
        unique=False,
    )
    op.create_index(
        "ix_content_deletion_events_batch_id",
        "content_deletion_events",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_deletion_events_project_id",
        "content_deletion_events",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_deletion_events_actor_user_id",
        "content_deletion_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_deletion_events_event_at",
        "content_deletion_events",
        ["event_at"],
        unique=False,
    )

    # Task deletion flows share one UUID across the task tree and its related
    # occurrence/time-entry tombstones.  The ORM columns are added by the
    # task workstream; keeping the DDL here makes this migration linear.
    for table in ("tasks", "task_occurrences", "time_entries"):
        op.add_column(
            table,
            sa.Column(
                "deletion_batch_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_deletion_batch_id",
            table,
            ["deletion_batch_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove batch markers and the deletion ledger."""

    for table in ("time_entries", "task_occurrences", "tasks"):
        op.drop_index(f"ix_{table}_deletion_batch_id", table_name=table)
        op.drop_column(table, "deletion_batch_id")

    for index_name in (
        "ix_content_deletion_events_event_at",
        "ix_content_deletion_events_actor_user_id",
        "ix_content_deletion_events_project_id",
        "ix_content_deletion_events_batch_id",
        "ix_content_deletion_events_root_event_at",
        "ix_content_deletion_events_entity",
    ):
        op.drop_index(index_name, table_name="content_deletion_events")
    op.drop_table("content_deletion_events")
