"""Make project storage quota counters non-null and safe by default."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0002"
down_revision = "20260804_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older Next.js project-member creation paths persisted only ``role`` and
    # left ``permissions`` NULL.  The authorization layer now intentionally
    # treats missing/malformed permission JSON as deny-all, so preserve the
    # access those historical rows had by materializing the role defaults
    # before the stricter checks are deployed.  Unknown roles stay NULL and
    # therefore fail closed.
    # ``projects.owner_id`` is the ownership authority. Repair a missing or
    # legacy owner membership first. Existing explicit permissions are kept.
    op.execute(
        sa.text(
            """
            INSERT INTO project_members (
                id, project_id, user_id, role, permissions, joined_at, invited_by
            )
            SELECT
                gen_random_uuid(), projects.id, projects.owner_id, 'owner',
                CAST('{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}' AS JSON),
                NOW(), NULL
            FROM projects
            ON CONFLICT (project_id, user_id) DO UPDATE
            SET role = 'owner',
                permissions = COALESCE(
                    project_members.permissions,
                    EXCLUDED.permissions
                )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE project_members
            SET permissions = CASE LOWER(TRIM(role))
                WHEN 'owner' THEN CAST('{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}' AS JSON)
                WHEN 'admin' THEN CAST('{"read": true, "write": true, "delete": true, "manage_members": true, "manage_settings": true}' AS JSON)
                WHEN 'member' THEN CAST('{"read": true, "write": false, "delete": false, "manage_members": false, "manage_settings": false}' AS JSON)
                WHEN 'viewer' THEN CAST('{"read": true, "write": false, "delete": false, "manage_members": false, "manage_settings": false}' AS JSON)
                ELSE CAST('{}' AS JSON)
            END
            WHERE permissions IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE projects
            SET storage_quota_mb = COALESCE(storage_quota_mb, 1000),
                storage_used_mb = COALESCE(storage_used_mb, 0)
            WHERE storage_quota_mb IS NULL OR storage_used_mb IS NULL
            """
        )
    )
    op.alter_column(
        "projects",
        "storage_quota_mb",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("1000"),
    )
    op.alter_column(
        "projects",
        "storage_used_mb",
        existing_type=sa.Float(),
        nullable=False,
        server_default=sa.text("0"),
    )


def downgrade() -> None:
    op.alter_column(
        "projects",
        "storage_used_mb",
        existing_type=sa.Float(),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "projects",
        "storage_quota_mb",
        existing_type=sa.Integer(),
        nullable=True,
        server_default=None,
    )
