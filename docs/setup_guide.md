# AoiTalk セットアップガイド（全プラットフォーム共通）

AoiTalkを最短で動かすための手順を1ファイルに集約しました。Windows/Linux/macOS共通の基本手順に加え、WindowsでのPostgreSQL運用（インストール、接続確認、パスワード再発行など）も本稿で完結します。

## 2026-03-21 更新
- WebUI は `frontend/` の **Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui** です。
- CRUD API（認証・タスク・会話・プロジェクト等）は Next.js Route Handler + Drizzle ORM で直接 PostgreSQL に接続します。
- Python FastAPI (port 3000) は AI/音声/エージェント機能専用です。WebUIの配信は行いません。
- Next.js (port 3002) がフロントエンドを配信します。Python APIは port 3000 で起動し、Next.js が `/api/python/*` でプロキシします。
- ローカルタスク/予定/工数管理は PostgreSQL を正本として扱います。
- PostgreSQL スキーマは Alembic で管理します。初回起動前に `alembic upgrade head` を実行してください。
- Node.js 22+ はフロントエンド実行に必須です（Next.js サーバー）。

## 0. 必要条件とゴール
- Python 3.10 以上
- Node.js 22 以上（フロントエンドのビルド/テスト時のみ）
- Windows 10/11、Linux (WSL2含む)、macOS のいずれか
- ネットワークアクセス（LLM/API利用）
- マイク入力デバイス
- （推奨）VOICEVOX などのTTSエンジン
- ゴール: Python API + Next.js の2プロセスを起動し、`http://127.0.0.1:3002` でWebUIにアクセス

## 1. クイックセットアップチェックリスト
1. リポジトリをクローンし、作業ディレクトリへ移動
2. 各OS向けセットアップスクリプト／コマンドで依存関係を導入
3. `frontend/` で `npm ci` を実行（ビルドは不要、devサーバーで起動）
4. `.env.sample` を `.env` にコピーし、APIキーとPostgreSQL接続情報を設定
5. `config/config.yaml` を環境に合わせて編集
6. PostgreSQL（ローカル）で `aoitalk` ロール + `aoitalk_memory` DB を作成
7. `alembic upgrade head` を実行
8. `run.bat` を実行（Python API + Next.js が同時起動）
9. ブラウザで `http://127.0.0.1:3002` にアクセス

## 2. リポジトリとベース環境

### 2.1 リポジトリの取得
```bash
git clone https://github.com/ttttdiva/41_AoiTalk.git
cd 41_AoiTalk
```

### 2.2 依存パッケージ
| OS | 推奨コマンド |
| --- | --- |
| Windows | `setup.bat`（対話式 `.env` 作成 + PostgreSQL/Python/Node.js導入 + DB初期化 + ビルド） |
| Linux / macOS | `python3 -m venv venv && source venv/bin/activate && pip install -e ".[audio,test]"` |

- Irodori-TTS を使う場合は `pip install -e ".[audio,irodori]"` と `pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" descript-audiotools argbind julius pystoi torch-stoi flatten-dict markdown2 randomname importlib-resources` を追加。推論 runtime は AoiTalk に同梱され、重みは初回合成時に Hugging Face から自動取得されます。
- A.I.VOICE / VOICEROID / CeVIO など Windows専用 TTS を使う場合は `pip install -e ".[audio,windows]"` を追加。
- 依存定義の一次情報は `pyproject.toml`。

### 2.3 フロントエンドの依存インストール
```bash
cd frontend
npm ci
cd ..
```

- Next.js は開発サーバー（`npm run dev -- -p 3002`）で起動します。ビルド不要。
- 本番環境では `npm run build && npm start -- -p 3002` を使用。
- `.env` はプロジェクトルートで管理。`run.bat` が起動時に `frontend/.env` へ自動コピーします。

