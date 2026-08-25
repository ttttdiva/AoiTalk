"""Create the canonical durable ClipIngest receipt ledger."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260824_0001"
down_revision = "20260823_0006"
branch_labels = None
depends_on = None


_TABLE = "docs_clip_ingest_receipts"
_LEGACY_TABLE = "clip_ingest_receipts"


def upgrade() -> None:
    """Create the receipt ledger without copying plaintext or legacy rows."""

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("docs_library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        # These three JSON/text columns contain field-crypto ciphertext when
        # written by the ORM.  The JSON defaults are only for an empty row.
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
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
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "action IN ('create', 'append', 'duplicate_skip')",
            name="ck_docs_clip_ingest_receipts_action",
        ),
        sa.ForeignKeyConstraint(
            ["docs_library_id"],
            ["docs_libraries.id"],
            name="fk_docs_clip_ingest_receipts_library",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_node_id"],
            ["knowledge_nodes.id"],
            name="fk_docs_clip_ingest_receipts_topic_node",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["knowledge_nodes.id"],
            name="fk_docs_clip_ingest_receipts_target_node",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_docs_clip_ingest_receipts_actor_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_docs_clip_ingest_receipts_docs_library_id", ["docs_library_id"]),
        ("ix_docs_clip_ingest_receipts_topic_node_id", ["topic_node_id"]),
        ("ix_docs_clip_ingest_receipts_target_node_id", ["target_node_id"]),
        ("ix_docs_clip_ingest_receipts_actor_user_id", ["actor_user_id"]),
        ("ix_docs_clip_ingest_receipts_source_sha256", ["source_sha256"]),
        ("ix_docs_clip_ingest_receipts_created_at", ["created_at"]),
    ):
        op.create_index(name, _TABLE, columns, unique=False)


def downgrade() -> None:
    """Drop the canonical ledger and the empty pre-contract ledger if present."""

    # The checkout may contain the earlier review version of this migration,
    # which created ``clip_ingest_receipts``.  That table is known to be empty
    # and must not survive the parent's canonical downgrade/upgrade cycle.
    op.execute(
        sa.text(
            f"DROP TABLE IF EXISTS {_TABLE} CASCADE;"
            f" DROP TABLE IF EXISTS {_LEGACY_TABLE} CASCADE;"
        )
    )
