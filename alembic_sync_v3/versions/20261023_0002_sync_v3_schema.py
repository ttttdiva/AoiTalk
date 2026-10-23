"""Install the optional Sync v3 schema and receipt retirement fields.

This revision belongs to the dedicated Sync v3 Alembic tree.  It deliberately
has no parent in the standard application migration graph, and its version
row is stored in ``sync_v3_alembic_version``.  The regular startup upgrade can
therefore never install this optional schema.
"""

from alembic import op

from src.sync_v3 import contract, receipts
from src.sync_v3.schema_readiness import (
    SYNC_V3_MIGRATION_REVISION,
    inspect_connection,
    migration_preflight,
    migration_receipt_hardening_sql,
)
from src.sync_v3.schema_sql import build_install_sql


revision = SYNC_V3_MIGRATION_REVISION
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    # Every precondition runs before the first DDL statement.  In particular,
    # legacy receipt rows are never assigned guessed device_id/op_seq values.
    migration_preflight(connection)

    # build_install_sql owns the established changefeed/entity-version,
    # dependency helper, row trigger, and commit-order contract.  Receipt DDL is
    # deliberately appended here because build_install_sql alone does not cover
    # sync_op_receipts, device floors, or their retirement index.
    op.execute(build_install_sql())
    op.execute(contract.SYNC_OP_RECEIPTS_DDL)
    op.execute(receipts.RECEIPT_RETIREMENT_DDL)
    op.execute(migration_receipt_hardening_sql())

    # Fail the transaction if an object was omitted or has an unexpected shape.
    # The dedicated revision is stamped only after this function returns.
    report = inspect_connection(connection, check_key=False, require_revision=False)
    if report.missing or report.wrong:
        details = ", ".join((*report.missing, *report.wrong))
        raise RuntimeError(f"sync-v3 schema verification failed: {details}")


def downgrade() -> None:
    raise RuntimeError(
        "sync-v3 downgrade is refused; stop v3, return to the known server, "
        "and retain changefeed/receipts/business data for recovery"
    )
