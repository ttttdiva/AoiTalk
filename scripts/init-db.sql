-- ============================================================================
-- AoiTalk PostgreSQL初期化スクリプト
-- Docker Compose起動時に自動実行される
-- ============================================================================

-- pgvector拡張の有効化 (ベクトル検索用)
CREATE EXTENSION IF NOT EXISTS vector;

-- UUID拡張の有効化
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 基本的な権限設定
GRANT ALL PRIVILEGES ON DATABASE aoitalk_memory TO aoitalk;
GRANT USAGE ON SCHEMA public TO aoitalk;
GRANT CREATE ON SCHEMA public TO aoitalk;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO aoitalk;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO aoitalk;

-- デフォルト権限 (今後作成されるオブジェクトにも適用)
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO aoitalk;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO aoitalk;

-- 確認メッセージ
DO $$
BEGIN
    RAISE NOTICE 'AoiTalk PostgreSQL初期化完了';
    RAISE NOTICE 'pgvector拡張: %', (SELECT extversion FROM pg_extension WHERE extname = 'vector');
END
$$;
