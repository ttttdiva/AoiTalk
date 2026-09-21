"""Create the immutable MediaOps research-candidate decision ledger.

``media_research_candidates.status`` is a mutable projection used for fast
listing.  This migration adds the append-only evidence ledger used to explain
each review, expiry, and promotion transition.  The ledger deliberately keeps
the candidate snapshot separate from the safe API projection and installs
database-level update/delete guards for both PostgreSQL and SQLite fixtures.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0022"
down_revision = "20260902_0021"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # Existing candidate rows are a mutable projection and have no prior
    # review authority.  Start them at version zero; promotion will require a
    # fresh explicit human decision rather than inferring legacy status.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    candidate_columns = {
        column["name"]
        for column in inspector.get_columns("media_research_candidates")
    }
    if "decision_version" not in candidate_columns:
        decision_column = sa.Column(
            "decision_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        if bind.dialect.name == "sqlite":
            # SQLite cannot ALTER a table to add a standalone CHECK
            # constraint; batch recreation keeps legacy rows transactional.
            with op.batch_alter_table(
                "media_research_candidates",
                recreate="always",
            ) as batch:
                batch.add_column(decision_column)
                batch.create_check_constraint(
                    "ck_media_research_candidates_decision_version",
                    "decision_version >= 0",
                )
        else:
            op.add_column("media_research_candidates", decision_column)
            op.create_check_constraint(
                "ck_media_research_candidates_decision_version",
                "media_research_candidates",
                "decision_version >= 0",
            )
        op.create_index(
            "ix_media_research_candidates_decision_version",
            "media_research_candidates",
            ["decision_version"],
        )

    op.create_table(
        "media_research_candidate_decisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "candidate_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_id", _uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False, server_default=sa.text("'system'")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("prev_event_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("content_item_id", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('triage', 'accept', 'reject', 'expire', 'promote')",
            name="ck_media_research_candidate_decisions_event_type",
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('discovered', 'triaged', 'accepted')",
            name="ck_media_research_candidate_decisions_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')",
            name="ck_media_research_candidate_decisions_to_status",
        ),
        sa.CheckConstraint(
            "actor_type IN ('human', 'agent', 'system', 'admin', 'unknown')",
            name="ck_media_research_candidate_decisions_actor_type",
        ),
        sa.CheckConstraint(
            "event_type NOT IN ('accept', 'reject') OR (length(trim(coalesce(reason, ''))) > 0 AND actor_type IN ('human', 'admin'))",
            name="ck_media_research_candidate_decisions_reason",
        ),
        sa.CheckConstraint(
            "event_type <> 'promote' OR content_item_id IS NOT NULL",
            name="ck_media_research_candidate_decisions_promotion_content",
        ),
        sa.CheckConstraint(
            "(event_type = 'triage' AND to_status = 'triaged') OR "
            "(event_type = 'accept' AND to_status = 'accepted') OR "
            "(event_type = 'reject' AND to_status = 'rejected') OR "
            "(event_type = 'expire' AND to_status = 'expired') OR "
            "(event_type = 'promote' AND to_status = 'promoted')",
            name="ck_media_research_candidate_decisions_event_target",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_media_research_candidate_decisions_sequence",
        ),
        sa.CheckConstraint(
            "length(candidate_hash) = 64",
            name="ck_media_research_candidate_decisions_candidate_hash",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_media_research_candidate_decisions_request_hash",
        ),
        sa.CheckConstraint(
            "length(decision_hash) = 64",
            name="ck_media_research_candidate_decisions_decision_hash",
        ),
        sa.CheckConstraint(
            "prev_event_hash IS NULL OR length(prev_event_hash) = 64",
            name="ck_media_research_candidate_decisions_prev_event_hash",
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64",
            name="ck_media_research_candidate_decisions_event_hash",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["media_research_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["media_content_items.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "sequence",
            name="uq_media_research_candidate_decisions_sequence",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "idempotency_key",
            name="uq_media_research_candidate_decisions_idempotency",
        ),
    )
    for name, columns in {
        "ix_media_research_candidate_decisions_candidate_id": ["candidate_id"],
        "ix_media_research_candidate_decisions_owner_user_id": ["owner_user_id"],
        "ix_media_research_candidate_decisions_project_id": ["project_id"],
        "ix_media_research_candidate_decisions_candidate_created": [
            "candidate_id",
            "created_at",
        ],
        "ix_media_research_candidate_decisions_event_type": ["event_type"],
        "ix_media_research_candidate_decisions_candidate_hash": ["candidate_hash"],
        "ix_media_research_candidate_decisions_decision_hash": ["decision_hash"],
        "ix_media_research_candidate_decisions_event_hash": ["event_hash"],
        "ix_media_research_candidate_decisions_content_item_id": ["content_item_id"],
        "ix_media_research_candidate_decisions_created_at": ["created_at"],
        "ix_media_research_candidate_decisions_owner_project": [
            "owner_user_id",
            "project_id",
        ],
    }.items():
        op.create_index(name, "media_research_candidate_decisions", columns)

    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION media_research_candidate_decisions_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'media research candidate decisions are immutable';
            END;
            $$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_research_candidate_decisions_no_update
            BEFORE UPDATE OR DELETE ON media_research_candidate_decisions
            FOR EACH ROW EXECUTE FUNCTION media_research_candidate_decisions_immutable();
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_research_candidate_decisions_no_truncate
            BEFORE TRUNCATE ON media_research_candidate_decisions
            FOR EACH STATEMENT EXECUTE FUNCTION media_research_candidate_decisions_immutable();
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER media_research_candidate_decisions_no_update
            BEFORE UPDATE ON media_research_candidate_decisions
            BEGIN
              SELECT RAISE(ABORT, 'media research candidate decisions are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_research_candidate_decisions_no_delete
            BEFORE DELETE ON media_research_candidate_decisions
            BEGIN
              SELECT RAISE(ABORT, 'media research candidate decisions are immutable');
            END;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS media_research_candidate_decisions_no_update "
            "ON media_research_candidate_decisions"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS media_research_candidate_decisions_no_truncate "
            "ON media_research_candidate_decisions"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS media_research_candidate_decisions_immutable()"
        )
    elif dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS media_research_candidate_decisions_no_update"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS media_research_candidate_decisions_no_delete"
        )
    op.drop_table("media_research_candidate_decisions")
    bind = op.get_bind()
    candidate_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("media_research_candidates")
    }
    if "decision_version" in candidate_columns:
        op.drop_index(
            "ix_media_research_candidates_decision_version",
            table_name="media_research_candidates",
        )
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(
                "media_research_candidates",
                recreate="always",
            ) as batch:
                batch.drop_constraint(
                    "ck_media_research_candidates_decision_version",
                    type_="check",
                )
                batch.drop_column("decision_version")
        else:
            op.drop_constraint(
                "ck_media_research_candidates_decision_version",
                "media_research_candidates",
                type_="check",
            )
            op.drop_column("media_research_candidates", "decision_version")