## 3. `.env` 設定
```bash
cp .env.sample .env
```
主要項目:
```env
NEXTAUTH_SECRET=change-this-nextauth-session-secret
AOITALK_WEB_AUTH_SECRET=change-this-fastapi-session-secret
INTERNAL_API_KEY=change-this-internal-proxy-key

POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=aoitalk
POSTGRES_PASSWORD=ChangeMe!
POSTGRES_DB=aoitalk_memory

OPENROUTER_API_KEY=your-openrouter-api-key  # llm_provider: openrouter の場合
GEMINI_API_KEY=your-gemini-api-key          # llm_provider: gemini の場合
OPENAI_API_KEY=your-openai-api-key          # llm_provider: openai またはOpenAI依存ツールの場合
OLLAMA_MODEL=gemma4:e4b                     # llm_provider: ollama で標準モデルを変える場合
XAI_API_KEY=your-xai-api-key                # Grok X 検索を使う場合
```
オプション:
- 認証: `AOITALK_JWT_SECRET`
- Discord Bot: `DISCORD_BOT_TOKEN`
- OpenRouter設定: `OPENROUTER_SITE_URL`
- Spotify: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
- 天気: `OPENWEATHER_API_KEY`
- にじボイス: `NIJIVOICE_API_KEY`
- ローカル音楽: `AUDIO_PLAYER_DIR`
- ファイラー絶対パス閲覧: `FILER_ROOT_PATH`, `FILER_VIDEO_THUMBNAIL_CACHE`
- Grok設定: `XAI_GROK_MODEL`, `XAI_API_BASE`

WebUIのログインユーザー/パスワードは `.env` ではなく PostgreSQL の `users` テーブルで管理します。
固定ログイン用のユーザー名/パスワード環境変数は現行実装では使用しません。

通常のローカル起動では、`OPENROUTER_BASE_URL`、`OPENROUTER_APP_NAME`、`PYTHON_API_URL`、`OLLAMA_BASE_URL`、`VOICEVOX_HOST`、`QDRANT_HOST` のような既定URL/ホストは `.env` に書く必要はありません。標準値はコード側で持ち、サービス側URLやポートを意図的に変える時だけ `.env.sample` のコメントアウトされたoverrideを有効化してください。


## 4. `config/config.yaml` のポイント
- `llm_model`, `default_character`, `device_index` を環境に合わせる
- `speech_recognition.current_engine` で Whisper / Google / Parakeet / Gemini を切替
- `tts_settings` に VOICEVOX/VOICEROID/A.I.VOICE/Irodori-TTS などの実行パスや参照音声フォルダを設定
- `runtime_feature_permissions.allowed_discord_user_ids` などセキュリティ設定を見直す

## 5. PostgreSQL セットアップ
AoiTalk のデータベースは PostgreSQL を使用します（ベクトル検索はQdrant RAGを使用）。以下はローカル DB 前提の統合手順です。

### 5.1 インストール
- **Windows**: 管理者権限を持つユーザーで `setup.bat` を実行します。必要に応じてUACが表示され、次の情報を対話入力します。
  - 初回のみ: PostgreSQLホスト、ポート、AoiTalk用DB名、DBユーザー
  - 任意: Gemini APIキー（空欄なら後から `.env` に設定可能）
  - PostgreSQL管理者 `postgres` のパスワード（確認入力あり、`.env` には保存しない）
- PostgreSQL未導入時は、入力した管理者パスワードを使ってPostgreSQL 16を無人インストールし、そのままサービス起動待ち、AoiTalk用DB作成、スキーマ初期化まで続行します。
- AoiTalk用DBパスワードとWeb認証シークレットは自動生成して `.env` に保存します。既存の `.env` がある場合は接続設定を維持し、空の認証シークレットだけを補完します。
- セットアップに失敗した場合は、表示されたエラーを解消して同じ `setup.bat` を再実行できます。処理は再実行可能です。
- **Linux/macOS**: OS標準パッケージを利用。

### 5.2 サービス/デーモン確認と自動起動設定

#### Windows
```powershell
# サービス状態確認
Get-Service -Name "postgresql*"
# 手動起動（管理者権限が必要）
Start-Service -Name "postgresql-x64-16"
```

**⚠️ 重要: 自動起動設定（推奨）**

PostgreSQLサービスをWindows起動時に自動起動するよう設定すると、AoiTalkを**管理者権限なしで実行**できるようになります。以下のコマンドを**管理者権限のPowerShell**で一度だけ実行してください：

```powershell
# 自動起動を有効化（管理者権限で実行）
Set-Service -Name "postgresql-x64-16" -StartupType Automatic
```

または、GUIで設定する場合：
1. `Win + R` → `services.msc` を開く
2. `postgresql-x64-16` を探してダブルクリック
3. 「スタートアップの種類」を「自動」に変更
4. 「OK」をクリック

設定後はPC再起動時にPostgreSQLが自動で起動するため、AoiTalkを通常権限で実行できます。

