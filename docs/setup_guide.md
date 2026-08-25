# AoiTalk セットアップガイド

この文書は現在の setup/run scripts を基準にした環境構築手順です。依存バージョンや初期化処理をこの文書だけから手作業で再現せず、まず各 platform の正規スクリプトを使ってください。

## 正本

| 項目 | 正本 |
| --- | --- |
| Python version / dependencies | `pyproject.toml` |
| Web dependencies / scripts | `frontend/package.json` |
| Windows setup | `setup.bat`, `scripts/setup_env_db.ps1` |
| Linux/WSL setup | `setup.sh` |
| Windows start | `run.bat` |
| Linux/WSL start | `run.sh` |
| DB schema | `alembic/versions/` |
| DB initialization | `scripts/init_db_schema.py` |
| runtime defaults | `src/config_defaults.py` |
| environment template | `.env.sample` |

## アーキテクチャ上の注意

AoiTalk は Next.js BFF と FastAPI の両方を使います。

- `frontend/src/app/api/**` には Drizzle ORM を使う Next.js Route Handler があります。
- `src/api/**` には FastAPI route があります。AI/音声/agent だけでなく、機能によっては task 等の application API も存在します。
- PostgreSQL schema の正本は Alembic です。Drizzle schema は Next.js が参照する定義で、`scripts/check_schema_drift.py` と CI で実 DB と照合します。
- FastAPI の OpenAPI 型生成対象と Next.js BFF の型は別です。詳細は [openapi_typegen.md](openapi_typegen.md) を参照してください。

「Next.js が全 CRUD」「FastAPI は AI 専用」のような境界を前提に新規実装しないでください。

## Windows

### 要件

現行 `setup.bat` は次を確認し、足りない場合は導入を試みます。

- Python 3.12 以上
- Node.js 22 以上
- Git
- PostgreSQL とアプリ用 DB / `.env`

管理者権限が必要な処理は `setup.bat` 自身が昇格して実行します。

### セットアップ

```powershell
setup.bat
```

主な処理:

1. `.env` と PostgreSQL を `scripts/setup_env_db.ps1` で準備
2. Python 3.12+ の venv を作成 / 古い venv を再作成
3. NVIDIA GPU がある Windows では、公式 cu128 index から `torch==2.10.0` / `torchaudio==2.10.0` を明示導入し、GPU/driver を検証
4. `.[audio,windows,test,irodori,yomi-linter]` と Irodori 用 DACVAE / runtime dependencies を導入
5. 全 Python 依存導入後に PyTorch CUDA build / device を再検証（NVIDIA 環境で CPU-only なら失敗）
6. `frontend/` で `npm ci`
7. `scripts/init_db_schema.py` で DB schema を初期化 / migration
8. `npm run build:production` で production Next.js build を生成

NVIDIA Windows の PyTorch 正常系は CUDA 12.8 (`cu128`) です。CUDA 対応 PyTorch を導入しても、Whisper の既定 device は CPU、`fp16` は false のままです。一方、BGE-M3 embedding と Reranker は CUDA を正常系とし、CUDA が使えない場合だけ設定に従って CPU fallback します。Linux/WSL は `setup.sh` の既存方針を使い、Windows 用 cu128 index を流用しません。llama.cpp/llama-server の GPU offload は PyTorch と独立しています。

Irodori の依存理由と個別 smoke は [irodori_tts.md](irodori_tts.md) を参照してください。

### 起動

```powershell
run.bat
```

`run.bat` は `venv\Scripts\python.exe` が Python 3.12+ であることを確認し、ルート `.env` を `frontend/.env` へコピーして `main.py` を起動します。終了コード `42` は restart 契約です。

既定ポート:

- FastAPI: `3000`
- Next.js: `3002`
- Caddy: `6002`

ポートは `AOITALK_WEB_PORT`, `AOITALK_NEXT_PORT`, `AOITALK_CADDY_PORT` で変更できます。

## Linux / WSL

`setup.sh` は Debian / Ubuntu 系 Linux と WSL を主対象とします。

### 要件

- Python 3.12 以上。見つからず `uv` がある場合は 3.12 の取得を試みます。
- Node.js 20 以上
- PostgreSQL client/server または外部 PostgreSQL
- package install が必要な場合は `sudo`

Windows と Linux で Node.js gate が同じではない点に注意してください。Windows `setup.bat` は 22+、Linux `setup.sh` は 20+ です。

### セットアップ

