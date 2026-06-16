# AoiTalk

[日本語](/README.md) /
[英語](/docs_i18n/README_en.md)

AoiTalkは、音声認識と複数TTSによる読み上げに対応した、OpenClaw + ClickUp 的なタスク管理・プロジェクト実行ワークスペースです。
会話だけで終わるAIチャットではなく、相談、タスク化、予定確認、作業時間、資料参照、外部ツール実行を同じ文脈で扱うためのアプリです。

## 位置づけ

個人版および公開版のAoiTalkは、声で相談し、AIに作業を分担させ、その結果をタスク管理へ戻すためのワークスペースです。
中心はタスク・プロジェクト管理です。そこにWebUIチャット、音声入力、読み上げ、Discord、ファイル操作、検索、RAG、カレンダー連携を必要に応じて組み合わせます。

## 中核機能

- **音声対応の会話操作**: Whisper、Parakeet、Google Speech、Gemini系の認識器と、VOICEVOX、VOICEROID、A.I.VOICE、CeVIO、AivisSpeech、Nijivoice、Azure TTS、gTTS、Irodori TTS などの読み上げエンジンを構成に応じて利用できます。
- **タスク・プロジェクト管理**: プロジェクト、スペース、タスク、ステータス、期日、繰り返し、発生日、通知、作業時間、レポートをPostgreSQL上で管理します。
- **AI専門エージェント**: project management、filesystem、utility、media、Spotify、skills などの専門ランナーに処理を委譲し、会話だけで終わらない作業を実行します。
- **資料と知識の活用**: ファイラー、Office/PDF読取、QdrantベースのRAG、知識ワークスペース、プロジェクト情報整理を使い、案件資料や作業メモを再利用しやすくします。
- **外部連携**: Google Calendar、MCPサーバー、OpenAI互換LLM、OpenRouter、Gemini、Ollama、SGLangなどを構成に応じて使えます。

## アプリ構成

- **Webアプリ**: `frontend/` にある Next.js UI が現在の主要開発対象です。
- **Backend API**: FastAPI、SQLAlchemy、Alembic、PostgreSQL、WebSocketで構成されています。
- **Mobileアプリ**: `mobile/` は Expo + React Native です。現在はメンテナンス中心で、明示的なモバイル作業以外では変更対象にしません。
- **音声・Botランタイム**: `src/audio/`、`src/tts/`、`src/bot/`、`src/assistant/` が音声入出力、Discord、会話実行を担当します。

## セットアップ

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

## 重要な環境変数

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

## タスク管理API

タスク管理はAoiTalk本体に組み込まれています。`project_management_assistant` は別のMCPサーバーではなく、アプリのバックエンドとAPIを直接操作します。

- `/api/tasks`
- `/api/task-occurrences`
- `/api/time-entries`
- `/api/reports/time`
- `/api/notifications`
- `/api/projects/{id}/notification-settings`

## Specialist Tools

Runtime specialist delegation は `src/llm/runtime_tool_registry.py` で管理しています。

- `project_management_assistant`
- `filesystem_assistant`
- `utility_assistant`
- `media_assistant`
- `spotify_assistant`
- `skills_assistant`

## MCP Servers

MCPサーバー設定は `config/config.yaml` の `mcp` セクションで管理します。

- `utility`
- `web_search`
- `x_search`
- `workspace`
- `memory_rag`
- `os_operations`
- `media`

## READMEの使い分け

- `README.md`: 開発元リポジトリと公開版Publishで使う日本語READMEです。
- `docs_i18n/README_en.md`: 公開版向けの英語READMEです。
- `README.enterprise.md`: 開発リポジトリ側に置くEnterprise出力用の日本語READMEソースです。公開版Publishには含めません。
- `README.enterprise.en.md`: Enterprise出力用の英語READMEソースです。公開版Publishには含めません。
- `scripts/publish_enterprise.ps1`: Enterprise出力先の `README.md` / `docs_i18n/README_en.md` / `ENTERPRISE_PUBLISH_README.md` をEnterprise用テンプレートから生成します。

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
