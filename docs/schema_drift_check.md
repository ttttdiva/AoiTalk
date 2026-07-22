# スキーマドリフト検知（Drizzle vs 実DB）

## 目的

同一 PostgreSQL を 2 系統が手書きで二重定義している。

- **Python 側 Alembic**（`alembic/versions/` の 98 リビジョン、約 108 テーブル）
- **フロントエンド側 Drizzle**（`frontend/src/db/schema.ts`、約 54 テーブル）

**正本は Alembic（= 実DB スキーマ）**と定める。`schema.ts` が実DB（Alembic の適用結果）と
食い違ったら検知して失敗させ、乖離を早期に潰すのがこの仕組みの目的である。

`schema.ts` は BFF（`frontend/src/app/api/**`）が Drizzle クエリで参照するため、実DB とズレると
実行時型と TypeScript 型が食い違い、null 安全性やクエリの前提が崩れる。

## スクリプト

`scripts/check_schema_drift.py`

- 接続先は `.env` の `DATABASE_URL`、無ければ `POSTGRES_HOST/PORT/USER/PASSWORD/DB`。
- **読み取り専用**。`information_schema.columns` を SELECT するだけで、DB へは一切書き込まない。
- `schema.ts` を正規表現でパースし、`pgTable` 名・列名・SQL 型・nullable を抽出。
- **比較対象は `schema.ts` に定義されたテーブルのみ**。Alembic 側にしかないテーブルは対象外。
- 検知して失敗させる項目:
  - **列の過不足**（`schema.ts` にあって実DB に無い／実DB にあって `schema.ts` に無い）
  - **型不一致**（Drizzle 型 → PostgreSQL 型に正規化して比較。例: `json` vs `jsonb`、
    `varchar` vs `text`、`timestamp` vs `timestamp with time zone`）
  - **nullable 不一致**（`.notNull()`／`.primaryKey()` の有無 vs 実DB の `is_nullable`）
- 不一致が 1 件でもあれば `exit 1`、無ければ `exit 0`。

### 比較から除外している項目（誤検知回避）

意図的に判定へ含めていない。ここを厳密比較すると false positive の温床になるため。

- **default 式の内容・有無**。`$defaultFn(() => crypto.randomUUID())` はアプリ側デフォルトで
  DB default を生成しない。Alembic の `server_default` 表現（`gen_random_uuid()`、`'{}'::json` 等）とも
  文字列表現が一致しないため、default は判定に使わない。
- **インデックス・ユニーク制約・外部キー・主キー構成そのもの**（列の nullable としては
  `primaryKey`／`notNull` を反映するが、制約単体の突合はしない）。
- **`schema.ts` 未定義のテーブル**（Alembic 専用テーブル）。

## 実行方法

ローカル（Windows / venv 前提）:

```bash
venv\Scripts\python.exe scripts\check_schema_drift.py
```

必要な pip 依存は `psycopg2`（または `psycopg2-binary`）と `python-dotenv` のみ。
ローカルは `localhost:5432/aoitalk_memory` が稼働している前提。

出力例（ドリフトなし）:

```
schema.ts から 54 テーブルを検出

ドリフトなし: schema.ts は実DB（Alembic適用結果）と整合しています
```

ドリフト検出時は `[列欠落]`／`[列余剰]`／`[型不一致]`／`[nullable不一致]` を列単位で出力し `exit 1`。

## ドリフトを検出したら

**正本は Alembic（実DB）**。原則として `schema.ts` 側を実DB に合わせて修正する。
実DB の方が間違っている（Alembic の設計ミス）と判断した場合のみ、別途 Alembic マイグレーションで直す。

`schema.ts` の nullable/型を厳密化すると、それを参照する BFF ルート（`frontend/src/app/api/**`）で
TypeScript 型エラーが顕在化することがある（例: nullable を前提にしていたコードが非 null に締まる）。
その場合は BFF 側の型・分岐を実DB の実態に合わせて修正する。修正後は必ず以下で確認する。

```bash
cd frontend && npx tsc --noEmit
```

## CI 組み込みの考え方（推奨構成）

> 注: `.github/workflows/ci.yml` の編集はこのタスクのスコープ外。以下は推奨・実現性メモ。

ドリフト検知は「実DB スキーマ = Alembic の適用結果」を前提にするため、CI では
**postgres service コンテナに Alembic を流してから**スクリプトを実行する。

推奨ジョブ構成:

1. `services:` に `postgres:16`（`pgvector` 拡張は不要。後述）を立て、`.env` 相当の
   `POSTGRES_*` を環境変数で与える。
2. Alembic 実行に必要な**最小 pip 依存だけ**を入れる（実測値。重量モジュールは不要）:

   ```
   pip install alembic "sqlalchemy>=2" psycopg2-binary python-dotenv cryptography
   ```

3. `alembic upgrade head` で空 DB にスキーマを構築。
4. `python scripts/check_schema_drift.py` を実行し、`exit 1` で CI を fail させる。

### Alembic 実行の実現性（実測）

- `alembic/env.py` は `from src.memory.models import Base` を import する。この import 連鎖を
  クリーン venv（上記 5 パッケージのみ）で実測したところ、**全モデルモジュールと 98 個の
  マイグレーションが ModuleNotFoundError なく読み込め、1380 行超の DDL がレンダリングできた**。
- つまり env.py は `src` の**重量モジュール（LLM・Qdrant・FastAPI 等）を引き込まない**。
  モデルの追加依存は `cryptography`（`src/security/field_crypto`）のみで、これは軽量・pip 導入可能。
- `pgvector` は不要。`src/memory/models/base.py` に「pgvector removed - using Qdrant」とあり、
  マイグレーションも `vector` 拡張を作らない（`CREATE EXTENSION vector` は存在しない）。
- 注意: 一部マイグレーションは `bind.exec_driver_sql(...)` によるデータ移行を含むため、
  `alembic upgrade head --sql`（オフライン）では途中で失敗する。**CI では実 DB へ接続する
  オンラインの `alembic upgrade head` を使うこと**（service コンテナがあれば問題ない）。
