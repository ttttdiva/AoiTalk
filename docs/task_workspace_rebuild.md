# ローカルタスクワークスペース再構築メモ

## 目的

案件管理をこのツール自身の PostgreSQL データモデルで運用するための現行整理です。

- タスク
- 予定
- 工数計測
- 通知
- チャットからの操作

を同じ案件ワークスペース上で扱います。

## 現行スタック

- Backend: Python FastAPI（AI/音声/エージェント専用）
- CRUD API: Next.js Route Handler + Drizzle ORM
- Database: PostgreSQL + Alembic マイグレーション
- Frontend: Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui
- ベクトル検索: Qdrant RAG

Next.js (port 3002) がフロントエンドとCRUD APIを担当し、Python FastAPI (port 3000) はAI/音声/エージェント機能専用です。

## データモデル

ローカルタスク基盤の主なテーブル:

- `tasks`
- `task_assignees`
- `task_comments`
- `task_activity`
- `task_dependencies`
- `task_recurrence_rules`
- `task_occurrences`
- `time_entries`
- `project_notification_settings`
- `notification_deliveries`

補助方針:

- タスクは案件単位で管理
- `start_at` / `end_at` を持つ予定ベース
- 1ユーザー1本だけアクティブタイマーを許可
- `Inbox` を既定案件として保持

## API

Next.js Route Handler（CRUD）:

- `/api/tasks`
- `/api/task-occurrences`
- `/api/time-entries`
- `/api/reports/time`
- `/api/notifications`
- `/api/projects`
- `/api/projects/[id]/tags`
- `/api/projects/[id]/members`
- `/api/conversations`

Python FastAPI（AI/音声/エージェント）:

- `/api/python/*` 経由で Next.js がプロキシ

## フロントエンド構成

`frontend/` が WebUI 本体です。Next.js が port 3002 で配信します。

画面構成:

- ヘッダー: ビュー切替タブ、キャラクター/LLM選択、音声ステータス、接続ステータス
- サイドバー: ナビリンク（チャット/タスク/ファイル管理）+ チャットページでは会話履歴
- 中央: チャット / タスク / カレンダー / レポート / ファイラー / 設定の主画面

## 起動

ローカル:

```bash
pip install -e ".[audio,test]"
cd frontend
npm ci
cd ..
alembic upgrade head
# Python API
venv\Scripts\python main.py
# Next.js (別ターミナル)
cd frontend && npm run dev -- -p 3002
```

## テスト

```bash
cd frontend
npx playwright test
cd ..
venv\Scripts\python -m pytest tests\test_task_management_service.py tests\test_api_smoke.py -q
```

## 既知の注意点

- 繰り返しタスクのAPI保存（task_recurrence_rules Route Handler）は未実装
