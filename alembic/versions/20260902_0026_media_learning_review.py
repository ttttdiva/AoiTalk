"""Add durable Learning review diffs and Persona application receipts.

The original WS8 proposal row intentionally stopped at a pending evidence
artifact.  WS07 extends that same row with bounded semantic diff fields and an
append-only review history; no managed-memory or provider tables are involved.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0026"
down_revision = "20260902_0025"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def _add_columns() -> None:
    columns = [
        sa.Column("human_decision_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("target_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("proposed_before", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("proposed_after", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("expected_persona_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expected_persona_revision_version", sa.Integer(), nullable=True),
        sa.Column("expected_persona_revision_hash", sa.String(length=64), nullable=True),
        sa.Column("review_history", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("applied_persona_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_persona_revision_version", sa.Integer(), nullable=True),
        sa.Column("applied_persona_revision_hash", sa.String(length=64), nullable=True),
    ]
    if _is_sqlite():
        with op.batch_alter_table("media_learning_proposals", recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
    else:
        for column in columns:
            op.add_column("media_learning_proposals", column)


def _drop_columns() -> None:
    names = [
        "applied_persona_revision_hash",
        "applied_persona_revision_version",
        "applied_persona_revision_id",
        "review_history",
        "expected_persona_revision_hash",
        "expected_persona_revision_version",
        "expected_persona_revision_id",
        "proposed_after",
        "proposed_before",
        "target_fields",
        "human_decision_refs",
    ]
    if _is_sqlite():
        with op.batch_alter_table("media_learning_proposals", recreate="always") as batch:
            for name in names:
                batch.drop_column(name)
    else:
        for name in names:
            op.drop_column("media_learning_proposals", name)


def upgrade() -> None:
    _add_columns()
    op.create_index("ix_media_learning_proposals_expected_persona_revision_id", "media_learning_proposals", ["expected_persona_revision_id"])
    op.create_index("ix_media_learning_proposals_expected_persona_revision_hash", "media_learning_proposals", ["expected_persona_revision_hash"])
    op.create_index("ix_media_learning_proposals_applied_persona_revision_id", "media_learning_proposals", ["applied_persona_revision_id"])
    op.create_index("ix_media_learning_proposals_applied_persona_revision_hash", "media_learning_proposals", ["applied_persona_revision_hash"])


def downgrade() -> None:
    for name in (
        "ix_media_learning_proposals_applied_persona_revision_hash",
        "ix_media_learning_proposals_applied_persona_revision_id",
        "ix_media_learning_proposals_expected_persona_revision_hash",
        "ix_media_learning_proposals_expected_persona_revision_id",
    ):
        op.drop_index(name, table_name="media_learning_proposals")
    _drop_columns()
