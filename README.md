# AoiTalk

[日本語](/README.md) / [English](/docs_i18n/README_en.md)

AoiTalk は、チャット・音声・タスク・プロジェクト・Docs・Files・外部ツール実行を同じワークスペースで扱う AI アシスタントです。音声アシスタントだけを目的とした構成ではなく、現在の主要 UI は `frontend/` の Web アプリです。

## 現在の構成

- **Web / BFF**: `frontend/` — Next.js 16 + React 19。画面と、Drizzle ORM を使う Next.js Route Handler を持ちます。
- **Python runtime / API**: `main.py`、`src/api/` — FastAPI、WebSocket、AI/Agent、音声、外部連携などを担当します。機能によっては Python 側にも業務 API があるため、「FastAPI は AI 専用」とは扱いません。
- **Database**: PostgreSQL。DB スキーマの正本は `alembic/versions/` です。`frontend/src/db/schema.ts` は Next.js BFF が利用する Drizzle 定義で、実 DB とのドリフトを CI で検査します。
- **RAG**: Qdrant を利用する検索経路があります。PostgreSQL の pgvector 拡張は現在の必須要件ではありません。
- **Mobile**: `mobile/` — Expo + React Native の **first-class Native client**（WebView の代替ではありません）。現行の完成条件は [Mobile Product Contract / Conformance](docs/mobile_product_contract.md)、共有 API 契約と型生成は [OpenAPI 型生成手順](docs/openapi_typegen.md) を参照してください。
- **Desktop**: `desktop/` — Tauri v2 のローカル WebView shell です。
- **音声 / Bot**: `src/audio/`、`src/tts/`、`src/bot/`。

詳細な文書一覧と「現行仕様 / 設計記録」の区別は [docs/README.md](docs/README.md) を参照してください。

## 主な機能

- Web チャット、会話履歴、プロジェクト文脈、Agent Team / specialist 実行
- タスク、予定、繰り返し、通知、作業時間、レポート
- Docs / project information / ClipIngest / Files / Office・PDF 読み取り
- Qdrant ベースの知識検索と会話検索
- Whisper / Parakeet / Google Speech / Gemini 系 ASR
- VOICEVOX、AivisSpeech、VOICEROID、A.I.VOICE、CeVIO、Nijivoice、Azure TTS、gTTS、Irodori-TTS、MioTTS などの TTS
- Discord Bot / Spotify / Google Calendar / MCP / Web 検索などの外部連携
- OpenAI、Gemini、OpenRouter、ローカル OpenAI 互換 server、Ollama、SGLang 等の LLM 経路

利用可能な provider / model / tool は設定と実装で変わるため、README の列挙ではなく実際の設定画面・catalog・`src/config_defaults.py` 等を正本として確認してください。

## セットアップ

### Windows

Windows の正規セットアップ入口は `setup.bat` です。個別の `pip install` や Alembic 手順を README から手作業で再現するより、まずスクリプトを使用してください。

```powershell
setup.bat
run.bat
```

現行 `setup.bat` は Python 3.12 以上、Node.js 22 以上、Git、PostgreSQL / `.env`、Python venv、フロントエンド依存、DB schema、production build を確認・準備します。Windows の音声・TTS 用 extra もセットアップ対象です。

### Linux / WSL

Debian / Ubuntu 系 Linux と WSL の正規入口は `setup.sh` / `run.sh` です。

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

現行 `setup.sh` は Python 3.12 以上と Node.js 20 以上を要求します。既定では重量級の音声依存を入れません。音声・Irodori 等も同時に入れる場合だけ明示します。

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

`setup.sh` は PostgreSQL を準備できますが、pgvector は必須にしません。外部 DB を使う場合は `.env` と `AOITALK_SKIP_DB_SETUP` の扱いを [セットアップガイド](docs/setup_guide.md) で確認してください。

### macOS

Python パッケージ自体は macOS を考慮していますが、`setup.sh` は Debian / Ubuntu 系のパッケージ管理を前提にした箇所があります。macOS では Python 3.12+、Node、PostgreSQL、venv、フロントエンド依存を個別に用意してから `scripts/init_db_schema.py` と production build を実行してください。詳細は [docs/setup_guide.md](docs/setup_guide.md) を参照してください。

### リポジトリについて

この開発リポジトリで作業している場合は現在の checkout をそのまま使用します。公開版の同期先は `ttttdiva/AoiTalk` であり、`scripts/publish_public.ps1` が公開用 tree を生成します。開発元と公開先のリポジトリ名が異なるのは意図された構成です。

## 起動時の既定ポート

起動ラッパーの既定値は次のとおりです。

| 用途 | 既定 |
| --- | --- |
| FastAPI | `3000` |
| Next.js | `3002` |
| Caddy | `6002` |

Windows `run.bat` と Linux `run.sh` では公開境界の既定が異なります。Linux / WSL は loopback + Caddy 無効が既定で、外部公開時は `run.sh --public --with-caddy` のように TLS 境界を明示します。単純に `0.0.0.0` へ直接公開しないでください。

## 設定・DB

- `.env` の雛形: `.env.sample`
- Python 依存: `pyproject.toml`
- Web 依存: `frontend/package.json`
- DB migration: `alembic/versions/`
- DB 初期化入口: `scripts/init_db_schema.py`
- Python defaults: `src/config_defaults.py`

セットアップスクリプトは認証・内部 API 用 secret と初回管理者 password を生成・確認します。固定 password を README に書かないでください。初回管理者 password は `.env` の `AOITALK_BOOTSTRAP_ADMIN_PASSWORD` を確認します。

## 開発・検証

リポジトリ内の作業規約は `AGENTS.md` と `CLAUDE.md` が正本です。通常の変更は変更範囲に応じたターゲット検証を行い、`main` push 後の GitHub Actions を確認します。WebUI のユーザー挙動を変える場合は [AI WebUI QA](docs/ai_webui_qa.md) の独立実ブラウザ確認が追加で必要です。

## 配布

- 公開版同期: [docs/public_publish.md](docs/public_publish.md)
- Mobile 自動更新 / APK: [docs/mobile-auto-update-standard.md](docs/mobile-auto-update-standard.md)
- Release 共通手順: [docs/release-checklist.md](docs/release-checklist.md)
- Enterprise handoff: `README.enterprise.md` が唯一の人間向け手順です。

## License

MIT
