#!/usr/bin/env bash
# AoiTalk Linux/WSL セットアップ
#
# 方針:
# - Python/Node/PostgreSQL の「存在」ではなく、実際に利用可能かを検査する。
# - DB スキーマは Alembic のみを正規ルートにする。
# - 音声系の巨大な依存は Enterprise/Linux の初回セットアップでは入れない。
#   必要な場合だけ AOITALK_INSTALL_AUDIO_DEPS=true を指定する。
# - .env は生成するが、Git 管理下へ戻さない。

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[AoiTalk] %s\n' "$*"; }
die() { printf '[AoiTalk][ERROR] %s\n' "$*" >&2; exit 1; }

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

set_env_value() {
    local key="$1"
    local value="$2"
    local escaped
    escaped="${value//\\/\\\\}"
    escaped="${escaped//&/\\&}"
    if grep -Eq "^${key}=" .env; then
        sed -E -i "s|^${key}=.*$|${key}=${escaped}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

load_env() {
    # .env を shell source しない。値に ``$(...)`` やバッククォートが
    # 含まれていても、セットアッププロセス内のコードとして実行しない。
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            if [ "${#value}" -ge 2 ] && \
                { [ "${value:0:1}" = '"' ] || [ "${value:0:1}" = "'" ]; } && \
                [ "${value:0:1}" = "${value: -1}" ]; then
                value="${value:1:${#value}-2}"
            fi
            export "$key=$value"
        fi
    done < .env
}

ensure_env_file() {
    if [ ! -f .env ]; then
        cp .env.sample .env
        log ".env を .env.sample から作成しました。"
    fi
    chmod 600 .env

    local key
    for key in NEXTAUTH_SECRET AOITALK_WEB_AUTH_SECRET AOITALK_JWT_SECRET INTERNAL_API_KEY AOITALK_CADDY_GATE_KEY; do
        if ! grep -Eq "^${key}=[^[:space:]]+" .env; then
            set_env_value "$key" "$(random_secret)"
            log "${key} を生成しました。"
        fi
    done

    # 固定された初期DBパスワードをそのまま使わない。既存環境の値は尊重する。
    if grep -Eq '^POSTGRES_PASSWORD=(|aoitalk_password)[[:space:]]*$' .env; then
        set_env_value POSTGRES_PASSWORD "$(random_secret)"
        log "POSTGRES_PASSWORD をランダム値へ置換しました。"
    fi
    if ! grep -Eq '^AOITALK_BOOTSTRAP_ADMIN_PASSWORD=[^[:space:]]+' .env; then
        set_env_value AOITALK_BOOTSTRAP_ADMIN_PASSWORD "$(random_secret)"
        log "初回管理者パスワードを生成しました。初回ログイン後に変更してください。"
    fi
}

ensure_node() {
    if ! command -v node >/dev/null 2>&1; then
        command -v sudo >/dev/null 2>&1 || die "Node.js がなく sudo もありません。Node.js 20以上を先に用意してください。"
        log "Node.js LTS をインストールします。"
        curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
    node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' \
        || die "Node.js 20以上が必要です: $(node --version)"
    log "Node.js: $(node --version), npm: $(npm --version)"
}

find_python() {
    local candidate
    for candidate in "${PYTHON:-}" python3.12 python3; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && \
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done

    # Debian/Ubuntu の標準リポジトリに 3.12 がない場合でも、uv の管理Pythonを使える。
    if command -v uv >/dev/null 2>&1; then
        log "Python 3.12 が見つからないため uv で取得します。" >&2
        uv python install 3.12 >&2
        uv python find 3.12
        return 0
    fi
    return 1
}

ensure_python_venv() {
    local python_cmd
    python_cmd="$(find_python)" || die "Python 3.12以上が必要です。uv または python3.12 を用意してください。"
    log "Python: $($python_cmd --version)"

    if [ -d venv ] && { [ ! -x venv/bin/python ] || ! venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; }; then
        log "既存venvのPythonが古いため作り直します。"
        rm -rf venv
    fi
    if [ ! -d venv ]; then
        "$python_cmd" -m venv venv || {
            command -v sudo >/dev/null 2>&1 && sudo apt-get install -y python3-venv python3-pip || true
            "$python_cmd" -m venv venv || die "venv の作成に失敗しました。"
        }
    fi

    # shellcheck disable=SC1091
    . venv/bin/activate
    python -m pip install --upgrade pip wheel
    if is_true "${AOITALK_INSTALL_AUDIO_DEPS:-false}"; then
        python -m pip install -e '.[audio,test,irodori,yomi-linter]'
        # DACVAE's descript-audiotools dependency pins protobuf<3.20, while
        # AoiTalk (mem0ai) requires protobuf>=5.29.6. Keep the existing
        # protobuf and install the codec plus its runtime imports explicitly,
        # rather than allowing pip to downgrade the memory stack.
        python -m pip install --no-deps \
            'dacvae @ git+https://github.com/facebookresearch/dacvae@414c20785fc3a28373073ea8ef7a1316eeeaca6e'
        python -m pip install --no-deps descript-audiotools==0.7.2
        python -m pip install \
            absl-py argbind einops ffmpy ipython julius librosa markdown2 matplotlib \
            flatten-dict importlib-resources pyloudnorm pystoi randomname rich scipy \
            soundfile tensorboard torch-stoi
    else
        python -m pip install -e '.[test]'
    fi
}

ensure_postgres() {
    if is_true "${AOITALK_SKIP_DB_SETUP:-false}"; then
        log "AOITALK_SKIP_DB_SETUP=true のためDBセットアップをスキップします。"
        return 0
    fi

    local host="${POSTGRES_HOST:-127.0.0.1}"
    local port="${POSTGRES_PORT:-5432}"
    if ! command -v pg_isready >/dev/null 2>&1; then
        if command -v sudo >/dev/null 2>&1; then
            log "PostgreSQLクライアントがないためインストールします。"
            sudo apt-get update
            sudo apt-get install -y postgresql postgresql-client
        else
            die "pg_isready がありません。PostgreSQL または AOITALK_SKIP_DB_SETUP=true を用意してください。"
        fi
    fi

    if ! pg_isready -h "$host" -p "$port" >/dev/null 2>&1; then
        if [[ "$host" == "localhost" || "$host" == "127.0.0.1" || "$host" == "::1" ]] && command -v sudo >/dev/null 2>&1; then
            log "ローカルPostgreSQLを起動します。"
            sudo systemctl enable --now postgresql 2>/dev/null || sudo service postgresql start 2>/dev/null || true
        fi
    fi
    pg_isready -h "$host" -p "$port" >/dev/null 2>&1 || \
        die "PostgreSQL ${host}:${port} に接続できません。Docker Composeを使う場合は AOITALK_SKIP_DB_SETUP=true を指定してください。"

    if [[ "$host" != "localhost" && "$host" != "127.0.0.1" && "$host" != "::1" ]]; then
        log "リモートPostgreSQLのrole/database変更は行いません。接続確認のみ完了しました。"
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 && \
        ! sudo systemctl is-active --quiet postgresql 2>/dev/null; then
        log "PostgreSQLがsystemd管理のローカルサービスではないため、role/database変更は行いません。"
        return 0
    fi

    # ローカルのpostgres管理ユーザーが使える場合だけ、アプリ用role/dbを冪等に整える。
    if ! command -v sudo >/dev/null 2>&1 || ! id postgres >/dev/null 2>&1; then
        log "postgres OSユーザーがないため、DB role/db作成は外部管理とみなします。"
        return 0
    fi
    [[ "${POSTGRES_USER:-aoitalk}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_USER は英数字と _ のみ使用してください。"
    [[ "${POSTGRES_DB:-aoitalk_memory}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_DB は英数字と _ のみ使用してください。"

    local user="${POSTGRES_USER:-aoitalk}"
    local db="${POSTGRES_DB:-aoitalk_memory}"
    local password="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD がありません}"
    local sql_password="${password//\'/\'\'}"
    local psql_admin=(sudo -u postgres psql -v ON_ERROR_STOP=1)

    if ! "${psql_admin[@]}" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='${user}'" | grep -q 1; then
        "${psql_admin[@]}" -d postgres -c "CREATE ROLE \"${user}\" LOGIN PASSWORD '${sql_password}';"
    else
        "${psql_admin[@]}" -d postgres -c "ALTER ROLE \"${user}\" LOGIN PASSWORD '${sql_password}';"
    fi
    if ! "${psql_admin[@]}" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
        "${psql_admin[@]}" -d postgres -c "CREATE DATABASE \"${db}\" OWNER \"${user}\";"
    fi
    "${psql_admin[@]}" -d "$db" -c "GRANT USAGE,CREATE ON SCHEMA public TO \"${user}\";"
    log "PostgreSQL ${host}:${port}/${db} を確認しました。pgvector拡張は必須にしません。"
}

ensure_frontend() {
    [ -f frontend/package.json ] || { log "frontend/package.json がないためビルドをスキップします。"; return; }
    install -m 600 .env frontend/.env
    # run.sh は canonical な frontend/.next を npm start で配信するため、
    # セットアップでは検証用の npm run build ではなく本番ビルドを生成する。
    (cd frontend && npm ci && npm run build:production)
}

main() {
    printf '%s\n' '===================================' 'AoiTalk Linux セットアップ開始' '==================================='
    ensure_env_file
    load_env
    ensure_node
    ensure_postgres
    ensure_python_venv
    ensure_frontend

    log "Alembicマイグレーションを実行します。"
    # shellcheck disable=SC1091
    . venv/bin/activate
    python scripts/init_db_schema.py
    chmod +x setup.sh run.sh 2>/dev/null || true
    printf '%s\n' '===================================' 'セットアップ完了' '==================================='
    printf '起動: ./run.sh\n'
    printf '初回ログイン: admin / .env の AOITALK_BOOTSTRAP_ADMIN_PASSWORD\n'
}

main "$@"