#### Linux/macOS
```bash
sudo systemctl status postgresql
# 自動起動を有効化
sudo systemctl enable postgresql
```

### 5.3 初期ユーザーとDB
```powershell
& "C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe" -h 127.0.0.1 -U postgres
```
```sql
CREATE ROLE aoitalk LOGIN PASSWORD 'ChangeMe!';
CREATE DATABASE aoitalk_memory OWNER aoitalk;
-- pgvectorは不要（Qdrant RAGを使用）
```

### 5.4 接続テスト
```bash
psql -h 127.0.0.1 -p 5432 -U aoitalk -d aoitalk_memory -c "SELECT NOW();"
```
成功後、`.env` に同じ値を記載し、AoiTalk 起動ログで `PostgreSQL connected` を確認。

### 5.5 Alembic マイグレーション
```bash
alembic upgrade head
```

- 初回起動前、またはスキーマ変更後は Alembic を先に適用します。
- `alembic/` と `alembic.ini` がローカルタスク/予定/工数管理のスキーマ定義です。

### 5.6 Windows向け詳細手順
1. **サービス名の確認/停止**
   ```powershell
   net stop postgresql-x64-16
   ```
2. **`postgresql.conf` と `pg_hba.conf`**
   - `listen_addresses = 'localhost,127.0.0.1'`
   - `pg_hba.conf` に以下を維持
     ```
     host    all    all    127.0.0.1/32    md5
     host    all    all    ::1/128         md5
     ```
3. **サービス再起動**
   ```powershell
   net start postgresql-x64-16
   ```
4. **psql のパス**: `C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe`
5. **ファイアウォール**: TCP/5432 をLANへ許可しない。PostgreSQL は localhost 専用で使う。
   現状確認と修正は以下で行う。
   ```powershell
   .\scripts\harden_local_security.ps1
   .\scripts\harden_local_security.ps1 -Apply
   ```

### 5.7 セキュリティ hardening
AoiTalk は会話本文・OAuthトークン・APIキーなどを扱うため、ローカル環境でも以下を標準にします。

- PostgreSQL / Qdrant は localhost でのみ待ち受ける
- `.env` は AoiTalk 実行ユーザー、SYSTEM、Administrators 以外に読ませない
- DB内の OAuth トークン、APIキー、会話本文、履歴、summary、context、案件DBレコード、knowledge chunk はアプリ層で暗号化する
- 暗号化データキーは `.env` に置かず、Windows では DPAPI 保護ファイルから取得する
- Qdrant payload の本文も暗号化され、検索結果は AoiTalk サーバ内で復号される

既存DBの平文データは dry-run で件数を確認してから暗号化します。

```powershell
venv\Scripts\python.exe scripts\encrypt_sensitive_data.py
venv\Scripts\python.exe scripts\encrypt_sensitive_data.py --apply
venv\Scripts\python.exe scripts\audit_plaintext_sensitive_data.py
```

### 5.8 パスワードを忘れた場合（Windows）
1. `net stop postgresql-x64-16`
2. `pg_hba.conf` の IPv4/IPv6 行を一時的に `trust` へ
3. `net start postgresql-x64-16`
4. `psql -h 127.0.0.1 -U postgres` でログインし `ALTER USER postgres WITH PASSWORD 'StrongNewPassword!';`
5. `pg_hba.conf` を `md5` に戻し、サービス再起動
6. `.env` の `POSTGRES_PASSWORD` 更新 → `psql -h 127.0.0.1 -U postgres -d postgres` で疎通確認
7. 手順後は `trust` を残さないこと

## 6. 追加オプション
- **MCP (Model Context Protocol)**: `config/config.yaml` の `mcp.servers` を編集し、外部ツール連携を設定。
- **Discord Bot**: `.env` に `DISCORD_BOT_TOKEN`、`config/config.yaml` の `runtime_features.discord_bot` / `discord_text` / `discord_vc_input` / `discord_vc_output` を用途に合わせて設定。
- **スマホ向けWebUI**: `config/mobile_ui.yaml` の `quick_commands` を編集し、実装に合わせて README/本ガイドを更新。
- **Grok 4.1 X検索**: `.env` に `XAI_API_KEY` をセットし、READMEの「Grok X検索ツール」節参照。
- **ローカルタスク管理**: [docs/task_workspace_rebuild.md](docs/task_workspace_rebuild.md) を参照。

