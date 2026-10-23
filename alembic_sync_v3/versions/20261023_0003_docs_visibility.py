"""Emit inherited Docs visibility changes without changing the wire contract."""
from alembic import op

from src.sync_v3.docs_visibility_sql import build_docs_visibility_sql
from src.sync_v3.schema_readiness import inspect_connection, migration_preflight

revision = "20261023_0003"
down_revision = "20261023_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    migration_preflight(connection)
    op.execute(build_docs_visibility_sql())
    report = inspect_connection(connection, include_docs_visibility=True)
    if report.missing or report.wrong:
        raise RuntimeError("sync-v3 visibility verification failed: " + ", ".join((*report.missing, *report.wrong)))


def downgrade() -> None:
    raise RuntimeError("sync-v3 downgrade is refused; preserve changefeed and business data")
