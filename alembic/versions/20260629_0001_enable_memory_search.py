"""Enable memory search in stored app config.

Revision ID: 20260629_0001
Revises: 20260619_0001
Create Date: 2026-06-29
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260629_0001"
down_revision = "20260619_0001"
branch_labels = None
depends_on = None


def _set_memory_search(enabled: bool) -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("app_config_settings"):
        return

    if bind.dialect.name == "postgresql":
        value = "true" if enabled else "false"
        op.execute(
            sa.text(
                """
                UPDATE app_config_settings
                SET value = jsonb_set(
                    COALESCE(value::jsonb, '{}'::jsonb),
                    '{memory,enable_search}',
                    (:enabled)::jsonb,
                    true
                )::json
                WHERE key = 'global'
                """
            ).bindparams(enabled=value)
        )
        return

    row = bind.execute(
        sa.text("SELECT value FROM app_config_settings WHERE key = :key"),
        {"key": "global"},
    ).fetchone()
    if row is None:
        return

    raw_value = row[0]
    if isinstance(raw_value, str):
        config = json.loads(raw_value or "{}")
    elif isinstance(raw_value, dict):
        config = dict(raw_value)
    else:
        config = {}

    memory = config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        config["memory"] = memory
    memory["enable_search"] = enabled

    bind.execute(
        sa.text("UPDATE app_config_settings SET value = :value WHERE key = :key"),
        {"key": "global", "value": json.dumps(config, ensure_ascii=False)},
    )


def upgrade() -> None:
    _set_memory_search(True)


def downgrade() -> None:
    _set_memory_search(False)
