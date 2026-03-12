# AoiTalk - AIキャラクター音声アシスタント

マイク・WebUI・Discord Botから音声やテキストを受け取り、音声認識 → LLMによる応答生成 → 音声合成で返答を行う、キャラクター性を持った音声アシスタントフレームワークです。

## 🌟 特徴

- ✅ **ローカルマイク / WebUI / Discord Bot** どちらにも対応
- 🎙️ **モジュラー音声認識** - Whisper、Google Speech、Parakeet、Geminiに対応
- 🤖 **マルチLLMプロバイダー** - OpenAI / Gemini / SGLang / Claude CLI / Gemini CLI / Codex CLI に対応
- 🗣️ **複数TTS対応** - VOICEVOX / VOICEROID / A.I.VOICE / CeVIO AI / AivisSpeech / にじボイス / Qwen3-TTS
- 👤 **キャラクタープロファイル** - YAMLによる詳細な個性制御
- 🔌 **MCP (Model Context Protocol) サポート** - CLIネイティブ委譲による外部ツール連携
- 📋 **ClickUp連携** - MCP経由でタスク管理
- 🧠 **記憶システム** - PostgreSQLによる会話履歴保存・Qdrant RAGによる意味検索
- 🎯 **高度なエコーキャンセレーション** - 自己音声の誤認識を防止
- 🔊 **高速割り込み対応** - 0.2秒以内の高速応答で音声割り込み可能
- 🌍 **Web検索・天気情報** - リアルタイム情報取得
- 📰 **X（旧Twitter）検索** - Grok のX Searchツールで最新ポストを要約
- 📱 **モバイル最適化WebUI** - スマホ画面でもチャット＋クイックコマンドを片手操作
- 🎯 **スキルシステム** - プロンプトテンプレートベースのカスタムスキル
- 💓 **Heartbeat機能** - 定期チェック条件のLLM評価・通知システム
- 🧩 **推論エンジン** - 複雑なタスクの段階的推論

## 📋 動作要件

- **Python 3.11以上**
- Windows / Linux (WSL2) / macOS
- マイク入力デバイス（音声入力する場合）
- PostgreSQL 14以上（会話履歴・ユーザー管理用）
- Qdrant（RAG検索用、オプション）
- TTSエンジン（VOICEVOX推奨）

## 🚀 セットアップ

詳細な手順は **[docs/setup_guide.md](docs/setup_guide.md)** を参照してください。

### 1. リポジトリのクローン

```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
```

### 2. 依存関係のインストール

```bash
# Windows（推奨）
setup.bat

# Linux/macOS
python3 -m venv venv
source venv/bin/activate
pip install -e ".[audio,test]"

# Qwen3-TTS使用時
pip install -e ".[audio,qwen3]"

# Windows専用TTS（VOICEROID/A.I.VOICE/CeVIO）使用時
pip install -e ".[audio,windows]"
```

### 3. 環境変数の設定（.env）

```bash
cp .env.sample .env
```

主要な設定項目：

```env
# 必須設定
OPENAI_API_KEY=your-openai-api-key
GEMINI_API_KEY=your-gemini-api-key

# PostgreSQL（必須）
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=aoitalk
POSTGRES_PASSWORD=your-password
POSTGRES_DB=aoitalk_memory

# オプション
XAI_API_KEY=your-xai-api-key           # Grok X検索用
NIJIVOICE_API_KEY=your-nijivoice-key   # にじボイス用
OPENWEATHER_API_KEY=your-key           # 天気情報用
DISCORD_BOT_TOKEN=your-token           # Discord Bot用

# Qdrant RAG（オプション）
QDRANT_HOST=localhost
QDRANT_PORT=6333
```

詳細は `.env.sample` を参照してください。

### 4. PostgreSQLセットアップ

```sql
CREATE ROLE aoitalk LOGIN PASSWORD 'your-password';
CREATE DATABASE aoitalk_memory OWNER aoitalk;
```

### 5. 設定ファイルの編集（config/config.yaml）

```yaml
llm_provider: "gemini"       # LLMプロバイダー（下記参照）
llm_model: "gemini-3-flash-preview"
default_character: "ずんだもん"
device_index: 0              # マイクデバイスのインデックス

speech_recognition:
  current_engine: "whisper"  # whisper, google, parakeet, gemini

tts_settings:
  voicevox:
    host: 127.0.0.1
    port: 50021
```

