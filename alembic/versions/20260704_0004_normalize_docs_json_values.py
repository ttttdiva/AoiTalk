"""Normalize Docs structured field values.

Revision ID: 20260704_0004
Revises: 20260704_0003
Create Date: 2026-07-04
"""

from __future__ import annotations

import ast
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_0004"
down_revision: Union[str, None] = "20260704_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _parse_structured(value: object) -> dict[str, object] | list[object] | None:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            parsed = ast.literal_eval(text)
        except Exception:
            return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _format_display(value: object) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {entry}" for key, entry in value.items())
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _normalize_field_values() -> None:
    if not _table_exists("knowledge_field_values"):
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT node_id, field_id, value_text, value_json
            FROM knowledge_field_values
            WHERE value_text LIKE '{%' OR value_text LIKE '[%' OR value_json IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        parsed = _parse_structured(row["value_text"]) or _parse_structured(row["value_json"])
        if parsed is None:
            continue
        bind.execute(
            sa.text(
                """
                UPDATE knowledge_field_values
                SET value_json = CAST(:value_json AS JSON),
                    value_text = :value_text
                WHERE node_id = :node_id AND field_id = :field_id
                """
            ),
            {
                "node_id": row["node_id"],
                "field_id": row["field_id"],
                "value_json": json.dumps(parsed, ensure_ascii=False),
                "value_text": _format_display(parsed),
            },
        )


def _normalize_json_column(table_name: str, column_name: str) -> None:
    if not _column_exists(table_name, column_name):
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT id, {column_name} AS value
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        parsed = _parse_structured(row["value"])
        if parsed is None:
            continue
        bind.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET {column_name} = CAST(:value AS JSON)
                WHERE id = :id
                """
            ),
            {"id": row["id"], "value": json.dumps(parsed, ensure_ascii=False)},
        )


def upgrade() -> None:
    _normalize_field_values()
    for table_name, column_name in [
        ("knowledge_fields", "default_value_json"),
        ("knowledge_fields", "options_json"),
        ("knowledge_supertags", "template_json"),
        ("knowledge_supertags", "config_json"),
        ("knowledge_saved_views", "config_json"),
        ("knowledge_nodes", "body_json"),
        ("knowledge_nodes", "display_props"),
        ("knowledge_nodes", "view_json"),
    ]:
        _normalize_json_column(table_name, column_name)

    if _column_exists("knowledge_nodes", "query_json"):
        op.execute(
            """
            UPDATE knowledge_nodes
            SET query_json = NULL
            WHERE COALESCE(node_type, 'node') <> 'search'
              AND query_json::text = '{}'
            """
        )


def downgrade() -> None:
    # The normalization is intentionally irreversible: it replaces invalid
    # Python repr strings with valid JSON-compatible values.
    pass
