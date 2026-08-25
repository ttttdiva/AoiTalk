# Mobile Product Contract / Conformance Gate

`contracts/product-contract.json` は、AoiTalk Native Mobile の完成条件を
機械判定するための唯一のデータ正本です。構造は
`contracts/product-contract.schema.json`、FastAPI の API 契約は
`contracts/openapi/fastapi.json` を正本とします。

## 責務分離

```text
FastAPI + Pydantic
       │ scripts/generate_openapi.py --canonical-only
       ▼
contracts/openapi/fastapi.json       (共有 OpenAPI 正本・生成物)
       ├─ frontend/openapi.json       (既存 Web 互換生成物)
       ├─ frontend/src/lib/api-types.gen.ts
       └─ mobile/src/types/api-types.gen.ts (Mobile 生成物)

contracts/product-contract.json      (P0 capability/navigation 正本)
       └─ scripts/validate_mobile_product_contract.py
```

OpenAPI の型は手書き DTO を正本にしません。`mobile/src/types/api.ts` などの
既存のドメイン型は段階的に generated type を利用しますが、API の wire contract
を新たに手動複製してはいけません。生成ファイルは直接編集せず、backend の
response/request model を変更して再生成します。

## Product Contract の内容

各 capability は次を必ず持ちます。

- `id`, `domain`, `criticality`（`p0` / `p1` / `p2`）
- `operations[].operation_id`（OpenAPI の `operationId`。raw URL/routeは禁止）
- `mobile.routes`, `mobile.entry_points`, `mobile.implementation`
- `roles`, `permissions`, `scope`（Project/Space/ACL の可視性）
- `offline`, `persistence`, `mutation`
- 実行可能な前提・操作・結果を表す `acceptance`

現行 P0 registry は auth/scope、canonical navigation、Chat search/session、
Memory、Task、Project visibility、Files、Docs nodes/scale sync、Story work/jobs、
TRPG snapshot/play を代表します。全 FastAPI route の一覧を product contract に
重複記載するのではなく、製品完成性を判定する critical capability に限定します。

## Navigation invariant

`navigation.tabs` はスマホの bottom tab に表示する **5 件**（Chat / Tasks /
Calendar / Files / Docs）だけです。Apps は route と capability を残し、Settings または
sidebar から到達できる hidden workspace として宣言します。

`navigation.routes` は `mobile/src/app` の screen file と一対一です。layout と test
fixture は screen ではありません。各 route は少なくとも一つの entry point を持ち、
各 capability は宣言済み route と entry point の組み合わせで到達可能でなければ
なりません。これにより orphan route、未到達 critical capability、unexpected tab を
検出できます。

## API transport invariant

P0 capability の API は直接 FastAPI (`fastapi`) または authoritative sync
(`sync`) の operationId を参照します。`next_bff` は P0 transport として禁止です。
Next.js 固有の画面補助 API を使う場合は、P0 registry に載せる前に FastAPI の
canonical operation を追加してください。`/api/conversations/search` はその例で、
OpenAPI には安定した `search_conversations` operationId を持ちます。

## 生成・検証

リポジトリルートで実行します。

```powershell
# FastAPI を起動せず共有 OpenAPI 正本だけを更新
venv\Scripts\python.exe scripts\generate_openapi.py --canonical-only

# Web 互換 artifact も更新する場合（既存 frontend typegen の入力）
venv\Scripts\python.exe scripts\generate_openapi.py

# Mobile 型（frontend の openapi-typescript を共有実行環境として利用）
cd mobile
npm run api:typegen
cd ..

# Product Contract / OpenAPI operationId / route / entry / scope / transport gate
venv\Scripts\python.exe scripts\validate_mobile_product_contract.py
```

validator は network/server を起動せず、結果をソートした deterministic error として
返します。`--skip-route-files` は contract 単体テストなど、Expo source tree が存在
しない環境だけで使用し、通常の CI では route check を省略しません。

変更時は capability の acceptance と対応する regression / integration / Android
scenario を同時に更新します。validator PASS は unit/実機 QA の代替ではなく、API・
navigation・scope の入口を失っていないことを保証する conformance gate です。
