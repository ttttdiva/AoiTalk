#!/usr/bin/env bash
# AoiTalk Linux 起動スクリプト (run.bat の Linux 版)
# - venv を有効化
# - .env を frontend/.env へコピー
# - main.py が exit code 42 で終了した場合は再起動

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x "venv/bin/python" ]; then
    echo "Python virtual environment not found: venv/bin/python" >&2
    exit 1
fi

# shellcheck disable=SC1091
. venv/bin/activate

python - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
if [ $? -ne 0 ]; then
    echo "Python 3.12以上の仮想環境が必要です。setup.shでvenvを再作成してください。" >&2
    exit 1
fi

cp -f .env frontend/.env 2>/dev/null || true

while :; do
    python main.py
    rc=$?
    if [ $rc -ne 42 ]; then
        exit $rc
    fi
    echo
    echo "=== Restarting AoiTalk ==="
    echo
    cp -f .env frontend/.env 2>/dev/null || true
    export AOITALK_WEB_AUTO_OPEN=false
done
