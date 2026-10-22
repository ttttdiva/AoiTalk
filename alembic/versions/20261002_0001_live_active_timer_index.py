"""削除済み工数を稼働中タイマーの一意制約から除外する。"""

from alembic import op
import sqlalchemy as sa

revision = "20261002_0001"
down_revision = "20261001_0001"
branch_labels = None
depends_on = None


def upgrade():
    # Do not rewrite historical starts, stops, or tombstones. The old index
    # already guarantees uniqueness in this narrower subset of live rows.
    op.drop_index("ix_time_entries_active_user", table_name="time_entries")
    op.create_index(
        "ix_time_entries_active_user",
        "time_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL AND deleted_at IS NULL"),
    )


def downgrade():
    # A historical unfinished tombstone may coexist with a new live timer.
    # Refuse a lossy rollback instead of changing user times to fit the old
    # constraint. PostgreSQL migrations run transactionally.
    bind = op.get_bind()
    duplicate = bind.execute(sa.text(
        "SELECT user_id FROM time_entries WHERE ended_at IS NULL "
        "GROUP BY user_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot restore the old active timer index while unfinished "
            "deleted entries coexist; retain this migration or resolve the "
            "affected historical records explicitly first."
        )
    op.drop_index("ix_time_entries_active_user", table_name="time_entries")
    op.create_index(
        "ix_time_entries_active_user",
        "time_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
