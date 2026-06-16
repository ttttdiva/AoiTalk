# AoiTalk

AoiTalkは、音声認識と複数TTSによる読み上げを備えた、OpenClaw + ClickUp 的なタスク管理・プロジェクト実行ワークスペースです。
単なるチャットUIではなく、会話、タスク、予定、作業時間、資料、外部ツールを同じ文脈で扱えるようにすることを目標にしています。

AoiTalk is an OpenClaw + ClickUp-like task and project workspace with speech recognition and read-aloud output through multiple TTS engines.
It is not just a chat UI. It keeps conversations, tasks, schedules, time records, documents, and tool-assisted work in the same operating context.

## 位置づけ / Positioning

個人版および公開版のAoiTalkは、声で相談し、AIに作業を分担させ、その結果をタスク管理へ戻すためのワークスペースです。
プロジェクト管理を中心に据えつつ、WebUI、音声入力、TTS、Discord、ファイル操作、検索、RAG、カレンダー連携を必要に応じて組み合わせます。

The original and public AoiTalk builds are for voice-capable personal and project work.
They center on task and project management, then add WebUI chat, speech input, TTS output, Discord, file operations, search, RAG, and calendar integrations where they help the workflow.

## 中核機能 / Core Capabilities

- **音声対応の会話操作**: Whisper、Parakeet、Google Speech、Gemini系の認識器と、VOICEVOX、VOICEROID、A.I.VOICE、CeVIO、AivisSpeech、Nijivoice、Azure TTS、gTTS、Irodori TTS などの読み上げエンジンを構成に応じて利用できます。
- **タスク・プロジェクト管理**: プロジェクト、スペース、タスク、ステータス、期日、繰り返し、発生日、通知、作業時間、レポートをPostgreSQL上で管理します。
- **AI専門エージェント**: project management、filesystem、utility、media、Spotify、skills などの専門ランナーに処理を委譲し、会話だけで終わらない作業を実行します。
- **資料と知識の活用**: ファイラー、Office/PDF読取、QdrantベースのRAG、知識ワークスペース、プロジェクト情報整理を使い、案件資料や作業メモを再利用しやすくします。
- **外部連携**: Google Calendar、MCPサーバー、OpenAI互換LLM、OpenRouter、Gemini、Ollama、SGLangなどを構成に応じて使えます。

- **Voice-capable interaction**: Depending on configuration, AoiTalk can use recognition engines such as Whisper, Parakeet, Google Speech, and Gemini, plus TTS engines such as VOICEVOX, VOICEROID, A.I.VOICE, CeVIO, AivisSpeech, Nijivoice, Azure TTS, gTTS, and Irodori TTS.
- **Task and project management**: Projects, spaces, tasks, statuses, due dates, recurrence rules, task occurrences, notifications, time entries, and reports are stored in PostgreSQL.
- **Specialist AI agents**: Runtime delegation covers project management, filesystem work, utility tasks, media, Spotify, and skills so the assistant can perform work instead of only discussing it.
- **Documents and knowledge**: The filer, Office/PDF reading, Qdrant-backed RAG, knowledge workspaces, and project information organizers help reuse project material and working notes.
- **Integrations**: Google Calendar, MCP servers, OpenAI-compatible LLMs, OpenRouter, Gemini, Ollama, and SGLang can be enabled as needed.

## アプリ構成 / Applications

- **Web app**: `frontend/` にある Next.js UI が現在の主要開発対象です。
- **Backend API**: FastAPI、SQLAlchemy、Alembic、PostgreSQL、WebSocketで構成されています。
- **Mobile app**: `mobile/` は Expo + React Native です。現在はメンテナンス中心で、明示的なモバイル作業以外では変更対象にしません。
- **Voice and bot runtime**: `src/audio/`、`src/tts/`、`src/bot/`、`src/assistant/` が音声入出力、Discord、会話実行を担当します。

- **Web app**: The main active UI lives in `frontend/` and uses Next.js.
- **Backend API**: The backend uses FastAPI, SQLAlchemy, Alembic, PostgreSQL, and WebSocket.
- **Mobile app**: `mobile/` is an Expo + React Native app. It is currently maintained conservatively and is not changed unless mobile work is explicitly requested.
- **Voice and bot runtime**: `src/audio/`, `src/tts/`, `src/bot/`, and `src/assistant/` handle audio I/O, Discord, and conversation execution.

