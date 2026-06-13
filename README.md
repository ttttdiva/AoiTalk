# AoiTalk

AoiTalk is a workspace app that combines character-based AI chat, project management, task tracking, calendar scheduling, and time reports in one system.

## Main Features

- AI chat over WebUI, terminal, and Discord
- Project-scoped task management backed by PostgreSQL
- Calendar and occurrence management
- Time tracking and reporting
- Specialist delegation for project management, filesystem work, media, utility tools, Spotify, and skills
- MCP integration for supported tool servers

## Stack

- Backend: FastAPI, SQLAlchemy, PostgreSQL, WebSocket
- Frontend: Next.js, React, TypeScript, Tailwind CSS
- Search / memory: PostgreSQL history plus Qdrant-based RAG

## Task Management

Task management is built into AoiTalk. Tasks, occurrences, time entries, reports, and reminders are handled inside the app.
The `project_management_assistant` specialist operates this system directly through the app backend and APIs, not through a separate MCP server.

Related APIs:

- `/api/tasks`
- `/api/task-occurrences`
- `/api/time-entries`
- `/api/reports/time`
- `/api/notifications`
- `/api/projects/{id}/notification-settings`

## Setup

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install Python dependencies.
4. Install frontend dependencies in `frontend/`.
5. Configure `.env`.
6. Prepare PostgreSQL.
7. Run Alembic migrations.

Example:

```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
python -m venv venv
venv\Scripts\activate
pip install -e ".[audio,test]"
cd frontend
npm ci
npm run build
cd ..
alembic upgrade head
```

## Important Environment Variables

```env
NEXTAUTH_SECRET=
AOITALK_WEB_AUTH_SECRET=
INTERNAL_API_KEY=

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=aoitalk
POSTGRES_PASSWORD=
POSTGRES_DB=aoitalk_memory

OPENROUTER_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
XAI_API_KEY=
```

WebUIのログインアカウントはPostgreSQLの users テーブルで管理します。
`.env` に固定ログイン用のユーザー名/パスワードは置きません。
標準URL/ホストはコード側に既定値があるため、通常は `.env` に書く必要はありません。

See `.env.sample` and `docs/setup_guide.md` for more detail.

## Linux / WSL2 セットアップ

Debian/Ubuntu 系 (WSL2 含む) では以下のスクリプトで一括セットアップできる。

```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
cp .env.sample .env    # 必要な API キーや証明書パスを編集
chmod +x setup.sh run.sh
./setup.sh             # PostgreSQL 16 + pgvector / Node.js / venv / alembic を一括実行
./run.sh               # AoiTalk を起動 (exit 42 で自動再起動)
```

`setup.sh` は Debian/Ubuntu の apt を前提としており、`sudo` パスワードが必要になる。
Caddy の TLS 証明書パスは `.env` の `AOITALK_CERT_CRT` / `AOITALK_CERT_KEY` で指定する。

## Run

```bash
run.bat
```

Python API and the Next.js WebUI run as separate processes. `run.bat` starts both and copies the root `.env` into `frontend/.env` before starting the frontend.

## Specialist Tools

Runtime specialist delegation is managed in `src/llm/runtime_tool_registry.py`.

Available specialist tools include:

- `project_management_assistant`
- `filesystem_assistant`
- `utility_assistant`
- `media_assistant`
- `spotify_assistant`
- `skills_assistant`

The project-management specialist uses the built-in task APIs and service layer directly. MCP is reserved for the external servers listed below.

## MCP Servers

Configured MCP servers live under the `mcp` section of `config/config.yaml`.

Current built-in integrations include:

- `utility`
- `web_search`
- `x_search`
- `workspace`
- `memory_rag`
- `os_operations`
- `media`

## Docs

- `docs/setup_guide.md`
- `docs/task_workspace_rebuild.md`
- `docs/DISCORD_BOT_SETUP.md`
- `docs/docker_setup.md`
- `docs/rag-index-guide.md`

## License

MIT
