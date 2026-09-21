"""Reclassify semantic Project Q&A rows created before provenance fix.

The first Project Q&A lifecycle migration intentionally classified unknown
agent rows as ``legacy_auto``.  During the short rolling-deployment window,
completed-turn semantic curation also used that value, which made those rows
look like the old raw-message candidates to the cleanup route.  Only rows
with an unambiguous durable scoped-memory provenance marker are repaired;
question text and encrypted answer content are never used as signals.
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260901_0003"
down_revision = "20260901_0002"
branch_labels = None
depends_on = None

_SEMANTIC_REF_TYPES = frozenset(
    {"scoped_memory_job", "project_qa_candidate", "memory_job"}
)


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _is_semantic_ref(value: object) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("type") or "")
        .strip()
        .casefold()
        .replace("-", "_")
        in _SEMANTIC_REF_TYPES
        for item in value
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "project_qa_entries"):
        return
    columns = _columns(bind, "project_qa_entries")
    if not {"origin", "review_state", "answer_source_refs"}.issubset(columns):
        # A legacy installation that skipped Project Information will receive
        # the full lifecycle columns from its own foundation migration later;
        # do not make this repair revision fail that upgrade.
        return

    if getattr(getattr(bind, "dialect", None), "name", "") == "postgresql":
        # ``answer_source_refs`` is JSON in the ORM.  Cast only the known
        # valid JSON value and inspect array members for an exact type marker.
        bind.execute(
            sa.text(
                """
                UPDATE project_qa_entries
                   SET origin = 'semantic_turn'
                 WHERE origin = 'legacy_auto'
                   AND LOWER(COALESCE(review_state, '')) IN ('candidate', 'rejected')
                   AND EXISTS (
                         SELECT 1
                           FROM JSONB_ARRAY_ELEMENTS(
                               CASE
                                   WHEN JSONB_TYPEOF(COALESCE(answer_source_refs::jsonb, '[]'::jsonb)) = 'array'
                                   THEN COALESCE(answer_source_refs::jsonb, '[]'::jsonb)
                                   ELSE '[]'::jsonb
                               END
                           ) AS ref
                          WHERE LOWER(REPLACE(BTRIM(COALESCE(ref->>'type', '')), '-', '_')) IN
                                ('scoped_memory_job', 'project_qa_candidate', 'memory_job')
                   )
                """
            )
        )
        return

    # SQLite/offline test databases do not expose PostgreSQL JSONB operators;
    # keep the same conservative predicate in Python for those dialects.
    rows = bind.execute(
        sa.text(
            """
            SELECT id, origin, review_state, answer_source_refs
              FROM project_qa_entries
             WHERE origin = 'legacy_auto'
               AND LOWER(COALESCE(review_state, '')) IN ('candidate', 'rejected')
            """
        )
    ).mappings()
    for row in rows:
        if not _is_semantic_ref(row.get("answer_source_refs")):
            continue
        bind.execute(
            sa.text(
                "UPDATE project_qa_entries SET origin = 'semantic_turn' WHERE id = :id"
            ),
            {"id": row["id"]},
        )


def downgrade() -> None:
    # The old ``legacy_auto`` value is ambiguous and cannot be restored
    # without reintroducing the cleanup bug.  Leave repaired provenance intact.
    pass
