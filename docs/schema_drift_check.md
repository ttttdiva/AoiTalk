# スキーマドリフト検知（Alembic / PostgreSQL vs Drizzle）

## 目的

AoiTalk では PostgreSQL schema を Python/Alembic と Next.js/Drizzle の両方から参照します。

- **正本**: `alembic/versions/` を適用した PostgreSQL schema
- **Next.js 側定義**: `frontend/src/db/schema.ts`

Drizzle 定義は Next.js BFF が query/type に利用するため、実 DB と食い違うと runtime error や nullability/type 前提の破綻につながります。`scripts/check_schema_drift.py` はこの差を検知するための read-only check です。

migration 数、table 数、Drizzle table 数は増減するため、この文書へ固定値を記載しません。実行時に script が検出した値を使います。

## 比較範囲

現在の checker は `frontend/src/db/schema.ts` に定義された table を基準に、実 PostgreSQL の対応 table/column を比較します。

主な検査:

- column の不足 / 余剰
- PostgreSQL type と Drizzle type の不一致
- nullable / not-null の不一致

意図的に完全一致判定へ含めないものがあります。default expression、全 index/FK/unique constraint 等の詳細は checker 実装を正本として確認してください。Drizzle に存在しない Alembic 専用 table を「余剰」として失敗させる用途でもありません。

## 実行

Windows venv 例:

```powershell
venv\Scripts\python.exe scripts\check_schema_drift.py
```

接続先は script が `.env` / `DATABASE_URL` / `POSTGRES_*` から解決します。DB を変更せず `information_schema` を参照します。

不一致は exit code 非 0 と差分表示で報告します。

## ドリフトを見つけた場合

原則:

1. Alembic migration と実 DB を正本として確認する。
2. 実 DB が正しいなら `frontend/src/db/schema.ts` を合わせる。
3. Alembic 側の設計自体が誤っている場合だけ、新しい migration で修正する。
4. 変更した route/query/type に必要な targeted TypeScript / test を実行する。

実 DB を手作業で直して Alembic history との不一致を残さないでください。

## CI

`.github/workflows/ci.yml` の schema-drift 系 job が、空の PostgreSQL service に migration を適用したうえで checker を実行します。job 名や package 数をこの文書へ固定せず、workflow file を正本とします。

通常の変更では `AGENTS.md` / `CLAUDE.md` に従って local targeted verification → `main` push → GitHub Actions を確認します。`scripts/run_canonical_verification.ps1` は手動専用の用途に限定します。

## 関連文書

- [setup_guide.md](setup_guide.md)
- [openapi_typegen.md](openapi_typegen.md)
- `alembic/versions/`
- `frontend/src/db/schema.ts`
- `scripts/check_schema_drift.py`
