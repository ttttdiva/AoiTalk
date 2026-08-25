"""案件管理アシスタントの重複レコードを統合する。

Revision ID: 20260807_0004
Revises: 20260807_0003
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260807_0004"
down_revision = "20260807_0003"
branch_labels = None
depends_on = None


LEGACY_SLUG = "project_management_assistant"
CANONICAL_SLUG = "project_manager"


def _replace_character_slug_references(bind: sa.engine.Connection) -> None:
    # SQLAlchemy's PostgreSQL inspector omits ``pg_temp`` relations.  Use the
    # active-schema catalog for disposable clone upgrades, with the inspector
    # as a fallback for lightweight/non-PostgreSQL test binds.
    def table_exists(table_name: str) -> bool:
        try:
            return (
                bind.execute(
                    sa.text(
                        """
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = current_schema()
                          AND table_name = :table_name
                        LIMIT 1
                        """
                    ),
                    {"table_name": table_name},
                ).first()
                is not None
            )
        except Exception:
            return sa.inspect(bind).has_table(table_name)

    for table_name, column_name in (
        ("conversation_sessions", "character_name"),
        ("conversation_archives", "character_name"),
        ("conversation_history", "character_name"),
        ("conversation_messages", "sender_id"),
        ("users", "preferred_character"),
        ("spotify_activity_logs", "character_name"),
        ("spotify_session_summaries", "character_name"),
    ):
        if not table_exists(table_name):
            continue
        bind.execute(
            sa.text(
                f"UPDATE {table_name} SET {column_name} = :canonical "
                f"WHERE {column_name} = :legacy"
            ),
            {"canonical": CANONICAL_SLUG, "legacy": LEGACY_SLUG},
        )

    bind.execute(
        sa.text(
            """
            UPDATE conversation_messages
            SET message_metadata = jsonb_set(
                message_metadata::jsonb,
                '{character_name}',
                to_jsonb(CAST(:canonical AS text)),
                false
            )::json
            WHERE message_metadata ->> 'character_name' = :legacy
            """
        ),
        {"canonical": CANONICAL_SLUG, "legacy": LEGACY_SLUG},
    )

    bind.execute(
        sa.text(
            """
            UPDATE conversation_sessions AS sessions
            SET group_character_names = (
                SELECT COALESCE(
                    json_agg(normalized.value ORDER BY normalized.first_position),
                    '[]'::json
                )
                FROM (
                    SELECT
                        CASE
                            WHEN item.value = :legacy THEN :canonical
                            ELSE item.value
                        END AS value,
                        MIN(item.position) AS first_position
                    FROM json_array_elements_text(
                        COALESCE(sessions.group_character_names, '[]'::json)
                    ) WITH ORDINALITY AS item(value, position)
                    GROUP BY CASE
                        WHEN item.value = :legacy THEN :canonical
                        ELSE item.value
                    END
                ) AS normalized
            )
            WHERE EXISTS (
                SELECT 1
                FROM json_array_elements_text(
                    COALESCE(sessions.group_character_names, '[]'::json)
                ) AS item(value)
                WHERE item.value = :legacy
            )
            """
        ),
        {"canonical": CANONICAL_SLUG, "legacy": LEGACY_SLUG},
    )

    bind.execute(
        sa.text(
            """
            DELETE FROM conversation_participants AS legacy_participant
            WHERE legacy_participant.participant_type = 'character'
              AND legacy_participant.participant_id = :legacy
              AND EXISTS (
                  SELECT 1
                  FROM conversation_participants AS canonical_participant
                  WHERE canonical_participant.session_id = legacy_participant.session_id
                    AND canonical_participant.participant_type = 'character'
                    AND canonical_participant.participant_id = :canonical
              )
            """
        ),
        {"canonical": CANONICAL_SLUG, "legacy": LEGACY_SLUG},
    )
    bind.execute(
        sa.text(
            """
            UPDATE conversation_participants
            SET participant_id = :canonical
            WHERE participant_type = 'character'
              AND participant_id = :legacy
            """
        ),
        {"canonical": CANONICAL_SLUG, "legacy": LEGACY_SLUG},
    )


def reconcile_project_manager_character(bind: sa.engine.Connection) -> None:
    """Idempotently apply the canonical character merge to ``bind``.

    The terminal Image Studio merge migration uses this function only when a
    legacy duplicate signature is still present (the old runtime-only
    ``alembic_version=20260807_0004`` ambiguity).  Normal and dual-head paths
    therefore execute no generic-data writes.
    """

    _replace_character_slug_references(bind)

    rows = {
        row.slug: row.id
        for row in bind.execute(
            sa.text(
                "SELECT id, slug FROM characters "
                "WHERE slug IN (:legacy, :canonical)"
            ),
            {"legacy": LEGACY_SLUG, "canonical": CANONICAL_SLUG},
        )
    }
    legacy_id = rows.get(LEGACY_SLUG)
    canonical_id = rows.get(CANONICAL_SLUG)

    if legacy_id is None:
        return

    if canonical_id is not None:
        bind.execute(
            sa.text(
                """
                DELETE FROM character_world_books AS duplicate_link
                WHERE duplicate_link.character_id = :canonical_id
                  AND EXISTS (
                      SELECT 1
                      FROM character_world_books AS legacy_link
                      WHERE legacy_link.character_id = :legacy_id
                        AND legacy_link.world_book_id = duplicate_link.world_book_id
                  )
                """
            ),
            {"canonical_id": canonical_id, "legacy_id": legacy_id},
        )
        bind.execute(
            sa.text(
                "UPDATE character_world_books SET character_id = :legacy_id "
                "WHERE character_id = :canonical_id"
            ),
            {"canonical_id": canonical_id, "legacy_id": legacy_id},
        )
        bind.execute(
            sa.text("DELETE FROM characters WHERE id = :canonical_id"),
            {"canonical_id": canonical_id},
        )

    bind.execute(
        sa.text("UPDATE characters SET slug = :canonical WHERE id = :legacy_id"),
        {"canonical": CANONICAL_SLUG, "legacy_id": legacy_id},
    )


def upgrade() -> None:
    reconcile_project_manager_character(op.get_bind())


def downgrade() -> None:
    # 重複レコードや分割済みの会話参照は安全に復元できないため戻さない。
    pass
