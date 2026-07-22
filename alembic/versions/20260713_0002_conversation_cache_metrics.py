"""会話コンテキストとプロンプトキャッシュの計測項目を追加

Revision ID: 20260713_0002
Revises: 20260713_0001
"""

from alembic import op


revision = "20260713_0002"
down_revision = "20260713_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        "reasoning_tokens": "INTEGER DEFAULT 0",
        "cache_read_tokens": "INTEGER DEFAULT 0",
        "cache_write_tokens": "INTEGER DEFAULT 0",
        "prompt_eval_tokens": "INTEGER DEFAULT 0",
        "prompt_eval_ms": "INTEGER DEFAULT 0",
        "cache_hit_rate": "DOUBLE PRECISION",
        "cache_evictions": "INTEGER DEFAULT 0",
        "cache_provider": "VARCHAR(50)",
        "cache_mode": "VARCHAR(50)",
        "cache_key": "VARCHAR(128)",
        "cache_supported": "BOOLEAN",
        "cache_active": "BOOLEAN",
        "metrics_source": "VARCHAR(50)",
    }
    for name, definition in columns.items():
        op.execute(
            f"ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS {name} {definition}"
        )
    op.execute(
        "ALTER TABLE model_pricing ADD COLUMN IF NOT EXISTS "
        "cache_write_input_price_per_1m DOUBLE PRECISION DEFAULT 0"
    )


def downgrade() -> None:
    # Usage history is retained on downgrade; the columns are harmless extras.
    pass