### LLMプロバイダー一覧（`llm_provider`）

| プロバイダー | 説明 | 必要な設定 |
| --- | --- | --- |
| `openai` | OpenAI API（GPT-4o等）。デフォルト | `OPENAI_API_KEY` |
| `gemini` | Google Gemini API | `GEMINI_API_KEY` |
| `sglang` | ローカルLLM（OpenAI互換API）。HuggingFaceモデル等 | `SGLANG_BASE_URL` 等（.env参照） |
| `claude-cli` | Anthropic Claude Code CLI経由 | `claude` コマンドがPATHに必要 |
| `gemini-cli` | Google Gemini CLI経由 | `gemini` コマンドがPATHに必要 |
| `codex-cli` | OpenAI Codex CLI経由 | `codex` コマンドがPATHに必要 |

**設定例：**

```yaml
# OpenAI APIを使う場合
llm_provider: openai
llm_model: gpt-4o

# Gemini APIを使う場合
llm_provider: gemini
llm_model: gemini-3-flash-preview

# ローカルLLM（SGLang）を使う場合
llm_provider: sglang
llm_model: default
sglang:
  auto_start: true
  model: default
  port: 30000

# Claude CLI経由で使う場合
llm_provider: claude-cli
llm_model: claude-sonnet-4-20250514
```

## 🎮 使い方

### 基本的な起動

```bash
# Windows（推奨）
run.bat

# 直接実行する場合
venv\Scripts\python.exe main.py --mode terminal

# Discord Botとして起動
venv\Scripts\python.exe main.py --mode discord

# 音声デバイスのリスト表示
python test.py
```

起動後、ブラウザで `http://127.0.0.1:3000` にアクセスしてWebUIを使用できます。

### モード一覧

| モード | 説明 |
| --- | --- |
| `terminal` | テキスト入力専用（WebUI経由） |
| `voice_chat` | マイク入力対応（WebUI経由） |
| `discord` | Discord Bot として動作 |

### Discord Bot機能

1. `.env` に `DISCORD_BOT_TOKEN` を設定
2. `--mode discord` で起動

**主なスラッシュコマンド:**
- `/join` - ボイスチャンネルに参加
- `/leave` - ボイスチャンネルから退出
- `/character` - キャラクター切り替え
- `/help` - ヘルプ表示

詳細は [docs/DISCORD_BOT_SETUP.md](docs/DISCORD_BOT_SETUP.md) を参照。

### MCP (Model Context Protocol)

CLIネイティブ委譲方式で外部ツールと連携します。MCPサーバーは `scripts/` 配下にあり、`config.yaml` の `mcp` セクションで設定します。

```yaml
mcp_enabled: true
mcp:
  servers:
    clickup:
      windows:
        command: "venv\\Scripts\\python.exe"
        args: ["scripts/clickup_mcp_server.py"]
      linux:
        command: "venv/bin/python"
        args: ["scripts/clickup_mcp_server.py"]
      env:
        CLICKUP_API_KEY: "${CLICKUP_API_KEY}"
```

**組み込みMCPサーバー：**

| サーバー | 説明 |
| --- | --- |
| `clickup` | ClickUpタスク管理 |
| `utility` | 天気情報等のユーティリティ |
| `web_search` | Web検索 |
| `x_search` | X（旧Twitter）検索 |
| `workspace` | ワークスペース管理 |
| `memory_rag` | RAGメモリ検索 |
| `os_operations` | OS操作（ファイル・コマンド） |
| `media` | メディアブラウザ |

### Grok X検索ツール

`.env` に `XAI_API_KEY` を設定すると、X（旧Twitter）の最新投稿を検索・要約できます。

## 👤 キャラクター設定

`config/characters/` ディレクトリにYAMLファイルを作成：

```yaml
name: "キャラクター名"
personality:
  details: |
    あなたは〇〇なキャラクターです。
    話し方の特徴：～なのだ、～だよ
  greeting: "こんにちは！"

voice:
  engine: "voicevox"  # voicevox, voiceroid, aivoice, cevio, aivisspeech, nijivoice, qwen3
  speaker_id: 3
  parameters:
    speed: 1.0
    pitch: 0.0

recognition_aliases:
  - "キャラ名"
  - "ニックネーム"
```

