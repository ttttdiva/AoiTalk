# AoiTalk

[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

**An AI assistant that lets you talk with speech-synthesis characters while managing tasks, schedules, knowledge, and files in the same workspace.**

AoiTalk uses a configured speech-synthesis character as the AI assistant's voice and personality surface, connecting text or voice conversations with practical information management for everyday life and work.

It is more than a chat app that simply reads AI responses aloud. AoiTalk keeps conversation and project context while bringing **task management, scheduling, Docs / Files, knowledge retrieval, Agents, and external tool execution** into one environment.

## What AoiTalk can do

### Talk with speech-synthesis characters

Each character can have its own voice configuration, and AI responses can be spoken using that character's voice. Speech input is also supported, so AoiTalk can be used as an actual voice conversation interface rather than only as text chat.

Most importantly, AoiTalk supports both **VOICEROID2** and **Irodori-TTS**.

- **VOICEROID2** — use VOICEROID2 characters as AoiTalk voices
- **Irodori-TTS** — locally integrated TTS. The default v4.1 Small model is supported alongside per-character choices such as v4.1 Anime and v3 VoiceDesign
- **Other TTS engines** — VOICEVOX, AivisSpeech, A.I.VOICE, CeVIO, Nijivoice, Azure TTS, gTTS, MioTTS, and more
- **Speech recognition** — Whisper, Parakeet, Google Speech, Gemini-family ASR, and more

See the [Irodori-TTS guide](docs/irodori_tts.md) for details.

### Let the character manage tasks and schedules

Tasks and plans can be managed continuously in the same workspace where conversations happen.

- Create, update, and complete tasks
- Schedules, recurrence, and notifications
- Work-time tracking
- Reports and progress review
- Management tied to conversation and project context

### Build and search your knowledge

Information does not have to disappear inside chat history. AoiTalk can store and retrieve Docs, Files, project information, and other knowledge when the assistant needs it.

- Docs / project information
- File management and retrieval
- Office document and PDF reading
- ClipIngest
- Qdrant-backed knowledge retrieval
- Conversation search

### Use Agents and external tools

AoiTalk is not limited to one chat screen. Agents and external services can be combined to carry work forward.

- Agent Team / specialist execution
- Heartbeat / scheduled execution
- MCP
- Web search
- Google Calendar
- Discord Bot
- Spotify
- Other tool integrations

Available providers, models, and tools depend on the current configuration and implementation. Treat the application's settings, catalog, and `src/config_defaults.py` as the source of truth.

### Use Web and Native Mobile clients

- **Web** — Next.js / React application under `frontend/`
- **Mobile** — Expo / React Native native application under `mobile/`. It is a first-class client with its own local database, synchronization, and state management rather than a WebView wrapper

See [Mobile Product Contract / Conformance](docs/mobile_product_contract.md) for the current mobile contract.

## Supported LLM routes

AoiTalk supports multiple LLM paths, including cloud APIs and local inference environments.

- OpenAI
- Gemini
- OpenRouter
- Ollama
- SGLang
- OpenAI-compatible local servers
- Other configured providers / models

## Current architecture

- **Web / BFF**: `frontend/` — Next.js 16 + React 19, including Next.js Route Handlers backed by Drizzle ORM.
- **Python runtime / API**: `main.py`, `src/api/` — FastAPI, WebSocket, AI / Agent runtime, audio, and integrations.
- **Database**: PostgreSQL. `alembic/versions/` is the canonical database schema history.
- **RAG**: Qdrant-backed retrieval paths.
- **Mobile**: `mobile/` — Expo + React Native native client.
- **Audio / Bot**: `src/audio/`, `src/tts/`, and `src/bot/`.

See [docs/README.md](docs/README.md) for the documentation map and the distinction between current specifications and design records.

## Setup

### Windows

Use the standard repository setup entry point:

```powershell
setup.bat
run.bat
```

The current Windows setup requires Python 3.12+, Node.js 22+, Git, and PostgreSQL / `.env`. It prepares the Python virtual environment, frontend dependencies, database schema, production build, and Windows audio / TTS extras.

### Linux / WSL

For Debian / Ubuntu-family Linux and WSL:

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

The current Linux setup requires Python 3.12+ and Node.js 20+. Large audio dependencies are optional by default. To install audio and Irodori-TTS dependencies as well:

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

See the [setup guide](docs/setup_guide.md) for external database and platform details.

### macOS

The Python packages account for macOS, but parts of `setup.sh` assume Debian / Ubuntu package management. On macOS, prepare Python 3.12+, Node.js, PostgreSQL, the Python virtual environment, and frontend dependencies manually, then run `scripts/init_db_schema.py` and the production build described in the [setup guide](docs/setup_guide.md).

## Default service ports

| Service | Default |
| --- | --- |
| FastAPI | `3000` |
| Next.js | `3002` |
| Caddy | `6002` |

Windows `run.bat` and Linux `run.sh` use different default exposure boundaries. Linux / WSL starts loopback-only with Caddy disabled by default. Public exposure should use an explicit TLS boundary such as `run.sh --public --with-caddy`.

## Configuration and database

- `.env.sample` — environment variable template
- `pyproject.toml` — Python dependencies
- `frontend/package.json` — Web dependencies
- `alembic/versions/` — database migrations
- `scripts/init_db_schema.py` — schema initialization entry point
- `src/config_defaults.py` — Python runtime defaults

Do not document fixed login passwords in the README. Bootstrap credentials are read from the local `.env`, including `AOITALK_BOOTSTRAP_ADMIN_PASSWORD`.

## Development and verification

`AGENTS.md` is the repository workflow authority. Use targeted verification for the changed scope, then verify GitHub Actions after pushing `main`. User-visible WebUI behavior changes also require the independent browser QA flow in [AI WebUI QA](docs/ai_webui_qa.md).

## Distribution

The private development repository and public distribution repository are intentionally separated. `scripts/publish_public.ps1` generates the public tree for `ttttdiva/AoiTalk`.

- Public publishing: [docs/public_publish.md](docs/public_publish.md)
- Mobile APK / auto-update standard: [docs/mobile-auto-update-standard.md](docs/mobile-auto-update-standard.md)
- Release checklist: [docs/release-checklist.md](docs/release-checklist.md)
- Enterprise handoff: `README.enterprise.md`

## License

MIT
