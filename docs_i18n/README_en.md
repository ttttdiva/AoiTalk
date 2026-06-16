# AoiTalk

[日本語](/README.md) /
[英語](/docs_i18n/README_en.md)

AoiTalk is an OpenClaw + ClickUp-like task and project workspace with speech recognition and read-aloud output through multiple TTS engines.
It is not just an AI chat UI. It is designed to keep conversations, task creation, schedules, time records, documents, and tool-assisted work in the same operating context.

## Positioning

The original and public AoiTalk builds are for voice-capable personal and project work.
Task and project management are the center of the product. WebUI chat, speech input, TTS output, Discord, file operations, search, RAG, and calendar integrations are added where they support that workflow.

## Core Capabilities

- **Voice-capable interaction**: Depending on configuration, AoiTalk can use recognition engines such as Whisper, Parakeet, Google Speech, and Gemini, plus TTS engines such as VOICEVOX, VOICEROID, A.I.VOICE, CeVIO, AivisSpeech, Nijivoice, Azure TTS, gTTS, and Irodori TTS.
- **Task and project management**: Projects, spaces, tasks, statuses, due dates, recurrence rules, task occurrences, notifications, time entries, and reports are stored in PostgreSQL.
- **Specialist AI agents**: Runtime delegation covers project management, filesystem work, utility tasks, media, Spotify, and skills so the assistant can perform work instead of only discussing it.
- **Documents and knowledge**: The filer, Office/PDF reading, Qdrant-backed RAG, knowledge workspaces, and project information organizers help reuse project material and working notes.
- **Integrations**: Google Calendar, MCP servers, OpenAI-compatible LLMs, OpenRouter, Gemini, Ollama, and SGLang can be enabled as needed.

## Applications

- **Web app**: The main active UI lives in `frontend/` and uses Next.js.
- **Backend API**: The backend uses FastAPI, SQLAlchemy, Alembic, PostgreSQL, and WebSocket.
- **Mobile app**: `mobile/` is an Expo + React Native app. It is currently maintained conservatively and is not changed unless mobile work is explicitly requested.
- **Voice and bot runtime**: `src/audio/`, `src/tts/`, `src/bot/`, and `src/assistant/` handle audio I/O, Discord, and conversation execution.

## Setup

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

`run.bat` starts the Python API and the Next.js WebUI. Before startup, it copies the root `.env` to `frontend/.env`.

### Linux / WSL2

On Debian/Ubuntu-based environments, including WSL2, setup can be done with the bundled scripts.

```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
cp .env.sample .env
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

`setup.sh` prepares PostgreSQL 16 + pgvector, Node.js, the Python virtual environment, and Alembic migrations. It requires `sudo`.

## Important Environment Variables

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

WebUI login accounts are stored in the PostgreSQL `users` table. Do not put fixed login usernames or passwords in `.env`.
See `.env.sample` and `docs/setup_guide.md` for details.

## Task Management APIs

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

## README Variants

- `README.md`: Japanese README for the development repository and public publish.
- `docs_i18n/README_en.md`: English README for public publish.
- `README.enterprise.md`: Japanese Enterprise README source kept only in the development repository.
- `README.enterprise.en.md`: English Enterprise README source kept only in the development repository.
- `scripts/publish_enterprise.ps1`: Generates the Enterprise output `README.md`, `docs_i18n/README_en.md`, and `ENTERPRISE_PUBLISH_README.md` from the Enterprise templates.

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
