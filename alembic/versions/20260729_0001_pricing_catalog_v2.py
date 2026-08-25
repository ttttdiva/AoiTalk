"""料金計算基盤v2: token_usage拡張と料金カタログテーブル追加

Revision ID: 20260729_0001
Revises: 20260727_0004

方針:
- 既存 migration に倣い、生SQL + IF NOT EXISTS / ADD COLUMN IF NOT EXISTS で
  PostgreSQL 上で何度再実行しても壊れないようにする。
- 既存データの DELETE / TRUNCATE / DROP は行わない。
- model_pricing テーブルは使わなくなるが履歴のため残す。
"""

from alembic import op

revision = "20260729_0001"
down_revision = "20260727_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. token_usage への料金計算列の追加（すべて nullable、既存データ非破壊）
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE token_usage
            ADD COLUMN IF NOT EXISTS requested_model VARCHAR(200),
            ADD COLUMN IF NOT EXISTS resolved_model VARCHAR(200),
            ADD COLUMN IF NOT EXISTS billing_scope_id VARCHAR(100),
            ADD COLUMN IF NOT EXISTS pricing_status VARCHAR(30),
            ADD COLUMN IF NOT EXISTS pricing_catalog_version VARCHAR(50),
            ADD COLUMN IF NOT EXISTS pricing_rule_id VARCHAR(200),
            ADD COLUMN IF NOT EXISTS free_incentive_group VARCHAR(10),
            ADD COLUMN IF NOT EXISTS applied_input_rate NUMERIC(18,8),
            ADD COLUMN IF NOT EXISTS applied_cached_input_rate NUMERIC(18,8),
            ADD COLUMN IF NOT EXISTS applied_cache_write_rate NUMERIC(18,8),
            ADD COLUMN IF NOT EXISTS applied_output_rate NUMERIC(18,8),
            ADD COLUMN IF NOT EXISTS list_input_cost NUMERIC(20,10),
            ADD COLUMN IF NOT EXISTS list_output_cost NUMERIC(20,10),
            ADD COLUMN IF NOT EXISTS list_tool_cost NUMERIC(20,10),
            ADD COLUMN IF NOT EXISTS list_total_cost NUMERIC(20,10),
            ADD COLUMN IF NOT EXISTS provider_reported_cost NUMERIC(20,10),
            ADD COLUMN IF NOT EXISTS provider_reported_cost_details JSONB,
            ADD COLUMN IF NOT EXISTS tool_invocations JSONB
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_pricing_status "
        "ON token_usage (pricing_status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_scope_created "
        "ON token_usage (billing_scope_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_free_group_created "
        "ON token_usage (free_incentive_group, created_at)"
    )

    # ------------------------------------------------------------------
    # 2. 既存行のバックフィル（NULL の行だけを対象にして再実行安全にする）
    # ------------------------------------------------------------------
    op.execute("""
        UPDATE token_usage
           SET requested_model = model
         WHERE requested_model IS NULL
    """)
    op.execute("""
        UPDATE token_usage
           SET billing_scope_id = 'default'
         WHERE billing_scope_id IS NULL
    """)
    op.execute("""
        UPDATE token_usage
           SET pricing_status = 'unknown'
         WHERE pricing_status IS NULL
    """)

    # ------------------------------------------------------------------
    # 3. pricing_rules（料金ルール履歴）
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS pricing_rules (
            id UUID PRIMARY KEY,
            rule_id VARCHAR(200) NOT NULL,
            provider VARCHAR(50) NOT NULL,
            canonical_model VARCHAR(200) NOT NULL,
            pricing_kind VARCHAR(30) NOT NULL,
            input_price_per_1m NUMERIC(18,8),
            cached_input_price_per_1m NUMERIC(18,8),
            cache_write_price_per_1m NUMERIC(18,8),
            output_price_per_1m NUMERIC(18,8),
            long_context_threshold INTEGER,
            long_context_input_multiplier NUMERIC(10,4),
            long_context_output_multiplier NUMERIC(10,4),
            tiers JSONB,
            tool_rates JSONB,
            currency VARCHAR(3) NOT NULL DEFAULT 'USD',
            effective_from TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            effective_to TIMESTAMP WITHOUT TIME ZONE,
            source VARCHAR(300),
            catalog_version VARCHAR(50) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """)
    # SQLAlchemy create_all が先に作成した環境でも列を揃える。
    op.execute("""
        ALTER TABLE pricing_rules
            ADD COLUMN IF NOT EXISTS tiers JSONB,
            ADD COLUMN IF NOT EXISTS tool_rates JSONB,
            ADD COLUMN IF NOT EXISTS long_context_threshold INTEGER,
            ADD COLUMN IF NOT EXISTS long_context_input_multiplier NUMERIC(10,4),
            ADD COLUMN IF NOT EXISTS long_context_output_multiplier NUMERIC(10,4),
            ADD COLUMN IF NOT EXISTS source VARCHAR(300),
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    """)
    # UNIQUE制約として作る（ON CONFLICT ON CONSTRAINT でも推論でも使えるように）。
    # 同名のインデックス/制約がすでに存在する環境では何もしない。
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class WHERE relname = 'uq_pricing_rules_scope'
            ) THEN
                ALTER TABLE pricing_rules
                    ADD CONSTRAINT uq_pricing_rules_scope
                    UNIQUE (provider, canonical_model, effective_from);
            END IF;
        END
        $$;
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pricing_rules_lookup "
        "ON pricing_rules (provider, canonical_model, effective_from, effective_to)"
    )

    # ------------------------------------------------------------------
    # 4. pricing_model_aliases（モデル名エイリアス）
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS pricing_model_aliases (
            id UUID PRIMARY KEY,
            rule_uuid UUID NOT NULL,
            provider VARCHAR(50) NOT NULL,
            alias VARCHAR(200) NOT NULL,
            canonical_model VARCHAR(200) NOT NULL,
            effective_from TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            effective_to TIMESTAMP WITHOUT TIME ZONE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """)
    op.execute("""
        ALTER TABLE pricing_model_aliases
            ADD COLUMN IF NOT EXISTS effective_to TIMESTAMP WITHOUT TIME ZONE,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class WHERE relname = 'uq_pricing_alias_scope'
            ) THEN
                ALTER TABLE pricing_model_aliases
                    ADD CONSTRAINT uq_pricing_alias_scope
                    UNIQUE (provider, alias, effective_from);
            END IF;
        END
        $$;
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pricing_alias_lookup "
        "ON pricing_model_aliases (provider, alias)"
    )

    # ------------------------------------------------------------------
    # 5. pricing_catalog_state（取り込み元ごとの同期状態）
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS pricing_catalog_state (
            source_key VARCHAR(50) PRIMARY KEY,
            catalog_version VARCHAR(50),
            rule_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMP WITHOUT TIME ZONE,
            last_success_at TIMESTAMP WITHOUT TIME ZONE,
            last_status VARCHAR(20),
            last_error TEXT,
            payload_digest VARCHAR(64),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
    """)
    op.execute("""
        ALTER TABLE pricing_catalog_state
            ADD COLUMN IF NOT EXISTS payload_digest VARCHAR(64),
            ADD COLUMN IF NOT EXISTS last_error TEXT,
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    """)


def downgrade() -> None:
    # upgrade は create_all 済みの既存テーブル・既存列も採用するため所有権を判別できない。
    # 料金履歴と利用履歴を破壊しないことを優先し、downgrade では何もしない。
    pass
