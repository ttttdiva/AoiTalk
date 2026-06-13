"""Unify legacy Default spaces with Inbox.

Revision ID: 20260502_0020
Revises: 20260501_0019
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op


revision = "20260502_0020"
down_revision = "20260501_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename legacy FastAPI-created Default spaces when no Inbox exists yet.
    op.execute(
        """
        UPDATE spaces s
        SET name = 'Inbox',
            slug = 'inbox-' || s.owner_id::text,
            description = COALESCE(s.description, '未整理のタスクを一時的に置く場所'),
            color = COALESCE(s.color, '#6b7280'),
            sort_order = LEAST(COALESCE(s.sort_order, 0), 0),
            updated_at = NOW()
        WHERE s.slug = 'default-' || s.owner_id::text
          AND NOT EXISTS (
              SELECT 1
              FROM spaces i
              WHERE i.owner_id = s.owner_id
                AND i.slug = 'inbox-' || s.owner_id::text
          )
        """
    )

    # For users that already have both Inbox and legacy Default, move projects.
    op.execute(
        """
        UPDATE projects p
        SET space_id = i.id,
            updated_at = NOW()
        FROM spaces d
        JOIN spaces i
          ON i.owner_id = d.owner_id
         AND i.slug = 'inbox-' || d.owner_id::text
        WHERE d.slug = 'default-' || d.owner_id::text
          AND p.space_id = d.id
        """
    )

    # Merge duplicate same-name tags before moving legacy tags to Inbox.
    op.execute(
        """
        WITH pairs AS (
            SELECT lt.id AS legacy_tag_id, it.id AS inbox_tag_id
            FROM tags lt
            JOIN spaces d ON d.id = lt.space_id
            JOIN spaces i
              ON i.owner_id = d.owner_id
             AND i.slug = 'inbox-' || d.owner_id::text
            JOIN tags it
              ON it.space_id = i.id
             AND it.name = lt.name
            WHERE d.slug = 'default-' || d.owner_id::text
        )
        DELETE FROM task_tags tt
        USING pairs p
        WHERE tt.tag_id = p.legacy_tag_id
          AND EXISTS (
              SELECT 1
              FROM task_tags existing
              WHERE existing.task_id = tt.task_id
                AND existing.tag_id = p.inbox_tag_id
          )
        """
    )
    op.execute(
        """
        WITH pairs AS (
            SELECT lt.id AS legacy_tag_id, it.id AS inbox_tag_id
            FROM tags lt
            JOIN spaces d ON d.id = lt.space_id
            JOIN spaces i
              ON i.owner_id = d.owner_id
             AND i.slug = 'inbox-' || d.owner_id::text
            JOIN tags it
              ON it.space_id = i.id
             AND it.name = lt.name
            WHERE d.slug = 'default-' || d.owner_id::text
        )
        UPDATE task_tags tt
        SET tag_id = p.inbox_tag_id
        FROM pairs p
        WHERE tt.tag_id = p.legacy_tag_id
        """
    )
    op.execute(
        """
        DELETE FROM tags t
        USING spaces d, spaces i
        WHERE t.space_id = d.id
          AND d.slug = 'default-' || d.owner_id::text
          AND i.owner_id = d.owner_id
          AND i.slug = 'inbox-' || d.owner_id::text
          AND EXISTS (
              SELECT 1
              FROM tags it
              WHERE it.space_id = i.id
                AND it.name = t.name
          )
        """
    )
    op.execute(
        """
        UPDATE tags t
        SET space_id = i.id
        FROM spaces d
        JOIN spaces i
          ON i.owner_id = d.owner_id
         AND i.slug = 'inbox-' || d.owner_id::text
        WHERE d.slug = 'default-' || d.owner_id::text
          AND t.space_id = d.id
        """
    )

    # Remove legacy Default spaces once no data references them.
    op.execute(
        """
        DELETE FROM spaces d
        WHERE d.slug = 'default-' || d.owner_id::text
          AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.space_id = d.id)
          AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.space_id = d.id)
        """
    )

    # Ensure an Inbox project exists for every Inbox space.
    op.execute(
        """
        UPDATE projects p
        SET name = 'Inbox',
            description = COALESCE(p.description, '未整理のタスクを一時的に置く場所'),
            space_id = i.id,
            deleted_at = NULL,
            project_metadata = (
                COALESCE(p.project_metadata::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'aliases', jsonb_build_array('inbox'),
                    'color', '#6b7280',
                    'isInboxDefault', true
                )
            )::json,
            updated_at = NOW()
        FROM spaces i
        WHERE i.slug = 'inbox-' || i.owner_id::text
          AND p.slug = 'inbox-project-' || i.owner_id::text
        """
    )
    op.execute(
        """
        INSERT INTO projects (
            id,
            name,
            slug,
            description,
            owner_id,
            space_id,
            project_metadata,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            'Inbox',
            'inbox-project-' || i.owner_id::text,
            '未整理のタスクを一時的に置く場所',
            i.owner_id,
            i.id,
            json_build_object(
                'aliases', json_build_array('inbox'),
                'color', '#6b7280',
                'isInboxDefault', true
            ),
            NOW(),
            NOW()
        FROM spaces i
        WHERE i.slug = 'inbox-' || i.owner_id::text
          AND NOT EXISTS (
              SELECT 1
              FROM projects p
              WHERE p.slug = 'inbox-project-' || i.owner_id::text
          )
        """
    )

    # Ensure the owner can access the Inbox project.
    op.execute(
        """
        INSERT INTO project_members (
            id,
            project_id,
            user_id,
            role,
            permissions,
            joined_at
        )
        SELECT
            gen_random_uuid(),
            p.id,
            p.owner_id,
            'admin',
            NULL,
            NOW()
        FROM projects p
        WHERE p.slug = 'inbox-project-' || p.owner_id::text
          AND NOT EXISTS (
              SELECT 1
              FROM project_members pm
              WHERE pm.project_id = p.id
                AND pm.user_id = p.owner_id
          )
        """
    )


def downgrade() -> None:
    # The legacy Default naming was a bug; do not recreate it on downgrade.
    pass
