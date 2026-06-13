"""Finalize tags to space-scope: merge duplicates, drop project_id.

Revision ID: 20260420_0005
Revises: 20260420_0004
Create Date: 2026-04-20 12:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260420_0005"
down_revision = "20260420_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Backfill space_id for any rows where it was missed
    op.execute(
        """
        UPDATE tags
        SET space_id = projects.space_id
        FROM projects
        WHERE tags.project_id = projects.id
          AND tags.space_id IS NULL
          AND projects.space_id IS NOT NULL
        """
    )

    # 2. For tags whose project still has no space, create a private fallback
    # space per owner and assign the tag to it. We avoid deleting anything.
    op.execute(
        """
        WITH needy AS (
            SELECT DISTINCT p.owner_id
            FROM tags t
            JOIN projects p ON p.id = t.project_id
            WHERE t.space_id IS NULL
        ),
        ensured AS (
            INSERT INTO spaces (id, name, slug, description, color, owner_id, sort_order, created_at, updated_at)
            SELECT gen_random_uuid(), 'Default', 'default-' || owner_id::text, NULL, NULL, owner_id, 0, NOW(), NOW()
            FROM needy
            WHERE NOT EXISTS (
                SELECT 1 FROM spaces s WHERE s.owner_id = needy.owner_id AND s.slug = 'default-' || needy.owner_id::text
            )
            RETURNING id, owner_id
        )
        SELECT 1
        """
    )
    op.execute(
        """
        UPDATE tags t
        SET space_id = s.id
        FROM projects p, spaces s
        WHERE t.project_id = p.id
          AND t.space_id IS NULL
          AND s.owner_id = p.owner_id
          AND s.slug = 'default-' || p.owner_id::text
        """
    )

    # 3. Merge duplicates within the same space (keep oldest, redirect task_tags)
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                space_id,
                name,
                FIRST_VALUE(id) OVER (
                    PARTITION BY space_id, name
                    ORDER BY created_at NULLS LAST, id
                ) AS survivor_id,
                ROW_NUMBER() OVER (
                    PARTITION BY space_id, name
                    ORDER BY created_at NULLS LAST, id
                ) AS rn
            FROM tags
            WHERE space_id IS NOT NULL
        ),
        duplicates AS (
            SELECT id AS dup_id, survivor_id FROM ranked WHERE rn > 1
        )
        UPDATE task_tags SET tag_id = duplicates.survivor_id
        FROM duplicates
        WHERE task_tags.tag_id = duplicates.dup_id
          AND NOT EXISTS (
              SELECT 1 FROM task_tags tt2
              WHERE tt2.task_id = task_tags.task_id
                AND tt2.tag_id = duplicates.survivor_id
          )
        """
    )
    # Remove task_tags still pointing to the now-redundant duplicates
    op.execute(
        """
        DELETE FROM task_tags
        WHERE tag_id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY space_id, name
                    ORDER BY created_at NULLS LAST, id
                ) AS rn
                FROM tags WHERE space_id IS NOT NULL
            ) r WHERE rn > 1
        )
        """
    )
    # Delete the duplicate tag rows themselves
    op.execute(
        """
        DELETE FROM tags
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY space_id, name
                    ORDER BY created_at NULLS LAST, id
                ) AS rn
                FROM tags WHERE space_id IS NOT NULL
            ) r WHERE rn > 1
        )
        """
    )

    # 4. Drop project_id dependencies and column
    op.drop_constraint("uq_tags_project_name", "tags", type_="unique")
    op.drop_constraint("tags_project_id_fkey", "tags", type_="foreignkey")
    op.drop_index("ix_tags_project_id", table_name="tags")
    op.drop_column("tags", "project_id")

    # 5. Constrain space_id (NOT NULL + FK + unique)
    op.alter_column("tags", "space_id", nullable=False)
    op.create_foreign_key(
        "fk_tags_space_id_spaces",
        "tags",
        "spaces",
        ["space_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_tags_space_name", "tags", ["space_id", "name"])


def downgrade() -> None:
    # Downgrade is not fully reversible (tag→project mapping is lost once the
    # project_id column is dropped). Restore column as nullable and repopulate
    # from task_tags where possible.
    op.drop_constraint("uq_tags_space_name", "tags", type_="unique")
    op.drop_constraint("fk_tags_space_id_spaces", "tags", type_="foreignkey")
    op.alter_column("tags", "space_id", nullable=True)

    op.add_column(
        "tags",
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE tags SET project_id = sub.project_id
        FROM (
            SELECT tt.tag_id, MIN(tk.project_id) AS project_id
            FROM task_tags tt
            JOIN tasks tk ON tk.id = tt.task_id
            GROUP BY tt.tag_id
        ) sub
        WHERE tags.id = sub.tag_id
        """
    )
    op.create_index("ix_tags_project_id", "tags", ["project_id"])
    op.create_foreign_key(
        "tags_project_id_fkey",
        "tags",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_tags_project_name", "tags", ["project_id", "name"])
