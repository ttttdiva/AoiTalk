# OpenAPI → TypeScript 型生成パイプライン

FastAPI（`src/api/`）の Pydantic モデル / ルートから得る wire contract は、
`contracts/openapi/fastapi.json` を Web と Native Mobile が共有する唯一の正本とする。
手書き DTO やクライアントごとの API 複製を正本にせず、backend の変更を決定論的な
生成 artifact へ伝搬させる。

## 正本と生成 artifact

```
FastAPI (Pydantic モデル / ルート)
        │ app.openapi()（サーバー起動なし）
        ▼
scripts/generate_openapi.py
        │ 決定論的（キー順ソート）
        ▼
contracts/openapi/fastapi.json       … 共有 OpenAPI 正本
        ├─ frontend/openapi.json      … Web 互換 artifact（同一スキーマ）
        │    └─ frontend/src/lib/api-types.gen.ts  … Web 生成型
        └─ mobile/src/types/api-types.gen.ts       … Native Mobile 生成型
```

`frontend/openapi.json` は既存 Web の `npm run typegen` を保つための派生 artifact であり、
Mobile の入力ではない。Mobile の `mobile/scripts/generate-api-types.mjs` は必ず共有正本を
読み、Mobile 専用の出力先へ型を生成する。いずれの JSON / `.gen.ts` も手編集しない。

## 再生成手順

backend（`src/api/` の Pydantic モデルやルート）を変更したら、リポジトリルートで次を実行する。

```powershell
# 1. 共有 OpenAPI 正本だけを更新（Mobile / contract の通常入口）
venv\Scripts\python.exe scripts/generate_openapi.py --canonical-only

# 2. Web 互換 artifact も更新する場合（canonical + frontend/openapi.json）
venv\Scripts\python.exe scripts/generate_openapi.py

# 3. Web の型を再生成（frontend/src/lib/api-types.gen.ts）
cd frontend
npm run typegen
cd ..

# 4. Native Mobile の型を再生成（mobile/src/types/api-types.gen.ts）
cd mobile
npm run api:typegen
cd ..

# 5. Product Contract の operationId / route / scope / transport を検証
venv\Scripts\python.exe scripts\validate_mobile_product_contract.py
```

`--canonical-only` は共有正本だけを更新し、`scripts/generate_openapi.py` の引数なし実行は
共有正本と `frontend/openapi.json` の両方を更新する。Mobile 型生成は
`frontend/node_modules/openapi-typescript` を共有実行環境として利用するため、frontend の
依存を先にインストールしておく。どのコマンドも uvicorn 等のサーバーを起動しない。

- 出力は**決定論的**（辞書キーを再帰的にソート）で、同じ backend から再実行しても drift
  が出ない。 `npm run typegen` / `npm run api:typegen` は生成ヘッダーも付与する。
- API 型を直したい場合は backend の Pydantic response/request model と共有正本を更新して
  再生成する。生成された型ファイルの手編集や、frontend と mobile の片方だけへの API 型の
  手動複製は禁止する。

## BFF と direct / sync の境界

共有 OpenAPI が対象にするのは FastAPI の canonical API である。クライアントごとの到達経路は
次の境界を守る。

| 経路 | 役割 | 共有 OpenAPI / Mobile conformance |
|------|------|-----------------------------------|
| FastAPI `/api/...` | backend の canonical API。Mobile は認証済み base URL へ直接呼び出す | 対象。`operationId` を Product Contract から参照する |
| authoritative sync | Mobile の offline / 再同期契約を担う FastAPI operation | 対象。`sync` operationId として参照する |
| frontend `/api/python-proxy/{rest}` | Next.js が FastAPI `/api/{rest}` へ転送する Web 用 proxy | underlying FastAPI の生成型を使う。生成キーは FastAPI 側 `/api/...` |
| `frontend/src/app/api/**` の Next.js BFF / Drizzle route | Web 画面専用の BFF・DB 補助 API | 共有 FastAPI OpenAPI の対象外。P0 Mobile transport（`next_bff`）にしない |

つまり、Web の proxy path と Mobile の direct path は wire contract を共有するが、Next.js
BFF 専用 route を canonical FastAPI と混同しない。P0 Mobile capability は authoritative
FastAPI または sync を利用し、BFF 専用 route に依存させない。

## 方針・注意点

- `frontend/openapi.json` と `frontend/src/lib/api-types.gen.ts` は Web 用、
  `mobile/src/types/api-types.gen.ts` は Native 用の生成 artifact である。正本は常に
  `contracts/openapi/fastapi.json`。
- `api-types.gen.ts` は eslint の global ignore 対象であり、lint 用に手動整形しない。
- `/api/python-proxy/{rest}` は FastAPI の `/api/{rest}` に読み替えられるため、生成型は
  FastAPI 側の path（例: `/api/conversations/{session_id}/dispatch`）でキーされる。
- `openapi-typescript` は Pydantic のサーバー側 default を required として出力することが
  ある。クライアント送信で省略可能な値は既存クライアントの `OptionalizeDefaults<T, K>`
  等で扱い、生成 artifact 自体は変更しない。

## 型の引き方（例）

```ts
import type { components, paths } from "@/lib/api-types.gen";

type Schemas = components["schemas"];
function dispatch(body: Schemas["ConversationDispatchRequest"]) { /* ... */ }

type DispatchBody =
  paths["/api/conversations/{session_id}/dispatch"]["post"]["requestBody"]["content"]["application/json"];
```

Mobile 側は `mobile/src/types/api-types.gen.ts` の `components` / `paths` を import する。
手書きの request / response wire 型を新規追加する前に、response_model を付けて
backend から再生成できないかを確認する。

## 既知の制約

- 多くの GET エンドポイントは `response_model` を宣言していないため、OpenAPI 上の 200
  レスポンススキーマが空（`{}`）になり、生成型から有用な response 型を引けない。
  レスポンス型を生成で賄う場合は backend 側で `response_model` を付与する。
- Next.js BFF 専用 route は別の UI / Drizzle 契約であり、共有 OpenAPI の型生成で置き換えない。