```bash
chmod +x setup.sh run.sh
./setup.sh
```

`.env` がなければ `.env.sample` から作成し、認証・内部 API 用 secret と bootstrap admin password を生成します。ローカル PostgreSQL を管理できる環境では role/database も冪等に準備します。

### 音声依存

Linux setup の既定は `.[test]` で、重量級 audio stack を入れません。必要な場合だけ:

```bash
AOITALK_INSTALL_AUDIO_DEPS=true ./setup.sh
```

このとき `.[audio,test,irodori,yomi-linter]` と Irodori runtime dependencies を導入します。

### 外部 DB

外部 PostgreSQL を既に管理している場合は `.env` の接続先を設定します。setup に role/database 作成をさせない構成では:

```bash
AOITALK_SKIP_DB_SETUP=true ./setup.sh
```

`AOITALK_SKIP_DB_SETUP` は DB が不要になる指定ではありません。アプリが使う PostgreSQL は別途到達可能である必要があります。

### pgvector

現行 schema は PostgreSQL の pgvector 拡張を必須にしません。ベクトル検索は Qdrant を使う経路が正規です。古い手順にある `CREATE EXTENSION vector` をセットアップ必須条件として追加しないでください。

### 起動

```bash
./run.sh
```

既定は loopback bind / Caddy disabled / browser auto-open disabled です。外部公開時は TLS proxy を明示します。

```bash
./run.sh --public --with-caddy
```

`--public` を Caddy なしで直接公開する構成は `run.sh` が拒否します。

## macOS

`pyproject.toml` や一部 runtime は macOS を考慮していますが、`setup.sh` は apt/systemd 等の Linux 前提を含みます。macOS では次を手動で満たしてください。

1. Python 3.12+ で `venv` を作成
2. 必要な Python extra を `pyproject.toml` に従って導入
3. Node.js を用意し `frontend/` で `npm ci` と `npm run build:production`
4. PostgreSQL を起動し `.env` を設定
5. venv から `python scripts/init_db_schema.py`
6. `main.py` / platform に合う launcher で起動

macOS で Linux の package-manager command をそのまま実行しないでください。

## `.env` と初回ログイン

`.env.sample` を設定キーの一覧の正本とします。README やこの文書に provider key 一覧を複製しません。

セットアップが生成・確認する重要な secret には `NEXTAUTH_SECRET`, `AOITALK_WEB_AUTH_SECRET`, `AOITALK_JWT_SECRET`, `INTERNAL_API_KEY`, `AOITALK_CADDY_GATE_KEY` などがあります。

初回管理者 password はローカル `.env` の `AOITALK_BOOTSTRAP_ADMIN_PASSWORD` を確認します。固定 password を Git 管理文書へ書かないでください。

## DB migration

通常セットアップでは `scripts/init_db_schema.py` を使います。schema の正本は Alembic です。

個別 migration を確認する場合:

```powershell
venv\Scripts\python.exe -m alembic current
venv\Scripts\python.exe -m alembic heads
```

新しい schema を設計するときは Drizzle だけを変更して終わらせず、Alembic と `frontend/src/db/schema.ts` の必要な同期を行います。詳細は [schema_drift_check.md](schema_drift_check.md)。

## Frontend build

- `frontend/package.json` の `build` / `build:production` がコマンドの正本です。
- setup scripts は production build を生成します。
- repo の検証方針は `AGENTS.md` / `CLAUDE.md` を参照し、変更と無関係な full build を毎回実行しません。

## Qdrant

Qdrant は RAG / semantic search で使用します。アプリの全機能に対する PostgreSQL 代替ではありません。Qdrant が必要な機能を使う場合だけ、設定された host/port または deployment の service を準備してください。

## トラブルシューティング

### Python gate で止まる

`venv` が 3.12 未満なら setup script で再作成します。既存プロセスが venv をロックしている場合は停止してから再実行してください。

### Frontend が起動しない

`logs/web/frontend.log` と [logging.md](logging.md) を確認します。production start を検証する場合は production build が必要です。

### PostgreSQL に接続できない

`.env` の `POSTGRES_*` / `DATABASE_URL` と実 service を確認します。DB schema を手書き SQL で部分作成して migration を迂回しないでください。

### llama.cpp / GGUF

[llama_cpp_muse_glimmer_setup.md](llama_cpp_muse_glimmer_setup.md) を参照してください。managed profile では executable build、model path、served alias が readiness 条件です。

### ログ

ログ配置は [logging.md](logging.md) が正本です。
