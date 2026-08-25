"""Persist Enterprise bootstrap state and repair safe project ACL cases."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260807_0001"
down_revision = "20260806_0007"
branch_labels = None
depends_on = None


_OWNER_PERMISSIONS = (
    '{"read": true, "write": true, "delete": true, '
    '"manage_members": true, "manage_settings": true}'
)


def upgrade() -> None:
    op.create_table(
        "enterprise_bootstrap_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "bootstrap_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 1", name="ck_enterprise_bootstrap_singleton"),
        sa.ForeignKeyConstraint(
            ["bootstrap_user_id"],
            ["users.id"],
            name="fk_enterprise_bootstrap_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_enterprise_bootstrap_user_id",
        "enterprise_bootstrap_state",
        ["bootstrap_user_id"],
        unique=False,
    )

    # Ownership is unambiguous even in an already-upgraded database. Repair
    # only that authoritative relationship and preserve explicit ACL JSON.
    op.execute(
        sa.text(
            f"""
            INSERT INTO project_members (
                id, project_id, user_id, role, permissions, joined_at, invited_by
            )
            SELECT
                gen_random_uuid(), projects.id, projects.owner_id, 'owner',
                CAST('{_OWNER_PERMISSIONS}' AS JSON), NOW(), NULL
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

    # NULL is known to be unset. Rows equal to the faulty 0002 defaults are
    # deliberately not rewritten: explicit rows can have the same JSON.
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


def downgrade() -> None:
    op.drop_index(
        "ix_enterprise_bootstrap_user_id",
        table_name="enterprise_bootstrap_state",
    )
    op.drop_table("enterprise_bootstrap_state")
