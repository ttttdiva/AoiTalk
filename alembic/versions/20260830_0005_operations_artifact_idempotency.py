"""Enforce scoped content idempotency for Operations artifacts.

Artifact rows are content-addressed by owner, scope, digest, size, and MIME
type.  The scope split is represented by two PostgreSQL partial unique indexes
so that personal artifacts (``project_id IS NULL``) and project artifacts
cannot accidentally share a nullable-key uniqueness rule.

Existing duplicate groups are never silently reconciled.  The upgrade must
stop for manual review before creating either index when such rows are
present.
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260830_0005"
down_revision = "20260830_0004"
branch_labels = None
depends_on = None


_TABLE = "artifact_versions"
_PERSONAL_INDEX = "uq_artifact_versions_personal_content"
_PROJECT_INDEX = "uq_artifact_versions_project_content"


def _assert_no_content_duplicates(bind: Any) -> None:
    """Fail closed when rows would violate either new unique index.

    Keep the duplicate check explicit per scope.  This makes the migration
    reviewable and ensures a duplicate in one scope cannot be hidden by rows
    in the other scope.  No data mutation is performed here by design.
    """

    duplicate_checks = (
        """
        SELECT owner_user_id, sha256, size_bytes, mime_type
        FROM artifact_versions
        WHERE project_id IS NULL
        GROUP BY owner_user_id, sha256, size_bytes, mime_type
        HAVING count(*) > 1
        LIMIT 1
        """,
        """
        SELECT owner_user_id, project_id, sha256, size_bytes, mime_type
        FROM artifact_versions
        WHERE project_id IS NOT NULL
        GROUP BY owner_user_id, project_id, sha256, size_bytes, mime_type
        HAVING count(*) > 1
        LIMIT 1
        """,
    )
    for statement in duplicate_checks:
        if bind.execute(sa.text(statement)).first() is not None:
            raise RuntimeError(
                "artifact_versions contains duplicate content identity rows; "
                "manual review is required before adding idempotency indexes"
            )


def upgrade() -> None:
    """Add DB-enforced content idempotency after a lossless duplicate audit."""

    _assert_no_content_duplicates(op.get_bind())

    op.create_index(
        _PERSONAL_INDEX,
        _TABLE,
        ["owner_user_id", "sha256", "size_bytes", "mime_type"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        _PROJECT_INDEX,
        _TABLE,
        ["owner_user_id", "project_id", "sha256", "size_bytes", "mime_type"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop only the two scoped artifact idempotency indexes."""

    op.drop_index(_PROJECT_INDEX, table_name=_TABLE)
    op.drop_index(_PERSONAL_INDEX, table_name=_TABLE)
