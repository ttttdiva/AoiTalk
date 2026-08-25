# AoiTalk ログの見方

AoiTalk は大規模な logging framework を使わず、Caddy / Python logging / プロセスリダイレクトと小さな housekeeping でログを整理します。ログはプロジェクトルートの `logs/` 配下に集約されます。

## ディレクトリ構成

| ディレクトリ | 内容 |
|-------------|------|
| `logs/app/` | FastAPI / Python 本体。起動ごとに `app_YYYYMMDD_HHMMSS.log` を作成し、`latest.log` に実パスを記録 |
| `logs/web/` | HTTP 境界: Next.js frontend、Caddy access / runtime |
| `logs/models/` | ローカル LLM: llama.cpp、exo、mlx_lm、sglang |
| `logs/startup/` | 起動計測 `startup_timing_<run_id>.jsonl` |
| `logs/desktop/` | Tauri デスクトップから起動した backend |
| `logs/ops/` | DDNS watchdog など運用系 |
| `logs/discord/` | Discord bot（従来どおり） |

対象外（移動しない）: `workspaces/**/logs`（App job）、`logs/feedback_logs.jsonl`、`debug_audio/`。

## 各ログの意味

### アプリ本体 (`logs/app/`)

- **アクティブ**: 起動中の `app_YYYYMMDD_HHMMSS.log`（起動ごとに新規）
- **latest**: `logs/app/latest.log` は symlink ではなく、実ファイルパスのテキスト
- **保持**: 14 日超または合計 20 ファイル超の古いファイルを削除（最大 20 ファイル）

Python の通常ログ、起動フロー、多くの診断メッセージはここを見ます。

### Frontend (`logs/web/frontend.log`)

- **アクティブ**: `frontend.log`（Next.js `npm run start` の stdout/stderr）
- **履歴**: 起動時に既存があれば `frontend-YYYYMMDDTHHMMSS.log` へ rename してから新規作成（起動時削除はしない）
- **保持**: アクティブ 1 + 世代 10

ビルド失敗やポート待ちエラー時の tail メッセージもこのファイルを参照します。

### Caddy access (`logs/web/caddy-access.log`)

- **内容**: リバースプロキシへ入った HTTP リクエスト（JSON）。Authorization / Cookie / Set-Cookie / Proxy-Authorization は記録しません
- **ロール**: Caddy `roll_size 10MiB`、`roll_keep 5`
- **Windows**: Caddy は非圧縮ロール（`roll_uncompressed`）。gzip と古いファイル整理は AoiTalk housekeeping が担当

ブラウザからの到達、パス、ステータス、遅延の調査に使います。AoiTalk は通常このファイルを open/tail しません。

### Caddy runtime (`logs/web/caddy-runtime.log`)

- **内容**: Caddy プロセス自身のログ（Caddyfile グローバル `log` の `output file` へ永続化。config/load 前の stderr は native コンソールへ。service_manager は stdout/stderr をリダイレクトしない）
- **ロール**: `roll_size 5MiB`、`roll_keep 5`（access と同様の leftover 処理）

Caddy 起動失敗時は `caddy-runtime.log` の末尾がエラーメッセージに含まれます。証明書、リスナー、設定エラーの調査に使います。

### ローカル LLM (`logs/models/*.log`)

| ファイル | サーバー |
|---------|---------|
| `llama_cpp.log` | llama-server |
| `exo.log` | exo |
| `mlx_lm.log` | mlx_lm |
| `sglang_server.log` / `sglang_server_error.log` | SGLang |

- **ロール**: 起動前に 10MiB 超なら timestamp 付きへ rename
- **保持**: アクティブ 1 + 世代 3

モデルロード失敗、VRAM、DLL エラーはまず `llama_cpp.log` を確認します。
AoiTalk が管理する llama-server は、起動中の親コンソールにも stdout/stderr を
リアルタイムに mirror します。Windows でも llama-server 専用の空のコンソールは
開かず、AoiTalk を起動したコンソールへ表示します。永続ログの正本は引き続き
`logs/models/llama_cpp.log` です。
`exo` と `mlx_lm` は今回の mirror 対象外で、従来どおり各ログファイルへ
リダイレクトされます。

### 起動計測 (`logs/startup/`)

- **ファイル**: `startup_timing_<run_id>.jsonl`（run ごと新規）
- **保持**: 最大 20 ファイル

起動が遅いフェーズの特定に使います。環境変数 `AOITALK_STARTUP_TIMING_PATH` で上書き可能です。

### Discord (`logs/discord/`)

- **アクティブ**: 起動ごとの `bot_YYYYMMDD_HHMMSS.log`
- **latest**: `latest.log` ポインタ（discord 専用、削除しない）
- **保持**: 最大 20 ファイル（latest ポインタ除く）

### Desktop (`logs/desktop/desktop-tauri-backend.log`)

Tauri から起動した `main.py` の stdout/stderr。10MiB 超で rotate（housekeeping が世代整理）。

### DDNS (`logs/ops/ddns_update.log`)

1MB 超で `.1` へローテーション（watchdog スクリプト側）。

## 環境変数（Caddy）

| 変数 | 既定（相対は project root 基準） |
|------|--------------------------------|
| `AOITALK_CADDY_ACCESS_LOG` | `logs/web/caddy-access.log` |
| `AOITALK_CADDY_RUNTIME_LOG` | `logs/web/caddy-runtime.log` |
| `AOITALK_CADDY_LOG_ROLL_EXTRA` | Windows native のみ `roll_uncompressed`（service_manager が設定） |

Docker Compose では絶対パス `/logs/web/...` を渡します。

## housekeeping

`src/utils/log_housekeeping.py` が起動時（`main.py` と `_start_services()` の早い段階）に fail-open で実行されます。

- いま書き込み中のアクティブファイルは削除しない
- Windows でロック中（PermissionError / WinError 32）のファイルはスキップし、次回に回す
- Caddy の gzip-after-rotate が Windows で失敗しても、leftover の gzip・削除・keep 上限で無制限蓄積を防ぐ

正本のパス定義は `src/utils/log_layout.py` です。
