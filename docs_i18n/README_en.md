# AoiTalk

[日本語](/README.md) / [English](/docs_i18n/README_en.md)

AoiTalk is an AI workspace that combines chat, voice, tasks, projects, Docs, Files, and tool execution. It is no longer accurately described as only a voice assistant; the primary UI is the web application under `frontend/`.

## Current architecture

- **Web / BFF**: `frontend/` — Next.js 16 + React 19, including Next.js Route Handlers backed by Drizzle ORM.
- **Python runtime / API**: `main.py`, `src/api/` — FastAPI, WebSocket, AI/agent runtime, audio, integrations, and selected application APIs. FastAPI must not be treated as an AI-only backend.
- **Database**: PostgreSQL. `alembic/versions/` is the canonical database schema. `frontend/src/db/schema.ts` is the Drizzle schema used by the Next.js BFF and is checked for drift against the migrated database.
- **RAG**: Qdrant-backed retrieval. PostgreSQL pgvector is not a required runtime dependency in the current setup.
- **Mobile**: `mobile/` — Expo + React Native.
- **Desktop**: `desktop/` — Tauri v2 shell around the local web runtime.
- **Audio / Bot**: `src/audio/`, `src/tts/`, and `src/bot/`.

See [docs/README.md](../docs/README.md) for the documentation map and for the distinction between current runbooks, implementation contracts, design records, and generated/reference material.

## Main capabilities

AoiTalk includes web chat and conversation history, project context, task and time management, Docs and Files, ClipIngest, knowledge search, ASR/TTS, Discord and Spotify integration, Google Calendar, MCP, web search, and multiple cloud/local LLM providers. The exact provider/model/tool catalog changes with the implementation and configuration; use the application catalog and source configuration as the authority rather than a static README list.

## Setup

### Windows

Use the repository setup script instead of reproducing dependency installation manually:

```powershell
setup.bat
run.bat
```

The current Windows setup requires Python 3.12+ and Node.js 22+, prepares the venv and frontend dependencies, initializes/migrates PostgreSQL, and creates the production frontend build.

### Linux / WSL

For Debian/Ubuntu-family Linux and WSL:

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

The current Linux setup requires Python 3.12+ and Node.js 20+. Large audio dependencies are intentionally optional:

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

The setup can prepare PostgreSQL, but pgvector is not mandatory. See [docs/setup_guide.md](../docs/setup_guide.md) for external database and platform details.

### macOS

`setup.sh` contains Debian/Ubuntu-oriented package-management steps, so macOS is a manual setup path. Prepare Python 3.12+, Node.js, PostgreSQL, the Python venv and frontend dependencies, then run the database initialization and production frontend build described in the setup guide.

## Default service ports

| Service | Default |
| --- | --- |
| FastAPI | `3000` |
| Next.js | `3002` |
| Caddy | `6002` |

Linux/WSL starts loopback-only with Caddy disabled by default. Public exposure requires an explicit TLS boundary such as `./run.sh --public --with-caddy`.

## Configuration and schema sources

- `.env.sample` — environment variable template
- `pyproject.toml` — Python dependencies and supported Python version
- `frontend/package.json` — frontend dependencies and scripts
- `alembic/versions/` — canonical PostgreSQL schema history
- `scripts/init_db_schema.py` — schema initialization/migration entrypoint
- `src/config_defaults.py` — Python runtime defaults

Do not document fixed login passwords. Setup-generated bootstrap credentials are read from the local `.env`, including `AOITALK_BOOTSTRAP_ADMIN_PASSWORD`.

## Development and verification

`AGENTS.md` and `CLAUDE.md` are the repository workflow authority. Use targeted verification for the changed scope, then verify the GitHub Actions run after pushing `main`. User-visible WebUI behavior changes also require the independent browser QA flow in [docs/ai_webui_qa.md](../docs/ai_webui_qa.md).

## Distribution

This development repository and the public distribution repository are intentionally separate. `scripts/publish_public.ps1` synchronizes the sanitized public tree to `ttttdiva/AoiTalk`.

- Public publishing: [docs/public_publish.md](../docs/public_publish.md)
- Mobile APK/update standard: [docs/mobile-auto-update-standard.md](../docs/mobile-auto-update-standard.md)
- Release checklist: [docs/release-checklist.md](../docs/release-checklist.md)
- Enterprise handoff: `README.enterprise.md` is the single human-facing runbook.

## License

MIT
