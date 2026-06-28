#!/usr/bin/env bash
# AoiTalk Linux (Debian/Ubuntu, WSL2 を含む) 用セットアップスクリプト
# setup.bat の Linux 版。冪等に動作する想定。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==================================="
echo "AoiTalk セットアップ開始"
echo "==================================="

# ---------------------------------------------------------------------------
# [1/6] PostgreSQL 16 + pgvector インストール
# ---------------------------------------------------------------------------
echo
echo "[1/6] PostgreSQL 16 + pgvector をインストール中..."

if ! command -v psql >/dev/null 2>&1; then
    echo "  - apt で postgresql-16 をインストールします (sudo パスワードが必要な場合があります)"
    sudo apt-get update
    # postgresql-16 / pgvector が Debian 12 / Ubuntu 22.04 以降の公式 APT にあれば、それで入る。
    # 見つからない場合は PostgreSQL 公式 APT リポジトリの追加や、pgvector をソースビルドする必要がある。
    # 参考 (実行はしない):
    #   git clone https://github.com/pgvector/pgvector.git
    #   cd pgvector && make && sudo make install
    sudo apt-get install -y postgresql-16 postgresql-client-16 postgresql-16-pgvector || {
        echo "  [警告] postgresql-16 / postgresql-16-pgvector のインストールに失敗しました。"
        echo "         PostgreSQL 公式 APT リポジトリの追加や pgvector のソースビルドが必要かもしれません。"
        echo "         参考: https://www.postgresql.org/download/linux/  https://github.com/pgvector/pgvector"
        exit 1
    }
else
    echo "  - psql は既にインストールされています。スキップ。"
fi

echo
echo "  - PostgreSQL サービスを有効化/起動中..."
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now postgresql 2>/dev/null || {
        echo "  [情報] systemctl による起動に失敗しました。service コマンドで再試行します。"
        sudo service postgresql start || echo "  [警告] PostgreSQL の起動に失敗しました。手動で確認してください。"
    }
else
    sudo service postgresql start || echo "  [警告] PostgreSQL の起動に失敗しました。手動で確認してください。"
fi

# ---------------------------------------------------------------------------
# [2/6] .env 生成 + DB/ユーザー作成 (冪等)
# ---------------------------------------------------------------------------
echo
echo "[2/6] .env と PostgreSQL データベース/ユーザーを設定中..."

# .env が無ければ .env.sample から生成し、空の認証シークレットを自動生成する
if [ ! -f ".env" ]; then
    cp .env.sample .env
    for key in NEXTAUTH_SECRET AOITALK_WEB_AUTH_SECRET AOITALK_JWT_SECRET INTERNAL_API_KEY; do
        # CRLF 改行の .env.sample にも対応する
        if grep -Eq "^${key}=[[:space:]]*\r?$" .env; then
            secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
            sed -E -i "s|^${key}=[[:space:]]*(\r?)$|${key}=${secret}\1|" .env
            echo "  - ${key} を自動生成しました。"
        fi
    done
    echo "  - .env を .env.sample から作成しました。APIキー等は必要に応じて .env を編集してください。"
else
    echo "  - 既存の .env を使用します。"
fi

# peer 認証前提で sudo -u postgres psql を使う
PSQL_SU="sudo -u postgres psql -v ON_ERROR_STOP=1"

$PSQL_SU -tAc "SELECT 1 FROM pg_roles WHERE rolname='aoitalk'" | grep -q 1 || \
    $PSQL_SU -c "CREATE USER aoitalk WITH PASSWORD 'aoitalk_password';"

$PSQL_SU -tAc "SELECT 1 FROM pg_database WHERE datname='aoitalk_memory'" | grep -q 1 || \
    $PSQL_SU -c "CREATE DATABASE aoitalk_memory OWNER aoitalk;"

$PSQL_SU -c "GRANT ALL PRIVILEGES ON DATABASE aoitalk_memory TO aoitalk;"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d aoitalk_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d aoitalk_memory -c "GRANT USAGE ON SCHEMA public TO aoitalk;"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d aoitalk_memory -c "GRANT CREATE ON SCHEMA public TO aoitalk;"

echo "  - 完了しました。"

# ---------------------------------------------------------------------------
# [3/6] Node.js LTS の確認とインストール
# ---------------------------------------------------------------------------
echo
echo "[3/6] Node.js の確認とインストール..."

if command -v node >/dev/null 2>&1; then
    echo "  - Node.js は既にインストールされています ($(node --version))。スキップ。"
else
    echo "  - Node.js LTS を nodesource 経由でインストールします..."
    # NodeSource 公式手順 (LTS)
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# ---------------------------------------------------------------------------
# [4/6] Python venv + Python 依存
# ---------------------------------------------------------------------------
echo
echo "[4/6] Python 仮想環境とパッケージをインストール中..."

PYTHON_CMD=""
for candidate in "${PYTHON:-}" python3.12 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
        PYTHON_CMD="$candidate"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "  [エラー] Python 3.12以上が必要です。python3.12 をインストールしてから再実行してください。"
    exit 1
fi
echo "  - Python: $($PYTHON_CMD --version)"

# python3-venv が無いと venv 作成に失敗するので、可能なら入れておく
if ! "$PYTHON_CMD" -m venv --help >/dev/null 2>&1; then
    echo "  - Python venv パッケージをインストールします..."
    sudo apt-get install -y python3.12-venv python3-pip || sudo apt-get install -y python3-venv python3-pip || true
fi

if [ -d "venv" ]; then
    if [ -x "venv/bin/python" ] && venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
        echo "  - venv は既にPython 3.12以上です。スキップ。"
    else
        echo "  - 既存venvがPython 3.12未満のため再作成します..."
        rm -rf venv
    fi
fi

if [ ! -d "venv" ]; then
    "$PYTHON_CMD" -m venv venv || {
        echo "  [エラー] venv 作成に失敗しました。python3.12-venv をインストールしてから再実行してください。"
        exit 1
    }
    echo "  - venv を作成しました。"
fi

# shellcheck disable=SC1091
. venv/bin/activate
pip install --upgrade pip
# Linux では windows extras を除外
pip install -e ".[audio,test,irodori]"
pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" \
    descript-audiotools argbind julius pystoi torch-stoi flatten-dict \
    markdown2 randomname importlib-resources

# ---------------------------------------------------------------------------
# [5/6] フロントエンド依存
# ---------------------------------------------------------------------------
echo
echo "[5/6] フロントエンドの依存インストールとビルド中..."

if [ -f "frontend/package.json" ]; then
    cp -f .env frontend/.env
    (cd frontend && npm ci && npm run build)
    echo "  - 完了しました。"
else
    echo "  - frontend/package.json が見つかりません。スキップしました。"
fi

# ---------------------------------------------------------------------------
# [6/6] alembic マイグレーション
# ---------------------------------------------------------------------------
echo
echo "[6/6] データベーススキーマ初期化/マイグレーション実行中..."
# shellcheck disable=SC1091
. venv/bin/activate
python scripts/init_db_schema.py

echo
echo "==================================="
echo "セットアップ完了！"
echo "==================================="
echo
echo "起動: ./run.sh"
echo
