"""トークン使用量・モデル料金テーブルを正式管理

Revision ID: 20260712_0001
Revises: 20260710_0003
"""

from alembic import op

revision = "20260712_0001"
down_revision = "20260710_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧環境ではSQLAlchemy create_allが先に作成している場合があるため冪等に採用する。
    op.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id UUID PRIMARY KEY,
            session_id UUID,
            user_id VARCHAR,
            project_id UUID,
            provider VARCHAR(50) NOT NULL,
            model VARCHAR(100) NOT NULL,
            agent_name VARCHAR(100),
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cached_tokens INTEGER DEFAULT 0,
            input_cost DOUBLE PRECISION DEFAULT 0,
            output_cost DOUBLE PRECISION DEFAULT 0,
            total_cost DOUBLE PRECISION DEFAULT 0,
            request_type VARCHAR(50) DEFAULT 'chat',
            latency_ms INTEGER DEFAULT 0,
            is_streaming BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_session_id ON token_usage (session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_user_id ON token_usage (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_project_id ON token_usage (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_created_at ON token_usage (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_date_model ON token_usage (created_at, model)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_project_date ON token_usage (project_id, created_at)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS model_pricing (
            id UUID PRIMARY KEY,
            provider VARCHAR(50) NOT NULL,
            model VARCHAR(100) NOT NULL,
            input_price_per_1m DOUBLE PRECISION NOT NULL,
            output_price_per_1m DOUBLE PRECISION NOT NULL,
            cached_input_price_per_1m DOUBLE PRECISION DEFAULT 0,
            currency VARCHAR(3) DEFAULT 'USD',
            effective_from TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_model_pricing_provider_model ON model_pricing (provider, model)")


def downgrade() -> None:
    # upgradeはcreate_all済みの既存テーブルも採用するため、所有権を判別できない。
    # 利用履歴を破壊しないことを優先し、downgradeではテーブルを保持する。
    pass
