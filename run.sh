#!/usr/bin/env bash
# AoiTalk Linux 起動スクリプト (run.bat の Linux 版)
# - venv を有効化
# - .env を frontend/.env へコピー
# - main.py が exit code 42 で終了した場合は再起動

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
. venv/bin/activate

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
