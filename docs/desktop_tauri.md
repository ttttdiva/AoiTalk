# Tauri Desktop MVP

## 目的

`desktop/` は、既存の AoiTalk Next.js / FastAPI ランタイムを包む Tauri v2 ベースのデスクトップシェルです。
Next.js を静的 export して埋め込む構成ではなく、Tauri の WebView がローカルで起動した `http://127.0.0.1:3002/chat` を表示します。

## 初回MVPの前提

- `setup.bat` または `setup.sh` を実行済みであること。
- Python venv、Node.js、`frontend/` dependencies が準備済みであること。
- PostgreSQL が起動済みであること。
- ルート `.env` が設定済みであること。

## 起動手順

```powershell
cd desktop
npm ci
npm run dev
```

別の checkout を AoiTalk repo root として使う場合は、`AOITALK_ROOT` を指定します。

```powershell
$env:AOITALK_ROOT = "C:\path\to\AoiTalk"
npm run dev
```

Linux / macOS:

```bash
AOITALK_ROOT=/path/to/AoiTalk npm run dev
```

## Windowsの注意

- PostgreSQL サービスが起動していることを確認してください。
- 初回は `setup.bat` を先に実行してください。
- Tauri は `venv\Scripts\python.exe main.py` を repo root を cwd にして起動します。
- デスクトップ起動時は `AOITALK_DESKTOP=1`、`AOITALK_WEB_AUTO_OPEN=false`、`AOITALK_SKIP_CADDY=true` が付与され、Caddy は起動しません。

## Linux / macOSの注意

- `venv/bin/python` が存在することを確認してください。
- PostgreSQL、Qdrant、音声デバイス、ローカルLLMなどは既存の AoiTalk 手順に従って準備してください。
- Tauri が起動した `main.py` は process group として扱い、終了時に best-effort で子孫プロセスも停止します。

## ログ

Tauri から起動した `main.py` の stdout / stderr は `logs/desktop/desktop-tauri-backend.log` に追記されます。
Next.js の既存サービスログは `logs/web/frontend.log` も参照してください。

## 制限

- 今回の MVP では Python、Node.js、PostgreSQL をデスクトップアプリに同梱しません。
- Next.js の static export 方式ではありません。
- Tauri installer 単体で完全動作する配布版ではありません。
- 開発者またはローカル利用者向けに、既存セットアップ済み環境を前提にしています。

## 次フェーズ

- PyInstaller などによる Python API sidecar 化。
- Next.js standalone server の sidecar 化。
- PostgreSQL の外部サービス検出と初期化 wizard。
- auto updater、signing、installer 整備。
