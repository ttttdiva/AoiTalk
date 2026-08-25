"""Harden Docs candidate provenance, ownership, and retry idempotency."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0004"
down_revision = "20260823_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "docs_candidates",
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
    )

    # Existing rows predate the retry key.  Include the row id as a stable
    # tie-breaker so duplicate historical proposals can be migrated without
    # violating the new partial unique index.
    op.execute(
        sa.text(
            """
            UPDATE docs_candidates
            SET dedupe_key = md5(
                project_id::text || ':' ||
                coalesce(content_json::text, '') || ':' || id::text
            )
            WHERE dedupe_key IS NULL
            """
        )
    )

    # Candidate ownership is a real User FK.  Historical rows created before
    # that invariant inherit the owning Project actor; a remaining null is a
    # migration failure rather than an anonymously writable candidate.
    op.execute(
        sa.text(
            """
            UPDATE docs_candidates AS candidate
            SET created_by = project.owner_id
            FROM projects AS project
            WHERE candidate.project_id = project.id
              AND candidate.created_by IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM docs_candidates WHERE created_by IS NULL
                ) THEN
                    RAISE EXCEPTION 'docs_candidates.created_by backfill incomplete';
                END IF;
            END $$;
            """
        )
    )
    op.alter_column(
        "docs_candidates",
        "created_by",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # Replace the nullable SET NULL ownership FK inherited from 0003 with a
    # restrictive FK that preserves the non-null invariant.
    # 0003 used PostgreSQL's generated FK name; keep this idempotent for
    # deployments that already applied an earlier 0004 draft.
    op.execute(
        sa.text(
            "ALTER TABLE docs_candidates "
            "DROP CONSTRAINT IF EXISTS docs_candidates_created_by_fkey"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE docs_candidates "
            "DROP CONSTRAINT IF EXISTS fk_docs_candidates_created_by_users"
        )
    )
    op.create_foreign_key(
        "fk_docs_candidates_created_by_users",
        "docs_candidates",
        "users",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_docs_candidates_active_dedupe",
        "docs_candidates",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'proposed' AND dedupe_key IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "status = 'proposed' AND dedupe_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_docs_candidates_active_dedupe",
        table_name="docs_candidates",
    )
    op.execute(
        sa.text(
            "ALTER TABLE docs_candidates "
            "DROP CONSTRAINT IF EXISTS fk_docs_candidates_created_by_users"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE docs_candidates "
            "DROP CONSTRAINT IF EXISTS docs_candidates_created_by_fkey"
        )
    )
    op.create_foreign_key(
        "docs_candidates_created_by_fkey",
        "docs_candidates",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "docs_candidates",
        "created_by",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_column("docs_candidates", "dedupe_key")
