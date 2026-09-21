"""Allow low-value Knowledge Capture candidates to terminalize silently."""

from alembic import op


revision = "20260909_0002"
down_revision = "20260909_0001"
branch_labels = None
depends_on = None


_WITH_DISCARDED = (
    "status IN ('queued', 'pending', 'researching', 'needs_user', "
    "'draft_ready', 'approved', 'published', 'dismissed', 'discarded', "
    "'superseded', 'retry_wait', 'failed')"
)
_WITHOUT_DISCARDED = (
    "status IN ('queued', 'pending', 'researching', 'needs_user', "
    "'draft_ready', 'approved', 'published', 'dismissed', 'superseded', "
    "'retry_wait', 'failed')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_capture_candidates_status",
        "knowledge_capture_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_capture_candidates_status",
        "knowledge_capture_candidates",
        _WITH_DISCARDED,
    )


def downgrade() -> None:
    # Preserve rows while restoring the older check constraint.  Discarded is
    # the silent terminal equivalent of dismissed in the pre-0002 schema.
    op.execute(
        "UPDATE knowledge_capture_candidates SET status = 'dismissed' "
        "WHERE status = 'discarded'"
    )
    op.drop_constraint(
        "ck_knowledge_capture_candidates_status",
        "knowledge_capture_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_capture_candidates_status",
        "knowledge_capture_candidates",
        _WITHOUT_DISCARDED,
    )
