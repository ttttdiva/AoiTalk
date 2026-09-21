# AoiTalk

[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

**音声合成キャラクターと会話しながら、タスク・予定・ナレッジ・ファイルまでまとめて管理できる AI アシスタント。**

AoiTalk は、設定した音声合成キャラクターを AI アシスタントの「顔と声」として使い、テキストや音声での会話から日常・仕事の情報管理までを一つにつなげる AI ワークスペースです。

単に AI の返答を読み上げるだけではありません。会話履歴やプロジェクトの文脈を持ちながら、**タスク管理、予定管理、Docs / Files、ナレッジ検索、Agent、外部ツール実行**を同じ環境で扱えます。

## AoiTalk でできること

### リモートPCのブラウザとWindowsを操作する

操作するWindows PCで `AoiTalk-PC-Bridge.exe` を起動し、AoiTalkの設定「PC接続」で選択します。サーバーとは別PCのログイン済みEdgeやWindowsアプリを操作できる構成です。exe・初回設定・検証範囲は [PC Bridgeガイド](docs/pc_bridge.md) を参照してください。

### 音声合成キャラクターと会話する

キャラクターごとに音声を設定し、AI の返答をそのキャラクターの声で読み上げられます。音声入力にも対応しているため、テキストチャットだけでなく「キャラクターと話す」使い方ができます。

特に、AoiTalk は **VOICEROID2** と **Irodori-TTS** の両方に対応しています。

- **VOICEROID2** — VOICEROID2 のキャラクターを AoiTalk の音声として利用可能
- **Irodori-TTS** — AoiTalk に統合されたローカル TTS。既定の v4.1 Small に加え、v4.1 Anime / v3 VoiceDesign などをキャラクターごとに選択可能
- **その他の TTS** — VOICEVOX、AivisSpeech、A.I.VOICE、CeVIO、Nijivoice、Azure TTS、gTTS、MioTTS など
- **音声認識** — Whisper、Parakeet、Google Speech、Gemini 系 ASR など

Irodori-TTS の詳細は [Irodori-TTS ガイド](docs/irodori_tts.md) を参照してください。

### キャラクターにタスクや予定を管理してもらう

会話と同じワークスペースで、やることや予定を継続的に管理できます。

- タスクの作成・更新・完了管理
- 予定・繰り返し・通知
- 作業時間の記録
- レポートや進捗確認
- 会話やプロジェクト文脈と組み合わせた管理

### ナレッジを蓄積・検索する

チャットだけで情報を使い捨てず、Docs、Files、プロジェクト情報などを蓄積し、必要なときに AI から参照できます。

- Docs / project information
- Files の管理・参照
- Office ファイル・PDF の読み取り
- ClipIngest による情報取り込み
- Qdrant を使ったナレッジ検索
- 会話履歴の検索

### Agent と外部ツールを使う

AoiTalk は一つのチャット画面だけで完結する構成ではなく、Agent や外部サービスと組み合わせて作業を進められます。

- Agent Team / specialist 実行
- Heartbeat / scheduled execution
- MCP
- Web 検索
- Google Calendar
- Discord Bot
- Spotify
- その他の外部ツール連携

利用できる provider / model / tool は設定と実装によって変わります。実際の設定画面、catalog、`src/config_defaults.py` などを正本として確認してください。

### Web と Native Mobile の両方から使う

- **Web** — `frontend/` の Next.js / React アプリ
- **Mobile** — `mobile/` の Expo / React Native ネイティブアプリ。WebView の簡易ラッパーではなく、ローカル DB・同期・状態管理を持つ first-class client です

Mobile の現行仕様は [Mobile Product Contract / Conformance](docs/mobile_product_contract.md) を参照してください。

## 対応 LLM

クラウド API だけでなく、ローカル推論環境も含めて複数の LLM 経路を利用できます。

- OpenAI
- Gemini
- OpenRouter
- Ollama
- SGLang
- OpenAI 互換ローカルサーバー
- その他、設定されている provider / model

## 現在の構成

- **Web / BFF**: `frontend/` — Next.js 16 + React 19。画面と、Drizzle ORM を使う Next.js Route Handler を持ちます。
- **Python runtime / API**: `main.py`、`src/api/` — FastAPI、WebSocket、AI / Agent、音声、外部連携などを担当します。
- **Database**: PostgreSQL。DB スキーマの正本は `alembic/versions/` です。
- **RAG**: Qdrant を利用する検索経路があります。
- **Mobile**: `mobile/` — Expo + React Native の Native client。
- **音声 / Bot**: `src/audio/`、`src/tts/`、`src/bot/`。

文書一覧と「現行仕様 / 設計記録」の区別は [docs/README.md](docs/README.md) を参照してください。

## セットアップ

### Windows

Windows の標準セットアップ入口は `setup.bat` です。

```powershell
setup.bat
run.bat
```

現行 `setup.bat` は Python 3.12 以上、Node.js 22 以上、Git、PostgreSQL / `.env`、Python venv、フロントエンド依存、DB schema、production build を確認・準備します。Windows の音声・TTS 用 extra もセットアップ対象です。

### Linux / WSL

Debian / Ubuntu 系 Linux と WSL では `setup.sh` / `run.sh` を使用します。

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

現行 `setup.sh` は Python 3.12 以上と Node.js 20 以上を要求します。既定では重量級の音声依存を入れません。音声・Irodori-TTS も同時に入れる場合は次のように実行します。

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

外部 DB を使う場合などの詳細は [セットアップガイド](docs/setup_guide.md) を参照してください。

### macOS

Python パッケージ自体は macOS を考慮していますが、`setup.sh` には Debian / Ubuntu 系のパッケージ管理を前提にした箇所があります。macOS では Python 3.12+、Node、PostgreSQL、venv、フロントエンド依存を個別に用意し、`scripts/init_db_schema.py` と production build を実行してください。詳細は [セットアップガイド](docs/setup_guide.md) を参照してください。

## 起動時の既定ポート

| 用途 | 既定 |
| --- | --- |
| FastAPI | `3000` |
| Next.js | `3002` |
| Caddy | `6002` |

Windows `run.bat` と Linux `run.sh` では公開境界の既定が異なります。Linux / WSL は loopback + Caddy 無効が既定で、外部公開時は `run.sh --public --with-caddy` のように TLS 境界を明示します。

## 設定・DB

- `.env` の雛形: `.env.sample`
- Python 依存: `pyproject.toml`
- Web 依存: `frontend/package.json`
- DB migration: `alembic/versions/`
- DB 初期化入口: `scripts/init_db_schema.py`
- Python defaults: `src/config_defaults.py`

固定 password を README に記載しないでください。初回管理者 password はローカル `.env` の `AOITALK_BOOTSTRAP_ADMIN_PASSWORD` を確認します。

## 開発・検証

リポジトリ内の作業規約は `AGENTS.md` が正本です。通常の変更は変更範囲に応じたターゲット検証を行い、`main` push 後の GitHub Actions を確認します。WebUI のユーザー挙動を変える場合は [AI WebUI QA](docs/ai_webui_qa.md) の独立実ブラウザ確認が追加で必要です。

## 配布

この開発リポジトリと公開版リポジトリは分離されています。公開版の同期先は `ttttdiva/AoiTalk` で、`scripts/publish_public.ps1` が公開用 tree を生成します。

- 公開版同期: [docs/public_publish.md](docs/public_publish.md)
- Mobile 自動更新 / APK: [docs/mobile-auto-update-standard.md](docs/mobile-auto-update-standard.md)
- Release 共通手順: [docs/release-checklist.md](docs/release-checklist.md)
- Enterprise handoff: `README.enterprise.md`

## License

MIT
