# ClickUp MCP Server

AoiTalk プロジェクトの ClickUp タスク管理用 MCP (Model Context Protocol) サーバー。

## 機能

13個のツールを提供:

### ワークスペース
- `get_teams` — チーム一覧取得
- `get_spaces` — スペース一覧取得
- `get_lists` — リスト一覧取得

### タスク管理
- `create_task` — タスク作成（日時・優先度・担当者対応）
- `get_tasks` — リスト内タスク一覧
- `get_task_detail` — タスク詳細取得
- `update_task_status` — ステータス更新
- `update_task` — 汎用フィールド更新
- `delete_task` — タスク削除

### 検索
- `search_tasks` — キーワード・フィルタ検索
- `get_weekly_summary` — タスクサマリー（DB優先）

### コメント
- `add_comment` — コメント追加
- `get_task_comments` — コメント一覧取得

## 起動方法

```bash
# プロジェクトルートから実行
python -m src.tools.external.clickup_mcp
```

## 必要な環境変数

```
CLICKUP_API_KEY=pk_...          # 必須
CLICKUP_TEAM_ID=your-clickup-team-id-here   # 必須
CLICKUP_DEFAULT_LIST_ID=...     # 任意（デフォルトリストID）
```

PostgreSQL連携（任意 — なくても動作する）:
```
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=aoitalk_memory
POSTGRES_USER=aoitalk
POSTGRES_PASSWORD=your-postgres-password
```

## 設定例

### Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "clickup": {
      "command": "python",
      "args": ["-m", "src.tools.external.clickup_mcp"],
      "cwd": "C:\\path\\to\\AoiTalk",
      "env": {
        "CLICKUP_API_KEY": "pk_...",
        "CLICKUP_TEAM_ID": "your-clickup-team-id-here",
        "CLICKUP_DEFAULT_LIST_ID": "your-default-list-id-here"
      }
    }
  }
}
```

### Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "clickup": {
      "command": "python",
      "args": ["-m", "src.tools.external.clickup_mcp"],
      "cwd": "C:\\path\\to\\AoiTalk",
      "env": {
        "CLICKUP_API_KEY": "pk_...",
        "CLICKUP_TEAM_ID": "your-clickup-team-id-here"
      }
    }
  }
}
```

### VS Code (`.vscode/mcp.json`)

```json
{
  "servers": {
    "clickup": {
      "command": "python",
      "args": ["-m", "src.tools.external.clickup_mcp"],
      "env": {
        "CLICKUP_API_KEY": "${env:CLICKUP_API_KEY}",
        "CLICKUP_TEAM_ID": "${env:CLICKUP_TEAM_ID}"
      }
    }
  }
}
```

## アーキテクチャ

```
clickup_mcp/
├── server.py        # FastMCPサーバー（STDIO）
├── api_client.py    # httpx非同期APIクライアント
├── db_client.py     # PostgreSQL読み取りクライアント（任意）
└── tools/           # ツール定義
    ├── workspace.py # ワークスペース系
    ├── tasks.py     # タスク管理系
    ├── search.py    # 検索・サマリー系
    └── comments.py  # コメント系
```

- STDIO トランスポート使用（stdout=JSON-RPC、stderr=ログ）
- DB接続失敗時はAPIのみで動作を継続
- 既存の `clickup_tasks` テーブルをキャッシュとして活用
