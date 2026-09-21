"""Repair the recipe name column used by the Character dashboard.

Some deployments were stamped at the generation migration head after the
recipe table had already been created by an older schema variant.  The model
and dashboard both require the safe, non-secret display name; add it
idempotently without touching existing recipe rows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260902_0019"
down_revision = "20260901_0018"
branch_labels = None
depends_on = None


def _table_columns(bind: sa.Connection, table_name: str) -> set[str]:
    try:
        return {
            str(column["name"])
            for column in sa.inspect(bind).get_columns(table_name)
        }
    except sa.exc.NoSuchTableError:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    columns = _table_columns(bind, "media_creative_recipes")
    if columns and "name" not in columns:
        op.add_column(
            "media_creative_recipes",
            sa.Column(
                "name",
                sa.String(length=255),
                nullable=False,
                server_default="Untitled recipe",
            ),
        )


def downgrade() -> None:
    """Do not undo the canonical recipe ``name`` column.

    This revision repairs installations that may have created the table with
    an older shape.  Once the canonical model and API expose ``name`` there
    is no safe inverse: dropping it would destroy user-authored display data
    and leave a schema that newer code cannot read.  Keep the downgrade
    intentionally forward-compatible/no-op instead of guessing whether the
    column was created by this repair or by an earlier canonical migration.
    """

    return None
