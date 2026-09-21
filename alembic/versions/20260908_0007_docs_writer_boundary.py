"""Make the Docs guard Agent-local while retaining revision/index triggers.

Revision ID: 20260908_0007
Revises: 20260908_0006

``20260908_0005`` used ``docs_write_guard`` as a fail-fast global writer
boundary.  That made an ordinary Task/Project/User/Docs transaction fail just
because an Agent transaction happened to hold the advisory lock.  Agent
mutations now acquire the advisory lock themselves and prelock their complete
affected row set with ``NOWAIT``; ordinary writers must remain independent.

The guard function and trigger names are retained as a compatibility surface,
but the function is deliberately a no-op.  The ``docs_record_change`` and
``docs_record_truncate`` triggers are not changed: every canonical/domain
writer still advances the library/policy revision and coalesced index queue.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260908_0007"
down_revision = "20260908_0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = bind.dialect.identifier_preparer.quote(schema)
    # Preserve the trigger for installations that already applied 0005, but
    # remove its fail-fast behavior.  Returning NULL from a BEFORE statement
    # trigger leaves the original statement untouched.
    op.execute(sa.text(f"""
CREATE OR REPLACE FUNCTION {quoted}.docs_write_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
BEGIN
    RETURN NULL;
END $$;
"""))


def downgrade():
    # Restore the 0005 behavior when an operator deliberately downgrades the
    # schema and application together.  The safety boundary of this migration
    # is the upgrade path; the pre-0007 contract intentionally used the
    # fail-fast SQLSTATE to avoid row-lock/advisory-lock cycles.
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = bind.dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f"""
CREATE OR REPLACE FUNCTION {quoted}.docs_write_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO {quoted}, pg_catalog AS $$
BEGIN
    IF NOT pg_try_advisory_xact_lock(1146045267, 1) THEN
      RAISE EXCEPTION 'Docs writer lock is busy; retry the transaction'
        USING ERRCODE = '40001', HINT = 'Retry the complete Docs write transaction';
    END IF;
    RETURN NULL;
END $$;
"""))
