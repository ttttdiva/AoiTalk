"""Add durable Project-global Image Studio event cursors.

Revision ID: 20260807_0008
Revises: 20260807_0007
"""

import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260807_0008"
down_revision = "20260807_0007"
branch_labels = None
depends_on = None


def _purge_existing_image_studio_rows() -> None:
    """Re-purge Studio data before rebuilding event cursor invariants."""
    path = Path(__file__).with_name("20260807_0004_runtime_purge.py")
    spec = importlib.util.spec_from_file_location("_aoi_image_studio_runtime_purge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Image Studio purge helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.purge_existing_rows()


def upgrade() -> None:
    _purge_existing_image_studio_rows()
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "image_studio_project_event_cursors",
        sa.Column("project_id", uuid_type, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("last_cursor >= 0", name="ck_image_studio_project_event_cursor"),
    )
    op.add_column("image_studio_run_events", sa.Column("project_id", uuid_type, nullable=True))
    op.add_column("image_studio_run_events", sa.Column("project_cursor", sa.BigInteger(), nullable=True))
    op.execute(sa.text("""
        WITH ranked AS (
          SELECT e.id, r.project_id,
                 row_number() OVER (PARTITION BY r.project_id ORDER BY e.created_at, e.id) AS cursor
          FROM image_studio_run_events e JOIN image_studio_runs r ON r.id = e.run_id
        )
        UPDATE image_studio_run_events e
        SET project_id = ranked.project_id, project_cursor = ranked.cursor
        FROM ranked WHERE ranked.id = e.id
    """))
    op.execute(sa.text("""
        INSERT INTO image_studio_project_event_cursors(project_id, last_cursor)
        SELECT project_id, max(project_cursor) FROM image_studio_run_events GROUP BY project_id
    """))
    op.alter_column("image_studio_run_events", "project_id", nullable=False)
    op.alter_column("image_studio_run_events", "project_cursor", nullable=False)
    op.create_foreign_key("fk_image_studio_run_events_project", "image_studio_run_events", "projects", ["project_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("uq_image_studio_run_events_project_cursor", "image_studio_run_events", ["project_id", "project_cursor"])
    op.create_check_constraint("ck_image_studio_run_events_project_cursor", "image_studio_run_events", "project_cursor >= 1")
    op.create_index("ix_image_studio_run_events_project_id", "image_studio_run_events", ["project_id"])
    op.create_index("ix_image_studio_run_events_project_cursor", "image_studio_run_events", ["project_id", "project_cursor"])
    op.drop_constraint("uq_image_studio_runs_batch_logical_job", "image_studio_runs", type_="unique")
    op.create_unique_constraint("uq_image_studio_runs_batch_logical_job", "image_studio_runs", ["batch_id", "logical_job_key", "attempt_no"])


def downgrade() -> None:
    op.drop_constraint("uq_image_studio_runs_batch_logical_job", "image_studio_runs", type_="unique")
    op.create_unique_constraint("uq_image_studio_runs_batch_logical_job", "image_studio_runs", ["batch_id", "logical_job_key"])
    op.drop_index("ix_image_studio_run_events_project_cursor", table_name="image_studio_run_events")
    op.drop_index("ix_image_studio_run_events_project_id", table_name="image_studio_run_events")
    op.drop_constraint("ck_image_studio_run_events_project_cursor", "image_studio_run_events", type_="check")
    op.drop_constraint("uq_image_studio_run_events_project_cursor", "image_studio_run_events", type_="unique")
    op.drop_constraint("fk_image_studio_run_events_project", "image_studio_run_events", type_="foreignkey")
    op.drop_column("image_studio_run_events", "project_cursor")
    op.drop_column("image_studio_run_events", "project_id")
    op.drop_table("image_studio_project_event_cursors")
