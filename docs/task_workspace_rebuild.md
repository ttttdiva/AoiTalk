# タスクワークスペース 現行構成

> ファイル名は過去の「rebuild」作業から維持していますが、この文書は現在のタスク機能を参照するための索引です。古い rebuild 時点の未実装一覧や DB 件数を仕様として残しません。

## 目的

AoiTalk のタスク、予定、繰り返し、通知、作業時間、レポートを project workspace 内で扱うための現行構成をまとめます。

## 現行の責務境界

- UI / BFF の中心は `frontend/` の Next.js アプリです。
- `frontend/src/app/api/tasks/**`、`task-occurrences`、notifications 等には Next.js Route Handler があり、Drizzle を利用します。
- Python 側にも `src/api/routes/tasks/` と task management service が存在します。したがって FastAPI を「AI/音声/エージェント専用」とは扱いません。
- DB schema の正本は `alembic/versions/`。`frontend/src/db/schema.ts` は Next.js が利用する schema 定義です。

API 所有境界を変更するときは「この文書に書いてあるから」ではなく、現在の route と caller を検索して決めてください。

## 主な現在実装

現行コードで確認できる task 領域には、少なくとも次があります。

- task CRUD / project との関連
- assignee / comment / activity / dependency
- start / due / effective date
- recurrence rule / occurrence / exception / skip policy
- notification policy
- time entry / report
- list / tree / timeline / flow を含む Web UI 表現

過去文書にあった「繰り返しタスクの API 保存は未実装」という記述は現在は正しくありません。現行 HEAD には `frontend/src/app/api/tasks/[id]/recurrence/route.ts`、recurrence utility/hooks、Python task service、関連 migration/test が存在します。

## 依存関係

`parent_task_id`、予定日、task dependency は別概念です。

- 親子: task hierarchy
- start/due/effective date: schedule
- dependency: 実行順序の明示的な edge

親子や表示順から dependency を暗黙生成しないという設計は維持します。具体的な validation は現在の API/service を正本とします。

## 参照すべきコード

### Web / BFF

- `frontend/src/app/api/tasks/`
- `frontend/src/lib/task-api.ts`
- `frontend/src/components/tasks/`
- `frontend/src/lib/recurrence-*.ts`

### Python

- `src/api/routes/tasks/`
- `src/services/task_management/`

### Schema

- `alembic/versions/`
- `src/memory/models/tasks.py`
- `frontend/src/db/schema.ts`

### Test

- `tests/test_task_management_service.py`
- `frontend/e2e/` の task 関連 spec
- frontend component/unit tests の task 関連 spec

ファイル名や test 数は増減するため、この文書に固定本数を記録しません。

## 起動・検証

セットアップと起動は [setup_guide.md](setup_guide.md) を正本とします。過去の rebuild 用に Python API と Next dev server を別々に手動起動する手順を標準手順として使いません。

変更時は `AGENTS.md` / `CLAUDE.md` に従い、変更範囲の targeted test/typecheck/lint を実行します。ユーザーが触る task UI を変更した場合は [ai_webui_qa.md](ai_webui_qa.md) の独立 AI browser QA が必要です。

## 文書化ルール

- 「未実装」は route/service/test を検索してから記述する。
- table 数、migration 数、UI component 行数を仕様として固定しない。
- remote / Enterprise の可否は、現在の permission / remote API 実装で確認する。
- rebuild 当時の設計判断を残す必要がある場合は ADR / historical document として分離し、この現行索引へ混在させない。
