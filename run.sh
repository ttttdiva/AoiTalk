#!/usr/bin/env bash
# AoiTalk Linux/WSL 起動ラッパー
# 初期状態は loopback 限定・Caddy無効。公開する場合はTLS境界を明示的に構成し、
# ./run.sh --public --with-caddy または Docker Compose の caddy サービスを使う。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
    cat <<'EOF'
使い方: ./run.sh [--public] [--with-caddy] [--skip-services] [main.py options]

既定値: 127.0.0.1 bind / Caddy無効 / ブラウザ自動起動無効
公開時は会社DNS・証明書・ファイアウォールを確認してから --public を指定してください。
EOF
}

public_mode="${AOITALK_PUBLIC_MODE:-false}"
with_caddy="${AOITALK_SKIP_CADDY:-true}"
main_args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --public) public_mode=true; shift ;;
        --with-caddy) with_caddy=false; shift ;;
        --skip-caddy) with_caddy=true; shift ;;
        *) main_args+=("$1"); shift ;;
    esac
done

if { [ "$public_mode" = "true" ] || [ "$public_mode" = "1" ] || [ "$public_mode" = "yes" ]; } && \
   { [ "$with_caddy" = "true" ] || [ "$with_caddy" = "1" ] || [ "$with_caddy" = "yes" ]; }; then
    echo "--public は --with-caddy と併用し、TLS境界なしで直接公開しないでください。" >&2
    exit 2
fi

if [ ! -x venv/bin/python ]; then
    echo "Python virtual environment not found: venv/bin/python" >&2
    exit 1
fi

# shellcheck disable=SC1091
. venv/bin/activate
if ! python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "Python 3.12以上の仮想環境が必要です。setup.shを実行してください。" >&2
    exit 1
fi

if [ -f .env ]; then
    install -m 600 .env frontend/.env 2>/dev/null || true
fi

if [ "$public_mode" = "true" ] || [ "$public_mode" = "1" ] || [ "$public_mode" = "yes" ]; then
    export AOITALK_PUBLIC_MODE=true
    # Caddy is the only public listener. Keep application upstreams on
    # loopback even when the proxy listens on a company interface.
    export AOITALK_WEB_HOST=127.0.0.1
    export AOITALK_NEXT_HOST=127.0.0.1
    export AOITALK_FRONTEND_HOST=127.0.0.1
else
    export AOITALK_PUBLIC_MODE=false
    export AOITALK_WEB_HOST="${AOITALK_WEB_HOST:-127.0.0.1}"
    export AOITALK_NEXT_HOST="${AOITALK_NEXT_HOST:-127.0.0.1}"
    export AOITALK_FRONTEND_HOST="${AOITALK_FRONTEND_HOST:-127.0.0.1}"
fi
export AOITALK_SKIP_CADDY="$with_caddy"
export AOITALK_WEB_AUTO_OPEN="${AOITALK_WEB_AUTO_OPEN:-false}"
if [ "${AIVTUBER_ENV:-}" = "enterprise" ] || [ "${AOITALK_PROFILE:-}" = "enterprise" ]; then
    # Enterprise native mode is also a supervised WebUI service; stdin is not
    # an operator channel and must not re-enable console input.
    export AOITALK_HEADLESS=true
else
    export AOITALK_HEADLESS="${AOITALK_HEADLESS:-false}"
fi

child_pid=""
shutdown_requested=false
forward_signal() {
    shutdown_requested=true
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill -TERM "$child_pid" 2>/dev/null || true
    fi
}
trap forward_signal TERM INT

while :; do
    if [ "$shutdown_requested" = "true" ]; then
        exit 0
    fi
    python main.py "${main_args[@]}" &
    child_pid=$!
    wait "$child_pid"
    rc=$?
    if [ "$shutdown_requested" = "true" ]; then
        # Bash may interrupt wait(2) when the wrapper receives SIGTERM/SIGINT.
        # Wait once more so main.py can finish its async cleanup and close
        # FastAPI/Next/Caddy child processes before the supervisor exits.
        while kill -0 "$child_pid" 2>/dev/null; do
            wait "$child_pid" 2>/dev/null || true
        done
        wait "$child_pid" 2>/dev/null || true
        exit 0
    fi
    child_pid=""
    if [ "$rc" -ne 42 ]; then
        exit "$rc"
    fi
    echo
    echo "=== AoiTalkを再起動します ==="
    echo
    [ -f .env ] && install -m 600 .env frontend/.env 2>/dev/null || true
done
