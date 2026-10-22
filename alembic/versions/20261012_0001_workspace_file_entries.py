"""Canonical User/Project Files inventory for the workspace_file media index."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20261012_0001"
down_revision = "20261011_0002"
branch_labels = None
depends_on = None

TABLE = "workspace_file_entries"


def _is_sqlite() -> bool:
    try:
        return str(op.get_bind().dialect.name).lower() == "sqlite"
    except Exception:
        return False


def _uuid_type() -> sa.types.TypeEngine:
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


def _table_exists(name: str) -> bool:
    try:
        return bool(inspect(op.get_bind()).has_table(name))
    except Exception:
        return False


def _indexes(name: str) -> set[str]:
    try:
        return {str(item["name"]) for item in inspect(op.get_bind()).get_indexes(name)}
    except Exception:
        return set()


def upgrade() -> None:
    if not _table_exists(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", _uuid_type(), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("owner_id", _uuid_type(), nullable=False),
            sa.Column("rel_path", sa.Text(), nullable=False),
            sa.Column("name", sa.String(length=512), nullable=False),
            sa.Column("ext", sa.String(length=64), nullable=False, server_default=sa.text("''")),
            sa.Column("mime", sa.String(length=255), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("mtime_ns", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending_hash'")),
            sa.Column("excluded_reason", sa.String(length=128), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_scanned_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint("scope IN ('user','project')", name="ck_workspace_file_entries_scope"),
            sa.CheckConstraint(
                "status IN ('pending_hash','indexable','excluded','unsupported','deleted')",
                name="ck_workspace_file_entries_status",
            ),
        )
    existing = _indexes(TABLE)
    if "uq_workspace_file_entries_live_path" not in existing:
        op.create_index(
            "uq_workspace_file_entries_live_path",
            TABLE,
            ["scope", "owner_id", "rel_path"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )
    if "ix_workspace_file_entries_owner_status" not in existing:
        op.create_index("ix_workspace_file_entries_owner_status", TABLE, ["scope", "owner_id", "status"])
    if "ix_workspace_file_entries_content_hash" not in existing:
        op.create_index("ix_workspace_file_entries_content_hash", TABLE, ["scope", "owner_id", "content_hash"])


def downgrade() -> None:
    if _table_exists(TABLE):
        for name in ("ix_workspace_file_entries_content_hash", "ix_workspace_file_entries_owner_status",
                     "uq_workspace_file_entries_live_path"):
            if name in _indexes(TABLE):
                op.drop_index(name, table_name=TABLE)
        op.drop_table(TABLE)