### 対応TTSエンジン

| エンジン | プラットフォーム | 備考 |
| --- | --- | --- |
| VOICEVOX | 全OS | 推奨、無料 |
| AivisSpeech | 全OS | VOICEVOX互換 |
| VOICEROID | Windows | 有料ソフト |
| A.I.VOICE | Windows | 有料ソフト |
| CeVIO AI | Windows | 有料ソフト |
| にじボイス | 全OS | クラウドAPI |
| Qwen3-TTS | 全OS | ローカルAI TTS |

キャラクター一覧は [docs/reference/characters/](docs/reference/characters/) を参照。

## 🔧 アーキテクチャ

### 処理フロー

1. **入力** → マイク / WebUI / Discord からテキスト・音声を受信
2. **音声認識** → モジュラーエンジンで高精度文字起こし
3. **LLM処理** → マルチプロバイダーLLMで応答生成（ツール・MCP使用可能）
4. **音声合成** → キャラクターボイスで音声化
5. **出力** → 0.2秒以内の高速割り込み対応再生

### ディレクトリ構成

```
AoiTalk/
├── config/
│   ├── config.yaml              # メイン設定
│   ├── characters/              # キャラクター設定
│   ├── skills/                  # スキル定義（プロンプトテンプレート）
│   ├── heartbeats/              # Heartbeat定期チェック定義
│   ├── profiles/                # プロファイル設定
│   └── mobile_ui.yaml           # WebUIクイックコマンド
├── src/
│   ├── agents/                  # LLMエージェント実装
│   ├── api/                     # FastAPI Webサーバー
│   ├── assistant/               # アシスタントコア
│   ├── audio/                   # 音声処理・ASR
│   │   └── engines/             # 音声認識エンジン
│   ├── bot/                     # Discord Bot
│   ├── heartbeat/               # Heartbeat定期チェック
│   ├── llm/                     # LLMプロバイダー統合
│   ├── memory/                  # メモリシステム（PostgreSQL）
│   ├── mode_switch/             # モード切り替え
│   ├── models/                  # データモデル
│   ├── rag/                     # RAG検索（Qdrant）
│   ├── reasoning/               # 推論エンジン
│   ├── services/                # サービス層
│   ├── session/                 # セッション管理
│   ├── skills/                  # スキルシステム
│   ├── tools/                   # ツール群
│   ├── tts/                     # 音声合成
│   │   └── engines/             # TTSエンジン
│   ├── utils/                   # ユーティリティ
│   └── web/                     # Webフロントエンド
├── scripts/                     # MCPサーバー・ユーティリティスクリプト
├── tests/                       # テスト
├── docs/                        # ドキュメント
├── main.py                      # エントリーポイント
├── pyproject.toml               # 依存関係定義
└── run.bat                      # Windows起動スクリプト
```

## 🐛 トラブルシューティング

### 音声が認識されない
- `python test.py` でデバイス番号を確認
- `config.yaml` の `device_index` を適切に設定

### VOICEVOXが起動しない
- VOICEVOXを手動で起動してから実行
- `.env` のパスが正しいか確認

### PostgreSQL接続エラー
- サービスが起動しているか確認: `Get-Service -Name "postgresql*"`
- `.env` の接続情報を確認
- 詳細は [docs/setup_guide.md](docs/setup_guide.md) を参照

### LLM応答が生成されない
- `.env` にAPIキーが設定されているか確認
- ネットワーク接続を確認

## 📝 ドキュメント

- [セットアップガイド](docs/setup_guide.md) - 詳細な環境構築手順
- [Discord Bot設定](docs/DISCORD_BOT_SETUP.md) - Discord Bot の設定方法
- [Docker環境](docs/docker_setup.md) - Docker での実行方法
- [Qwen3-TTS自動登録](docs/qwen3_auto_registration.md) - Qwen3-TTSの使い方
- [RAGインデックス構築](docs/rag-index-guide.md) - RAGインデックスの作成方法

## 📄 ライセンス

MIT License

## 🤝 貢献

プルリクエストを歓迎します！バグ報告や機能提案は [Issues](https://github.com/ttttdiva/AoiTalk/issues) へ。
