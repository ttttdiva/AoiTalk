"""Extend the Trusted Operations Kernel for MediaOps manual actions.

MediaOps publication/update/delete/release proposals share the existing
ExternalAction, Approval, Attempt and Receipt ledger.  Older EngagementOps
rows retain their original bindings; only the legacy opportunity/draft columns
become nullable and the action-type CHECK is widened.  Media references are
opaque UUID bindings validated by the service so this migration remains safe
for lightweight installations where the optional MediaOps tables are absent.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0010"
down_revision = "20260901_0009"
branch_labels = None
depends_on = None


_TABLE = "external_actions"
_TYPE_CHECK = "ck_external_actions_type"
_ACTION_TYPES = (
    "engagement.submit_application",
    "media.publish_content",
    "media.update_content",
    "media.delete_content",
    "media.release_product",
)
_TYPE_EXPRESSION = "action_type IN ('engagement.submit_application','media.publish_content','media.update_content','media.delete_content','media.release_product')"
_MEDIA_COLUMNS = (
    ("content_item_id", postgresql.UUID(as_uuid=True)),
    ("content_variant_id", postgresql.UUID(as_uuid=True)),
    ("content_variant_revision_id", postgresql.UUID(as_uuid=True)),
    ("persona_revision_id", postgresql.UUID(as_uuid=True)),
    ("platform_account_id", postgresql.UUID(as_uuid=True)),
    ("platform_account_revision_id", postgresql.UUID(as_uuid=True)),
    ("platform", sa.String(length=16)),
)


def _sqlite_batch() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def upgrade() -> None:
    """Widen ExternalAction without rewriting existing EngagementOps rows."""

    if _sqlite_batch():
        # SQLite cannot ALTER a NOT NULL column or named CHECK in place.  The
        # batch recreate preserves all existing rows and indexes while applying
        # the same physical contract as PostgreSQL.
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.alter_column("opportunity_id", nullable=True)
            batch.alter_column("application_draft_id", nullable=True)
            batch.drop_constraint(_TYPE_CHECK, type_="check")
            batch.create_check_constraint(_TYPE_CHECK, _TYPE_EXPRESSION)
            for name, column_type in _MEDIA_COLUMNS:
                batch.add_column(sa.Column(name, column_type, nullable=True))
    else:
        op.alter_column(_TABLE, "opportunity_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        op.alter_column(_TABLE, "application_draft_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
        op.drop_constraint(_TYPE_CHECK, _TABLE, type_="check")
        op.create_check_constraint(_TYPE_CHECK, _TABLE, _TYPE_EXPRESSION)
        for name, column_type in _MEDIA_COLUMNS:
            op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))

    for name, _column_type in _MEDIA_COLUMNS:
        index_name = f"ix_external_actions_{name}"
        # Batch recreation may carry indexes from the old table, so tolerate a
        # pre-existing index when a deployment partially applied this revision.
        try:
            op.create_index(index_name, _TABLE, [name])
        except Exception as exc:  # pragma: no cover - defensive deployment path
            if "already exists" not in str(exc).lower():
                raise


def downgrade() -> None:
    """Refuse to discard MediaOps rows; restore only an empty extension."""

    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM external_actions "
            "WHERE action_type IN "
            "('media.publish_content','media.update_content','media.delete_content','media.release_product') "
            "LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError("cannot downgrade MediaOps Operations extension while media actions exist")

    for name, _column_type in reversed(_MEDIA_COLUMNS):
        index_name = f"ix_external_actions_{name}"
        try:
            op.drop_index(index_name, table_name=_TABLE)
        except Exception as exc:  # pragma: no cover - defensive deployment path
            if "does not exist" not in str(exc).lower():
                raise

    if _sqlite_batch():
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            for name, _column_type in reversed(_MEDIA_COLUMNS):
                batch.drop_column(name)
            batch.drop_constraint(_TYPE_CHECK, type_="check")
            batch.create_check_constraint(
                _TYPE_CHECK,
                "action_type = 'engagement.submit_application'",
            )
            batch.alter_column("opportunity_id", nullable=False)
            batch.alter_column("application_draft_id", nullable=False)
    else:
        for name, _column_type in reversed(_MEDIA_COLUMNS):
            op.drop_column(_TABLE, name)
        op.drop_constraint(_TYPE_CHECK, _TABLE, type_="check")
        op.create_check_constraint(
            _TYPE_CHECK,
            _TABLE,
            "action_type = 'engagement.submit_application'",
        )
        op.alter_column(_TABLE, "opportunity_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
        op.alter_column(_TABLE, "application_draft_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
