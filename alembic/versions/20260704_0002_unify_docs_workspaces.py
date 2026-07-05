"""Unify per-user Docs workspaces.

Revision ID: 20260704_0002
Revises: 20260704_0001
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_0002"
down_revision: Union[str, None] = "20260704_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    checks = inspector.get_check_constraints(table_name)
    uniques = inspector.get_unique_constraints(table_name)
    fks = inspector.get_foreign_keys(table_name)
    return any(item.get("name") == constraint_name for item in [*checks, *uniques, *fks])


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                owner_rec RECORD;
                source_ws RECORD;
                source_tag RECORD;
                source_field RECORD;
                canonical_workspace_id uuid;
                target_tag_id uuid;
                target_field_id uuid;
            BEGIN
                FOR owner_rec IN
                    SELECT owner_user_id
                    FROM knowledge_workspaces
                    WHERE owner_user_id IS NOT NULL
                    GROUP BY owner_user_id
                    HAVING count(*) > 1
                LOOP
                    SELECT id
                    INTO canonical_workspace_id
                    FROM knowledge_workspaces
                    WHERE owner_user_id = owner_rec.owner_user_id
                    ORDER BY (name = 'Personal Docs') DESC, created_at ASC, id ASC
                    LIMIT 1;

                    FOR source_ws IN
                        SELECT *
                        FROM knowledge_workspaces
                        WHERE owner_user_id = owner_rec.owner_user_id
                          AND id <> canonical_workspace_id
                        ORDER BY created_at ASC, id ASC
                    LOOP
                        FOR source_tag IN
                            SELECT *
                            FROM knowledge_supertags
                            WHERE workspace_id = source_ws.id
                            ORDER BY name ASC, id ASC
                        LOOP
                            SELECT id
                            INTO target_tag_id
                            FROM knowledge_supertags
                            WHERE workspace_id = canonical_workspace_id
                              AND name = source_tag.name
                            ORDER BY created_at ASC, id ASC
                            LIMIT 1;

                            IF target_tag_id IS NULL THEN
                                UPDATE knowledge_supertags
                                SET workspace_id = canonical_workspace_id,
                                    updated_at = now()
                                WHERE id = source_tag.id;

                                UPDATE knowledge_fields
                                SET workspace_id = canonical_workspace_id,
                                    updated_at = now()
                                WHERE supertag_id = source_tag.id;
                            ELSE
                                UPDATE knowledge_supertags
                                SET parent_supertag_id = target_tag_id
                                WHERE parent_supertag_id = source_tag.id;

                                UPDATE knowledge_saved_views
                                SET supertag_id = target_tag_id,
                                    updated_at = now()
                                WHERE supertag_id = source_tag.id;

                                DELETE FROM knowledge_node_supertags source_link
                                USING knowledge_node_supertags target_link
                                WHERE source_link.supertag_id = source_tag.id
                                  AND target_link.node_id = source_link.node_id
                                  AND target_link.supertag_id = target_tag_id;

                                UPDATE knowledge_node_supertags
                                SET supertag_id = target_tag_id
                                WHERE supertag_id = source_tag.id;

                                FOR source_field IN
                                    SELECT *
                                    FROM knowledge_fields
                                    WHERE supertag_id = source_tag.id
                                    ORDER BY name ASC, id ASC
                                LOOP
                                    SELECT id
                                    INTO target_field_id
                                    FROM knowledge_fields
                                    WHERE supertag_id = target_tag_id
                                      AND name = source_field.name
                                    ORDER BY created_at ASC, id ASC
                                    LIMIT 1;

                                    IF target_field_id IS NULL THEN
                                        UPDATE knowledge_fields
                                        SET workspace_id = canonical_workspace_id,
                                            supertag_id = target_tag_id,
                                            updated_at = now()
                                        WHERE id = source_field.id;
                                    ELSE
                                        DELETE FROM knowledge_field_values source_value
                                        USING knowledge_field_values target_value
                                        WHERE source_value.field_id = source_field.id
                                          AND target_value.node_id = source_value.node_id
                                          AND target_value.field_id = target_field_id;

                                        UPDATE knowledge_field_values
                                        SET field_id = target_field_id,
                                            updated_at = now()
                                        WHERE field_id = source_field.id;

                                        DELETE FROM knowledge_fields
                                        WHERE id = source_field.id;
                                    END IF;
                                END LOOP;

                                DELETE FROM knowledge_supertags
                                WHERE id = source_tag.id;
                            END IF;
                        END LOOP;

                        UPDATE knowledge_nodes
                        SET workspace_id = canonical_workspace_id,
                            updated_at = now()
                        WHERE workspace_id = source_ws.id;

                        UPDATE knowledge_search_index
                        SET workspace_id = canonical_workspace_id,
                            updated_at = now()
                        WHERE workspace_id = source_ws.id;

                        UPDATE knowledge_saved_views
                        SET workspace_id = canonical_workspace_id,
                            updated_at = now()
                        WHERE workspace_id = source_ws.id;

                        UPDATE knowledge_ai_suggestions
                        SET workspace_id = canonical_workspace_id,
                            updated_at = now()
                        WHERE workspace_id = source_ws.id;

                        UPDATE knowledge_import_jobs
                        SET workspace_id = canonical_workspace_id,
                            updated_at = now()
                        WHERE workspace_id = source_ws.id;

                        DELETE FROM knowledge_workspaces
                        WHERE id = source_ws.id;
                    END LOOP;
                END LOOP;

                UPDATE knowledge_workspaces
                SET name = 'Personal Docs',
                    description = COALESCE(NULLIF(description, ''), 'AoiTalk DBを正本にするDocsワークスペース'),
                    settings_json = (
                        COALESCE(settings_json, '{}'::json)::jsonb
                        || '{"canonical_store":"postgresql","derived_index":"qdrant"}'::jsonb
                    )::json,
                    updated_at = now()
                WHERE owner_user_id IS NOT NULL;
            END $$;
            """
        )
    )

    if not _constraint_exists("knowledge_workspaces", "uq_knowledge_workspaces_owner_user"):
        op.create_unique_constraint(
            "uq_knowledge_workspaces_owner_user",
            "knowledge_workspaces",
            ["owner_user_id"],
        )


def downgrade() -> None:
    if _constraint_exists("knowledge_workspaces", "uq_knowledge_workspaces_owner_user"):
        op.drop_constraint(
            "uq_knowledge_workspaces_owner_user",
            "knowledge_workspaces",
            type_="unique",
        )