## セットアップ / Setup

### Windows

```powershell
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

`run.bat` はPython APIとNext.js WebUIを起動します。起動前にルート `.env` を `frontend/.env` へコピーします。

`run.bat` starts the Python API and the Next.js WebUI. Before startup, it copies the root `.env` to `frontend/.env`.

### Linux / WSL2

Debian/Ubuntu系、WSL2を含む環境では以下のスクリプトで一括セットアップできます。

```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
cp .env.sample .env
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

`setup.sh` は PostgreSQL 16 + pgvector、Node.js、Python venv、Alembic migration をまとめて準備します。`sudo` が必要です。

`setup.sh` prepares PostgreSQL 16 + pgvector, Node.js, the Python virtual environment, and Alembic migrations. It requires `sudo`.

## 重要な環境変数 / Important Environment Variables

```env
NEXTAUTH_SECRET=
AOITALK_WEB_AUTH_SECRET=
AOITALK_JWT_SECRET=
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
NIJIVOICE_API_KEY=
```

WebUIのログインアカウントはPostgreSQLの `users` テーブルで管理します。`.env` に固定ログイン用のユーザー名やパスワードは置きません。
詳細は `.env.sample` と `docs/setup_guide.md` を参照してください。

WebUI login accounts are stored in the PostgreSQL `users` table. Do not put fixed login usernames or passwords in `.env`.
See `.env.sample` and `docs/setup_guide.md` for details.

## タスク管理API / Task Management APIs

タスク管理はAoiTalk本体に組み込まれています。`project_management_assistant` は別のMCPサーバーではなく、アプリのバックエンドとAPIを直接操作します。

Task management is built into AoiTalk. `project_management_assistant` works directly through the app backend and APIs rather than through a separate MCP server.

- `/api/tasks`
- `/api/task-occurrences`
- `/api/time-entries`
- `/api/reports/time`
- `/api/notifications`
- `/api/projects/{id}/notification-settings`

## Specialist Tools

Runtime specialist delegation is managed in `src/llm/runtime_tool_registry.py`.

- `project_management_assistant`
- `filesystem_assistant`
- `utility_assistant`
- `media_assistant`
- `spotify_assistant`
- `skills_assistant`

## MCP Servers

Configured MCP servers live under the `mcp` section of `config/config.yaml`.

- `utility`
- `web_search`
- `x_search`
- `workspace`
- `memory_rag`
- `os_operations`
- `media`

## READMEの使い分け / README Variants

- `README.md`: 開発元リポジトリと公開版Publishで使うREADMEです。音声入力、複数TTS、個人向け/公開向け機能を含めて説明します。
- `README.enterprise.md`: 開発リポジトリ側に置くEnterprise出力用のREADMEソースです。`publish_enterprise.bat` / `scripts/publish_enterprise.ps1` が生成先の `README.md` と `ENTERPRISE_PUBLISH_README.md` に反映します。公開版Publishには含めません。
- `scripts/publish_public.ps1`: 公開版では `README.md` を使い、Enterprise専用READMEや内部運用資料を公開成果物から除外します。

- `README.md`: Used by the development repository and public publish. It describes voice input, multiple TTS engines, and the personal/public feature set.
- `README.enterprise.md`: Enterprise README source kept in the development repository. `publish_enterprise.bat` / `scripts/publish_enterprise.ps1` writes it to the generated `README.md` and `ENTERPRISE_PUBLISH_README.md`. It is excluded from public publish output.
- `scripts/publish_public.ps1`: The public publisher uses `README.md` and excludes the Enterprise-specific README and internal operation files.

## Docs

- `docs/setup_guide.md`
- `docs/task_workspace_rebuild.md`
- `docs/public_publish.md`
- `docs/enterprise_publish.md`
- `docs/DISCORD_BOT_SETUP.md`
- `docs/docker_setup.md`
- `docs/rag-index-guide.md`

## License

MIT
