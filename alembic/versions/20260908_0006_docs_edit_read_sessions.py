"""Durable, sequential Docs edit-read pagination checkpoints.

The edit-read protocol stores only opaque continuation state and revision/
authorization bindings.  Canonical Docs content and model-facing page bodies
remain in their source tables and are rebuilt for delivery/replay.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0006"
down_revision = "20260908_0005"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    quoted = bind.dialect.identifier_preparer.quote(schema)
    op.execute(f"""
CREATE TABLE IF NOT EXISTS {quoted}.docs_edit_read_sessions (
    id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    root_id uuid NOT NULL,
    library_id uuid NOT NULL,
    revision bigint NOT NULL,
    policy_revision bigint NOT NULL,
    scope_binding varchar(64) NOT NULL,
    read_fingerprint varchar(64),
    depth integer NOT NULL,
    page_chars integer NOT NULL,
    turn_project_id uuid,
    next_offset integer NOT NULL DEFAULT 0,
    last_page_start integer,
    last_page_end integer,
    last_cursor varchar(128),
    next_cursor varchar(128),
    has_more boolean NOT NULL DEFAULT false,
    finished boolean NOT NULL DEFAULT false,
    lease_id uuid,
    terminal_status varchar(64),
    expires_at timestamp NOT NULL,
    created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_edit_read_sessions_actor_id ON docs_edit_read_sessions(actor_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_edit_read_sessions_root_id ON docs_edit_read_sessions(root_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_edit_read_sessions_library_id ON docs_edit_read_sessions(library_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_edit_read_sessions_scope_binding ON docs_edit_read_sessions(scope_binding)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_docs_edit_read_sessions_expires_at ON docs_edit_read_sessions(expires_at)")
    # Continuation tokens are generated from a CSPRNG.  A unique index makes
    # accidental token reuse fail closed instead of selecting an arbitrary
    # session.  NULL final tokens are excluded by PostgreSQL's normal UNIQUE
    # semantics and are harmless on other supported dialects.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_docs_edit_read_sessions_next_cursor ON docs_edit_read_sessions(next_cursor)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS docs_edit_read_sessions")
