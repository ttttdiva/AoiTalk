# OpenAPI → TypeScript 型生成パイプライン

FastAPI（`src/api/`）のスキーマを唯一の正とし、フロントエンドの API 型を
自動生成で同期するための手順と方針をまとめる。手書きでの型同期を廃し、
backend の変更をフロント型へ機械的に伝搬させることが目的。

## 全体像

```
FastAPI (Pydantic モデル / ルート)
        │  app.openapi()
        ▼
scripts/generate_openapi.py        … サーバー起動なしでスキーマ取得
        │  決定論的（キー順ソート）JSON
        ▼
frontend/openapi.json              … 生成物（差分検知にも使用）
        │  openapi-typescript
        ▼
frontend/src/lib/api-types.gen.ts  … 生成物（手編集禁止）
        │  import
        ▼
frontend/src/lib/chat-api.ts 等    … 実クライアントの型付けに利用
```

## 再生成手順

backend（`src/api/` の Pydantic モデルやルート）を変更したら、リポジトリルートで
以下を順に実行する。

```powershell
# 1. OpenAPI スキーマを frontend/openapi.json へ出力
venv\Scripts\python.exe scripts/generate_openapi.py

# 2. TypeScript 型を再生成（src/lib/api-types.gen.ts）
cd frontend
npm run typegen
```

- `scripts/generate_openapi.py` は `WebChatServer` を実体化して `app.openapi()` を
  取得するが、uvicorn 等のサーバーは起動しない（DB 接続やバックグラウンド処理は
  lifespan に閉じ込められており、スキーマ生成では走らない）。
- 出力は**決定論的**（辞書キーを再帰的にソート）。同一 backend に対して再実行しても
  `frontend/openapi.json` に差分が出ないため、CI での drift 検知に使える。
- `npm run typegen` は `openapi-typescript` 実行後に
  `scripts/apply-typegen-header.mjs` で日本語の「自動生成・手編集禁止・再生成手順」
  バナーを冪等に付与する。

## 方針・注意点

- **生成物は手編集しない**：`frontend/openapi.json` と
  `frontend/src/lib/api-types.gen.ts` は再生成で上書きされる。型を直したい場合は
  backend（Pydantic モデル）側を修正して再生成する。
- **eslint 対象外**：`api-types.gen.ts` は `frontend/eslint.config.mjs` の
  `globalIgnores` に登録済み。
- **スコープは FastAPI 経由のみ**：フロントから `/api/python-proxy/...`（FastAPI 直）へ
  向かう呼び出しだけが OpenAPI 型の対象。`/api/conversations/...` などの
  **Next.js BFF ルート（`frontend/src/app/api/` 配下・Drizzle 直）は OpenAPI 対象外**
  なので生成型で置換しない。
- **パスの対応**：フロントの `/api/python-proxy/{rest}` は FastAPI の `/api/{rest}` に
  対応する（Next のプロキシが `/api/python-proxy` を `/api` に読み替える）。生成型は
  FastAPI 側のパス（例：`/api/conversations/{session_id}/dispatch`）でキーされる。
- **default 値の required 化**：`openapi-typescript` は Pydantic のサーバー側デフォルト値を
  持つフィールドを required として出力する。クライアント送信では省略可能なため、
  `chat-api.ts` の `OptionalizeDefaults<T, K>` ユーティリティで該当キーを任意化している。

## 型の引き方（例）

```ts
import type { components } from "@/lib/api-types.gen";

type Schemas = components["schemas"];

// リクエストボディをスキーマ由来の型で受ける
function dispatch(body: Schemas["ConversationDispatchRequest"]) { /* ... */ }
```

パス単位で引く場合は `paths` を使う。

```ts
import type { paths } from "@/lib/api-types.gen";

type DispatchBody =
  paths["/api/conversations/{session_id}/dispatch"]["post"]["requestBody"]["content"]["application/json"];
```

## 既知の制約

- 多くの GET エンドポイントは `response_model` を宣言していないため、OpenAPI 上の
  200 レスポンススキーマが空（`{}`）になり、生成型からは有用なレスポンス型が引けない。
  レスポンス型を生成で賄いたい場合は backend 側で `response_model` を付与する必要がある。
