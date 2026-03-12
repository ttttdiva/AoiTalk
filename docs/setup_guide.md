# AoiTalk セットアップガイド（全プラットフォーム共通）

AoiTalkを最短で動かすための手順を1ファイルに集約しました。Windows/Linux/macOS共通の基本手順に加え、WindowsでのPostgreSQL運用（インストール、接続確認、パスワード再発行など）も本稿で完結します。

## 0. 必要条件とゴール
- Python 3.9 以上
- Windows 10/11、Linux (WSL2含む)、macOS のいずれか
- ネットワークアクセス（LLM/API利用）
- マイク入力デバイス
- （推奨）VOICEVOX などのTTSエンジン
- ゴール: Windowsは `venv\Scripts\python.exe main.py --mode terminal`、Linux/macOSは `venv/bin/python main.py --mode terminal` で起動し、WebUIにアクセス

## 1. クイックセットアップチェックリスト
1. リポジトリをクローンし、作業ディレクトリへ移動
2. 各OS向けセットアップスクリプト／コマンドで依存関係を導入
3. `.env.sample` を `.env` にコピーし、APIキーとPostgreSQL接続情報を設定
4. `config/config.yaml` を環境に合わせて編集
5. PostgreSQL（ローカル）で `aoitalk` ロール + `aoitalk_memory` DB を作成
6. Windowsは `venv\Scripts\python.exe main.py --mode terminal`、Linux/macOSは `venv/bin/python main.py --mode terminal` で起動し、WebUIにアクセス

## 2. リポジトリとベース環境

### 2.1 リポジトリの取得
```bash
git clone https://github.com/ttttdiva/AoiTalk.git
cd AoiTalk
```

### 2.2 依存パッケージ
| OS | 推奨コマンド |
| --- | --- |
| Windows | `setup.bat`（Python venv作成 + pip install + PostgreSQL導入） |
| Linux / macOS | `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt` |
| Linux (システムPython) | `pip install -r requirements-linux.txt`（GPUなし構成など最小依存向け） |

- A.I.VOICE 等 Windows専用 TTS を使う場合は `pip install pythonnet>=3.0.0` を追加。
- にじボイス利用時は `pydub` が必要ですが `requirements.txt` に含まれています。

## 3. `.env` 設定
```bash
cp .env.sample .env
```
主要項目（必須）:
```env
OPENAI_API_KEY=your-openai-api-key
GEMINI_API_KEY=your-gemini-api-key
XAI_API_KEY=your-xai-api-key  # Grok X 検索を使う場合
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=aoitalk
POSTGRES_PASSWORD=your-secure-password
POSTGRES_DB=aoitalk_memory
AOITALK_DISABLE_SEMANTIC_MEMORY=0
```
オプション:
- Spotify: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`
- 天気: `OPENWEATHER_API_KEY`
- にじボイス: `NIJIVOICE_API_KEY`
- ローカル音楽: `AUDIO_PLAYER_DIR`
- Grok設定: `XAI_GROK_MODEL`, `XAI_API_BASE`

## 4. `config/config.yaml` のポイント
- `llm_model`, `default_character`, `device_index` を環境に合わせる
- `speech_recognition.current_engine` で Whisper / Google / Parakeet / Gemini を切替
- `tts_settings` に VOICEVOX/VOICEROID/A.I.VOICE などの実行パスを設定
- `mode_switch.allowed_discord_user_ids` などセキュリティ設定を見直す

## 5. PostgreSQL セットアップ
AoiTalk のデータベースは PostgreSQL を使用します（ベクトル検索はQdrant RAGを使用）。以下はローカル DB 前提の統合手順です。

### 5.1 インストール
- **Windows**: `setup.bat` に PostgreSQL 16 導入タスクあり。失敗した場合は EnterpriseDB 公式インストーラーで 16.x を導入。
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
CREATE ROLE aoitalk LOGIN PASSWORD 'your-secure-password';
CREATE DATABASE aoitalk_memory OWNER aoitalk;
-- pgvectorは不要（Qdrant RAGを使用）
```

### 5.4 接続テスト
```bash
psql -h 127.0.0.1 -p 5432 -U aoitalk -d aoitalk_memory -c "SELECT NOW();"
```
成功後、`.env` に同じ値を記載し、AoiTalk 起動ログで `PostgreSQL connected` を確認。

### 5.5 Windows向け詳細手順
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
5. **ファイアウォール**: Windows Defender → 詳細設定 → 受信規則 → TCP/5432 を許可

### 5.6 パスワードを忘れた場合（Windows）
1. `net stop postgresql-x64-16`
2. `pg_hba.conf` の IPv4/IPv6 行を一時的に `trust` へ
3. `net start postgresql-x64-16`
4. `psql -h 127.0.0.1 -U postgres` でログインし `ALTER USER postgres WITH PASSWORD 'StrongNewPassword!';`
5. `pg_hba.conf` を `md5` に戻し、サービス再起動
6. `.env` の `POSTGRES_PASSWORD` 更新 → `psql -h 127.0.0.1 -U postgres -d postgres` で疎通確認
7. 手順後は `trust` を残さないこと

## 6. 追加オプション
- **MCP (Model Context Protocol)**: `config/mobile_ui.yaml` などを編集し、`mcp.servers` に外部ツールを記述。
- **Discord Bot**: `.env` に `DISCORD_BOT_TOKEN`、`config/config.yaml` で `mode: discord` を設定。
- **スマホ向けWebUI**: `config/mobile_ui.yaml` の `quick_commands` を編集し、実装に合わせて README/本ガイドを更新。
- **Grok 4.1 X検索**: `.env` に `XAI_API_KEY` をセットし、READMEの「Grok X検索ツール」節参照。

