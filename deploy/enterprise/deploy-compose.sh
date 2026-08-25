#!/usr/bin/env bash
set -Eeuo pipefail

# AoiTalk Enterprise target-side executable launcher.
#
# The canonical handoff carries a sanitized source tree, not image/model
# archives. This launcher verifies the handoff manifest and pinned model before
# building/starting Compose. It never performs an implicit model download.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

die() {
    echo "[AoiTalk Enterprise] ERROR: $*" >&2
    exit 1
}

# A handoff release has metadata at its root and the sanitized build/runtime
# tree below `source/`.  Keep these roots explicit so a launcher never looks
# for Dockerfile/config/Caddy/scripts beside (rather than under) source/.
RELEASE_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd -P)"
BUNDLE_ROOT="$RELEASE_ROOT/source"
HANDOFF_ROOT="$RELEASE_ROOT"
[[ -f "$HANDOFF_ROOT/bundle-manifest.json" && -d "$BUNDLE_ROOT" ]] || die "invalid Enterprise release layout: expected root metadata and source/ tree"
AOITALK_INSTALL_ROOT="${AOITALK_INSTALL_ROOT:-/opt/aoitalk}"
AOITALK_CONFIG_ROOT="${AOITALK_CONFIG_ROOT:-/etc/aoitalk}"
ENV_FILE="${AOITALK_ENV_FILE:-$AOITALK_CONFIG_ROOT/.env}"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"
COMPOSE_PROJECT_NAME="aoitalk-enterprise"
DOCKER=(docker)
OPERATION_LOCK_FD=""

info() {
    echo "[AoiTalk Enterprise] $*"
}

acquire_operation_lock() {
    local lock_file="$AOITALK_INSTALL_ROOT/.operation.lock" owner mode mode_num current resolved releases
    [[ "$(id -u)" -eq 0 ]] || die "state-changing Enterprise operations must run as root"
    [[ ! -L "$AOITALK_INSTALL_ROOT" ]] || die "install root must not be a symlink: $AOITALK_INSTALL_ROOT"
    assert_secure_ancestors "$AOITALK_INSTALL_ROOT"
    if [[ -e "$AOITALK_INSTALL_ROOT" ]]; then
        [[ -d "$AOITALK_INSTALL_ROOT" ]] || die "install root is not a directory: $AOITALK_INSTALL_ROOT"
        owner="$(stat -c '%u' "$AOITALK_INSTALL_ROOT" 2>/dev/null || true)"
        mode="$(stat -c '%a' "$AOITALK_INSTALL_ROOT" 2>/dev/null || true)"
        [[ "$owner" == 0 ]] || die "install root must be root-owned: $AOITALK_INSTALL_ROOT"
        mode_num=$((8#$mode)); (( (mode_num & 0022) == 0 )) || die "install root is group/world writable: $AOITALK_INSTALL_ROOT"
    fi
    ensure_root_directory "$AOITALK_INSTALL_ROOT" 0755
    current="$AOITALK_INSTALL_ROOT/current"; releases="$AOITALK_INSTALL_ROOT/releases"
    [[ ! -e "$current" || -L "$current" ]] || die "current pointer must be a symlink or absent: $current"
    if [[ -L "$current" ]]; then
        resolved="$(readlink -f -- "$current" 2>/dev/null || true)"
        [[ -n "$resolved" && "$resolved" == "$releases"/* ]] || die "current pointer escapes releases: $current"
        assert_root_owned_directory "$resolved"
    fi
    [[ ! -L "$lock_file" ]] || die "operation lock must not be a symlink: $lock_file"
    if [[ -e "$lock_file" ]]; then
        [[ -f "$lock_file" ]] || die "operation lock must be a regular file: $lock_file"
        owner="$(stat -c '%u' "$lock_file" 2>/dev/null || true)"; mode="$(stat -c '%a' "$lock_file" 2>/dev/null || true)"
        [[ "$owner" == 0 ]] || die "operation lock must be root-owned: $lock_file"
        mode_num=$((8#$mode)); (( (mode_num & 0077) == 0 )) || die "operation lock must not be group/world accessible: $lock_file"
    fi
    exec {OPERATION_LOCK_FD}>"$lock_file"
    chown root:root "$lock_file"
    chmod 0600 "$lock_file"
    flock -n "$OPERATION_LOCK_FD" || die "another Enterprise operation is already running for $AOITALK_INSTALL_ROOT"
    export AOIT_INTERNAL_OPERATION_LOCK_HELD="$lock_file"
}

release_operation_lock() {
    if [[ -n "$OPERATION_LOCK_FD" ]]; then
        flock -u "$OPERATION_LOCK_FD" || true
        eval "exec ${OPERATION_LOCK_FD}>&-"
        OPERATION_LOCK_FD=""
    fi
}

warn() {
    echo "[AoiTalk Enterprise] WARNING: $*" >&2
}

ensure_env_file() {
    [[ "$ENV_FILE" == "/etc/aoitalk/.env" ]] || \
        die "AOITALK_ENV_FILE cannot move operator configuration out of /etc/aoitalk/.env"
    [[ ! -L "$ENV_FILE" ]] || die "refusing symlinked .env: $ENV_FILE"
    if [[ ! -f "$ENV_FILE" ]]; then
        [[ "$(id -u)" == "0" ]] || die "run the launcher with sudo before creating $ENV_FILE"
        [[ -f "$ENV_EXAMPLE" ]] || die "missing $ENV_EXAMPLE"
        ensure_root_directory "$AOITALK_CONFIG_ROOT" 0755
        install -m 0600 "$ENV_EXAMPLE" "$ENV_FILE"
        info "Created $ENV_FILE from .env.example; review provider, path, and port settings."
    fi
    assert_root_owned_file "$ENV_FILE"
    # Compose v2 has historically rejected a UTF-8 BOM in --env-file even
    # though the shell parser below can ignore it. Normalize the operator
    # file once so direct and sudo Docker paths consume identical bytes.
    if [[ "$(LC_ALL=C od -An -tx1 -N3 "$ENV_FILE" | tr -d ' \n')" == "efbbbf" ]]; then
        local normalized
        normalized="$(mktemp "${ENV_FILE}.XXXXXX")"
        tail -c +4 "$ENV_FILE" > "$normalized"
        chown root:root "$normalized"
        chmod 0600 "$normalized"
        mv -f "$normalized" "$ENV_FILE"
    fi
}

load_env_file() {
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        # Windows PowerShell 5.1 may leave a UTF-8 BOM when an operator edits
        # the copied .env.  Strip it only from the first character so a valid
        # key remains readable without accepting malformed lines elsewhere.
        line="${line#$'\ufeff'}"
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            if [[ "$value" == \"*\" && "$value" == *\" ]]; then
                value="${value:1:${#value}-2}"
            elif [[ "$value" == \"*\" && "$value" != *\" ]]; then
                die "unterminated double quote in $ENV_FILE"
            fi
            if [[ "$value" == \'* && "$value" == *\' ]]; then
                value="${value:1:${#value}-2}"
            elif [[ "$value" == \'* && "$value" != *\' ]]; then
                die "unterminated single quote in $ENV_FILE"
            fi
            case "$key" in
                AOITALK_*|SGLANG_MODEL|GEMMA_*|DEEPSEEK_*) ;;
                *) die "unsupported key in $ENV_FILE: $key" ;;
            esac
            export "$key=$value"
        else
            die "invalid .env line in $ENV_FILE: $line"
        fi
    done < "$ENV_FILE"
}

set_defaults() {
    # The release tree is immutable.  Do not inherit a release path from the
    # operator env file: invoking a different /opt/aoitalk/releases/<id>
    # launcher must always bind that exact release's read-only files.
    export AOITALK_BUNDLE_ROOT="$BUNDLE_ROOT"
    export AOITALK_RELEASE_ROOT="$RELEASE_ROOT"
    export AOITALK_CURRENT_LINK="${AOITALK_CURRENT_LINK:-$AOITALK_INSTALL_ROOT/current}"
    export AOITALK_DATA_ROOT="${AOITALK_DATA_ROOT:-/var/lib/aoitalk}"
    export AOITALK_SECRETS_DIR="${AOITALK_SECRETS_DIR:-/etc/aoitalk/secrets}"
    export AOITALK_RUNTIME_CONFIG_FILE="${AOITALK_RUNTIME_CONFIG_FILE:-/etc/aoitalk/runtime-config/enterprise.yaml}"
    export AOITALK_INIT_DB_SQL="${AOITALK_INIT_DB_SQL:-$AOITALK_BUNDLE_ROOT/scripts/init-db.sql}"
    export AOITALK_CADDY_CERTS_DIR="${AOITALK_CADDY_CERTS_DIR:-$AOITALK_DATA_ROOT/caddy/certs}"
    export AOITALK_HTTPS_PORT="${AOITALK_HTTPS_PORT:-6002}"
    export AOITALK_HTTP_PORT="${AOITALK_HTTP_PORT:-6001}"
    export AOITALK_BOOTSTRAP_HTTPS_PORT="${AOITALK_BOOTSTRAP_HTTPS_PORT:-8443}"
    # Backend and transport are first-class values.  Keep AOITALK_LLM_MODE as
    # a backwards-compatible alias for older operator .env files.
    export AOITALK_BACKEND="${AOITALK_BACKEND:-${AOITALK_LLM_MODE:-external}}"
    export AOITALK_IMAGE="${AOITALK_IMAGE:-}"
    export AOITALK_TRANSPORT="${AOITALK_TRANSPORT:-https}"
    export AOITALK_LLM_MODE="${AOITALK_LLM_MODE:-$AOITALK_BACKEND}"
    # An unset allowlist preserves compatibility with older operator files;
    # the checked-in .env.example intentionally narrows it to the local
    # llama.cpp router.
    export AOITALK_ALLOWED_PROVIDER_IDS="${AOITALK_ALLOWED_PROVIDER_IDS:-openrouter,openai,gemini,kimi,deepseek,deepinfra,openai_compatible_local}"
    export AOITALK_EXTERNAL_PROVIDER="${AOITALK_EXTERNAL_PROVIDER:-openai_compatible_local}"
    if [[ -z "${AOITALK_EXTERNAL_MODEL:-}" ]]; then
        case "$AOITALK_EXTERNAL_PROVIDER" in
            openai_compatible_local) AOITALK_EXTERNAL_MODEL=qwen3.8-27b ;;
            *) AOITALK_EXTERNAL_MODEL=openai/gpt-4o-mini ;;
        esac
    fi
    export AOITALK_EXTERNAL_MODEL
    if [[ -z "${AOITALK_EXTERNAL_BASE_URL:-}" ]]; then
        case "$AOITALK_EXTERNAL_PROVIDER" in
            openrouter) AOITALK_EXTERNAL_BASE_URL=https://openrouter.ai/api/v1 ;;
            openai_compatible_local) AOITALK_EXTERNAL_BASE_URL=http://host.docker.internal:18080/v1 ;;
            *) AOITALK_EXTERNAL_BASE_URL=https://openrouter.ai/api/v1 ;;
        esac
    fi
    export AOITALK_EXTERNAL_BASE_URL
    export AOITALK_EXTERNAL_REQUIRED_MODELS="${AOITALK_EXTERNAL_REQUIRED_MODELS:-qwen3.8-27b,gemma-4-26b-a4b-it-qat-q4-0}"
    export AOITALK_GEMMA_VLLM_IMAGE="${AOITALK_GEMMA_VLLM_IMAGE:-}"
    export AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE="${AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE:-}"
    export AOITALK_GEMMA_MODEL="${AOITALK_GEMMA_MODEL:-google/gemma-4-E4B-it}"
    export AOITALK_GEMMA_SERVED_MODEL="${AOITALK_GEMMA_SERVED_MODEL:-$AOITALK_GEMMA_MODEL}"
    export AOITALK_GEMMA_MODEL_REVISION="${AOITALK_GEMMA_MODEL_REVISION:-ee0ef6023621cff504d758262d4e04895a5af4a2}"
    export AOITALK_GEMMA_MODEL_FILE="${AOITALK_GEMMA_MODEL_FILE:-model.safetensors}"
    export AOITALK_GEMMA_MODEL_SIZE_BYTES="${AOITALK_GEMMA_MODEL_SIZE_BYTES:-15992595884}"
    export AOITALK_GEMMA_MODEL_SHA256="${AOITALK_GEMMA_MODEL_SHA256:-}"
    export AOITALK_GEMMA_MODEL_DIR="${AOITALK_GEMMA_MODEL_DIR:-$AOITALK_DATA_ROOT/huggingface/gemma-4E4B-it}"
    export AOITALK_GEMMA_VLLM_BASE_URL="${AOITALK_GEMMA_VLLM_BASE_URL:-http://gemma-vllm:8000/v1}"
    export AOITALK_GEMMA_VLLM_SERVER_PROFILE="${AOITALK_GEMMA_VLLM_SERVER_PROFILE:-vllm}"
    export AOITALK_GEMMA_VLLM_PORT="${AOITALK_GEMMA_VLLM_PORT:-8000}"
    export AOITALK_GEMMA_VLLM_CONTEXT_LENGTH="${AOITALK_GEMMA_VLLM_CONTEXT_LENGTH:-32768}"
    export AOITALK_GEMMA_VLLM_DTYPE="${AOITALK_GEMMA_VLLM_DTYPE:-bfloat16}"
    export AOITALK_GEMMA_VLLM_MEM_UTIL="${AOITALK_GEMMA_VLLM_MEM_UTIL:-0.80}"
    export AOITALK_DEEPSEEK_MODEL_DIR="${AOITALK_DEEPSEEK_MODEL_DIR:-$AOITALK_DATA_ROOT/huggingface/deepseek-llamacpp}"
    export AOITALK_DEEPSEEK_MODEL_FILE="${AOITALK_DEEPSEEK_MODEL_FILE:-model.gguf}"
    export AOITALK_DEEPSEEK_SERVED_MODEL="${AOITALK_DEEPSEEK_SERVED_MODEL:-deepseek-llamacpp}"
    export AOITALK_DEEPSEEK_BASE_URL="${AOITALK_DEEPSEEK_BASE_URL:-http://deepseek-llamacpp:8080/v1}"
    export COMPOSE_PROJECT_NAME
}

apply_manifest_image_defaults() {
    local manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}" key name ref current
    [[ -f "$manifest" && ! -L "$manifest" ]] || die "handoff manifest is missing or symlinked: $manifest"
    command -v python3 >/dev/null 2>&1 || die "python3 is required to generate dependency image settings"
    while IFS=$'\t' read -r name ref; do
        [[ -n "$name" && -n "$ref" ]] || continue
        case "$name" in
            postgres) key=AOITALK_POSTGRES_IMAGE ;;
            qdrant) key=AOITALK_QDRANT_IMAGE ;;
            caddy) key=AOITALK_CADDY_IMAGE ;;
            busybox) key=AOITALK_BUSYBOX_IMAGE ;;
            curl) key=AOITALK_CURL_IMAGE ;;
            gemma-vllm) key=AOITALK_GEMMA_VLLM_IMAGE ;;
            sglang) key=AOITALK_SGLANG_IMAGE ;;
            deepseek-llamacpp) key=AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE ;;
            aoitalk|dockerfile-node|dockerfile-python) continue ;;
            *) die "handoff manifest has an unknown image pin name: $name" ;;
        esac
        current="${!key:-}"
        [[ -z "$current" || "$current" == "$ref" ]] || die "$key must match the immutable handoff manifest; do not replace it with a tag"
        printf -v "$key" '%s' "$ref"
        export "$key"
    done < <(python3 - "$manifest" <<'PY'
import json, pathlib, re, sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
if m.get('format') != 'aoitalk-enterprise-handoff' or int(m.get('version',0)) != 1:
    raise SystemExit('unsupported handoff manifest')
for row in m.get('image_pins',[]):
    if row.get('kind') == 'dependency':
        ref=str(row.get('ref',''))
        if not re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}',ref): raise SystemExit(f'image pin is not immutable: {ref}')
        print(f"{row.get('name','')}\t{ref}")
PY
    )
}

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "run this target-side command as root (for example: sudo ./deploy/enterprise/deploy-compose.sh $1)"
}

require_absolute_path() {
    local name="$1" value="$2"
    [[ "$value" == /* ]] || die "$name must be an absolute Linux path: $value"
}

validate_safe_path() {
    local name="$1" value="$2"
    require_absolute_path "$name" "$value"
    case "$value" in
        /|/bin|/dev|/etc|/home|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
            die "$name points at a protected/broad directory: $value"
            ;;
        *'//'*)
            die "$name must use canonical single-slash path separators: $value"
            ;;
        */../*|*/..|*/./*|*/.)
            die "$name contains an unresolved path component: $value"
            ;;
    esac
    [[ "$value" == "/" || "$value" != */ ]] || die "$name must not have a trailing slash: $value"
}

paths_overlap() {
    local left="$1" right="$2"
    [[ "$left" == "$right" || "$left" == "$right"/* || "$right" == "$left"/* ]]
}

assert_secure_ancestors() {
    local path="$1" current parent owner mode mode_num
    current="$(dirname "$path")"
    while [[ "$current" != "/" ]]; do
        if [[ -L "$current" ]]; then
            die "path ancestor is a symlink: $current"
        fi
        if [[ -e "$current" ]]; then
            [[ -d "$current" ]] || die "path ancestor is not a directory: $current"
            owner="$(stat -c '%u' "$current" 2>/dev/null || true)"
            mode="$(stat -c '%a' "$current" 2>/dev/null || true)"
            [[ "$owner" == "0" ]] || die "path ancestor must be root-owned: $current"
            mode_num=$((8#$mode))
            (( (mode_num & 0022) == 0 )) || die "path ancestor is group/world writable: $current"
        fi
        parent="$(dirname "$current")"
        [[ "$parent" != "$current" ]] || break
        current="$parent"
    done
}

ensure_root_directory() {
    local path="$1" mode="$2"
    assert_secure_ancestors "$path"
    [[ ! -L "$path" ]] || die "refusing symlink directory: $path"
    (umask 077; mkdir -p "$path")
    chown root:root "$path"
    chmod "$mode" "$path"
}

assert_root_owned_file() {
    local path="$1" owner mode mode_num
    [[ -f "$path" && ! -L "$path" ]] || die "secret/config path is missing or symlinked: $path"
    owner="$(stat -c '%u' "$path" 2>/dev/null || true)"
    [[ "$owner" == "0" ]] || die "secret/config file must be root-owned: $path"
    mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    mode_num=$((8#$mode))
    (( (mode_num & 0022) == 0 )) || die "secret/config file is group/world writable: $path"
}

assert_root_owned_directory() {
    local path="$1" owner mode mode_num
    [[ -d "$path" && ! -L "$path" ]] || die "directory is missing or symlinked: $path"
    owner="$(stat -c '%u' "$path" 2>/dev/null || true)"
    [[ "$owner" == "0" ]] || die "directory must be root-owned: $path"
    mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    mode_num=$((8#$mode))
    (( (mode_num & 0022) == 0 )) || die "directory is group/world writable: $path"
}

validate_value() {
    local name="$1" value="$2"
    case "$value" in
        *$'\n'*|*$'\r'*) die "$name must not contain a newline" ;;
    esac
}

provider_allowed_by_operator() {
    local provider="$1" raw token
    raw="${AOITALK_ALLOWED_PROVIDER_IDS:-}"
    [[ -z "$raw" ]] && return 0
    IFS=',' read -ra _allowed_provider_ids <<< "$raw"
    for token in "${_allowed_provider_ids[@]}"; do
        # Provider IDs are deliberately whitespace-free in the env contract;
        # trimming here makes hand-edited comma lists less surprising without
        # changing the value passed to the application.
        token="${token#${token%%[![:space:]]*}}"
        token="${token%${token##*[![:space:]]}}"
        [[ "$token" == "$provider" ]] && return 0
    done
    die "AOITALK_EXTERNAL_PROVIDER is not permitted by AOITALK_ALLOWED_PROVIDER_IDS: $provider"
}

validate_external_required_models() {
    local provider="$1" raw token has_qwen=false has_gemma=false
    raw="${AOITALK_EXTERNAL_REQUIRED_MODELS:-}"
    validate_value AOITALK_EXTERNAL_REQUIRED_MODELS "$raw"
    [[ -n "$raw" ]] || { [[ "$provider" != openai_compatible_local ]] && return 0; die "AOITALK_EXTERNAL_REQUIRED_MODELS is required for the local router"; }
    IFS=',' read -ra _required_models <<< "$raw"
    for token in "${_required_models[@]}"; do
        [[ "$token" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]] || die "AOITALK_EXTERNAL_REQUIRED_MODELS contains an unsafe model ID"
        [[ "$token" == qwen3.8-27b ]] && has_qwen=true
        [[ "$token" == gemma-4-26b-a4b-it-qat-q4-0 ]] && has_gemma=true
    done
    if [[ "$provider" == openai_compatible_local ]]; then
        [[ "$has_qwen" == true && "$has_gemma" == true ]] || \
            die "AOITALK_EXTERNAL_REQUIRED_MODELS must include qwen3.8-27b and gemma-4-26b-a4b-it-qat-q4-0"
    fi
}

validate_profile() {
    case "$1" in
        core|external|gemma-vllm|deepseek-llamacpp|sglang|sglang-cuda|http) ;;
        *) die "backend/profile must be external, gemma-vllm, deepseek-llamacpp, sglang-cuda, or a compatibility alias: $1" ;;
    esac
}

normalize_backend() {
    local value="${1:-${AOITALK_BACKEND:-external}}"
    case "$value" in
        core) printf '%s\n' "${AOITALK_BACKEND:-external}" ;;
        external) printf '%s\n' external ;;
        gemma-vllm|deepseek-llamacpp|sglang-cuda) printf '%s\n' "$value" ;;
        sglang) printf '%s\n' sglang-cuda ;;
        http) printf '%s\n' "${AOITALK_BACKEND:-external}" ;;
        *) die "unsupported Enterprise backend: $value" ;;
    esac
}

normalize_transport() {
    local value="${1:-${AOITALK_TRANSPORT:-https}}"
    case "$value" in
        https) printf '%s\n' https ;;
        http|http-redirect) printf '%s\n' http-redirect ;;
        *) die "transport must be https or http-redirect: $value" ;;
    esac
}

validate_settings() {
    local provider="$AOITALK_EXTERNAL_PROVIDER" managed backend transport
    backend="$(normalize_backend "${AOITALK_BACKEND:-$AOITALK_LLM_MODE}")"
    transport="$(normalize_transport "${AOITALK_TRANSPORT:-https}")"
    export AOITALK_BACKEND="$backend" AOITALK_TRANSPORT="$transport"
    case "$AOITALK_LLM_MODE" in
        external|core|gemma-vllm|deepseek-llamacpp|sglang|sglang-cuda|openai_compatible_local) ;;
        *) die "AOITALK_LLM_MODE is not a supported compatibility value: $AOITALK_LLM_MODE" ;;
    esac
    case "$provider" in
        openrouter|openai|gemini|kimi|deepseek|deepinfra|openai_compatible_local) ;;
        *) die "unsupported AOITALK_EXTERNAL_PROVIDER: $provider" ;;
    esac
    provider_allowed_by_operator "$provider"
    validate_value AOITALK_EXTERNAL_PROVIDER "$provider"
    validate_value AOITALK_EXTERNAL_MODEL "$AOITALK_EXTERNAL_MODEL"
    validate_value AOITALK_EXTERNAL_BASE_URL "$AOITALK_EXTERNAL_BASE_URL"
    validate_external_required_models "$provider"
    validate_value SGLANG_MODEL "${SGLANG_MODEL:-google/gemma-4-E4B-it}"
    validate_value AOITALK_GEMMA_SERVED_MODEL "$AOITALK_GEMMA_SERVED_MODEL"
    validate_value AOITALK_GEMMA_MODEL_REVISION "$AOITALK_GEMMA_MODEL_REVISION"
    validate_value AOITALK_GEMMA_MODEL_FILE "$AOITALK_GEMMA_MODEL_FILE"
    validate_value AOITALK_DEEPSEEK_SERVED_MODEL "$AOITALK_DEEPSEEK_SERVED_MODEL"
    case "${SGLANG_MODEL:-google/gemma-4-E4B-it}" in
        /*|*..*|*$' '*|*$'\t'*) die "SGLANG_MODEL contains an unsafe model identifier" ;;
    esac
    [[ "${SGLANG_MODEL:-google/gemma-4-E4B-it}" != *\"* && "${SGLANG_MODEL:-google/gemma-4-E4B-it}" != *"'"* ]] || \
        die "SGLANG_MODEL contains an unsafe quote"
    [[ "$AOITALK_EXTERNAL_BASE_URL" =~ ^https?:// ]] || die "AOITALK_EXTERNAL_BASE_URL must start with http:// or https://"
    if [[ "$provider" == openai_compatible_local ]]; then
        [[ "$AOITALK_EXTERNAL_BASE_URL" == "http://host.docker.internal:18080/v1" ]] || \
            die "openai_compatible_local Enterprise router must use http://host.docker.internal:18080/v1"
    fi
    [[ "$AOITALK_GEMMA_VLLM_BASE_URL" =~ ^http://gemma-vllm:[0-9]+/v1$ ]] || die "Gemma/vLLM base URL must be the internal http://gemma-vllm:<port>/v1 endpoint"
    [[ "$AOITALK_DEEPSEEK_BASE_URL" =~ ^http://deepseek-llamacpp:[0-9]+/v1$ ]] || die "DeepSeek llama.cpp base URL must be the internal http://deepseek-llamacpp:<port>/v1 endpoint"
    [[ "$AOITALK_GEMMA_MODEL_SIZE_BYTES" =~ ^[0-9]+$ ]] || die "AOITALK_GEMMA_MODEL_SIZE_BYTES must be numeric"
    [[ "$AOITALK_GEMMA_MODEL_REVISION" =~ ^[0-9a-fA-F]{40}$ ]] || die "Gemma model revision must be a 40-character immutable commit"
    [[ "$AOITALK_GEMMA_VLLM_SERVER_PROFILE" == vllm ]] || die "Gemma/vLLM server profile must be vllm"
    validate_safe_path AOITALK_BUNDLE_ROOT "$AOITALK_BUNDLE_ROOT"
    validate_safe_path AOITALK_INSTALL_ROOT "$AOITALK_INSTALL_ROOT"
    validate_safe_path AOITALK_CONFIG_ROOT "$AOITALK_CONFIG_ROOT"
    validate_safe_path AOITALK_DATA_ROOT "$AOITALK_DATA_ROOT"
    validate_safe_path AOITALK_SECRETS_DIR "$AOITALK_SECRETS_DIR"
    validate_safe_path AOITALK_INIT_DB_SQL "$AOITALK_INIT_DB_SQL"
    validate_safe_path AOITALK_CADDY_CERTS_DIR "$AOITALK_CADDY_CERTS_DIR"
    validate_safe_path AOITALK_RUNTIME_CONFIG_FILE "$AOITALK_RUNTIME_CONFIG_FILE"
    validate_safe_path AOITALK_GEMMA_MODEL_DIR "$AOITALK_GEMMA_MODEL_DIR"
    validate_safe_path AOITALK_DEEPSEEK_MODEL_DIR "$AOITALK_DEEPSEEK_MODEL_DIR"
    [[ "$ENV_FILE" == "/etc/aoitalk/.env" ]] || \
        die "ENV_FILE must be /etc/aoitalk/.env for the immutable release layout"
    [[ "$AOITALK_DATA_ROOT" == "/var/lib/aoitalk" ]] || \
        die "AOITALK_DATA_ROOT must be /var/lib/aoitalk for the immutable release layout"
    [[ "$AOITALK_CONFIG_ROOT" == "/etc/aoitalk" ]] || \
        die "AOITALK_CONFIG_ROOT must be /etc/aoitalk for the immutable release layout"
    [[ "$AOITALK_SECRETS_DIR" == "/etc/aoitalk/secrets" ]] || \
        die "AOITALK_SECRETS_DIR must be /etc/aoitalk/secrets for the immutable release layout"
    [[ "$(dirname "$AOITALK_RUNTIME_CONFIG_FILE")" == "/etc/aoitalk/runtime-config" ]] || \
        die "runtime config must be stored under /etc/aoitalk/runtime-config"
    [[ "$AOITALK_CADDY_CERTS_DIR" == "/var/lib/aoitalk/caddy/certs" ]] || \
        die "Caddy certificates must be stored at /var/lib/aoitalk/caddy/certs"
    [[ "$AOITALK_CURRENT_LINK" == "/opt/aoitalk/current" ]] || \
        die "AOITALK_CURRENT_LINK must be /opt/aoitalk/current"
    paths_overlap "$AOITALK_DATA_ROOT" "$AOITALK_SECRETS_DIR" && die "data and secrets paths must not overlap"
    paths_overlap "$AOITALK_DATA_ROOT" "$(dirname "$AOITALK_RUNTIME_CONFIG_FILE")" && die "runtime config must not be stored inside application data"
    paths_overlap "$AOITALK_SECRETS_DIR" "$(dirname "$AOITALK_RUNTIME_CONFIG_FILE")" && die "runtime config must not be stored inside secrets"
    [[ "$AOITALK_DATA_ROOT" != "$AOITALK_BUNDLE_ROOT" && "$AOITALK_BUNDLE_ROOT" != "$AOITALK_DATA_ROOT"/* ]] || \
        die "data root must not be the bundle root or its parent"
    [[ "$AOITALK_SECRETS_DIR" != "$AOITALK_DATA_ROOT"/* ]] || \
        die "secrets directory must not be inside application data"
    [[ "$AOITALK_CADDY_CERTS_DIR" == "$AOITALK_DATA_ROOT"/* ]] || \
        die "Caddy certificates must be stored below the application data root"
    for managed in postgres qdrant qdrant-snapshots caddy/data caddy/config workspaces cache logs tmp huggingface; do
        paths_overlap "$AOITALK_CADDY_CERTS_DIR" "$AOITALK_DATA_ROOT/$managed" && \
            die "Caddy certificates must not overlap managed data: $AOITALK_CADDY_CERTS_DIR"
    done
    validate_port AOITALK_HTTPS_PORT "$AOITALK_HTTPS_PORT"
    validate_port AOITALK_HTTP_PORT "$AOITALK_HTTP_PORT"
    validate_port AOITALK_BOOTSTRAP_HTTPS_PORT "$AOITALK_BOOTSTRAP_HTTPS_PORT"
    [[ "$AOITALK_HTTPS_PORT" != "80" ]] || die "AOITALK_HTTPS_PORT=80 conflicts with Caddy's internal HTTP listener"
    [[ "$AOITALK_HTTPS_PORT" != "8443" ]] || die "AOITALK_HTTPS_PORT=8443 conflicts with Caddy's bootstrap listener"
    [[ "$AOITALK_HTTPS_PORT" != "$AOITALK_BOOTSTRAP_HTTPS_PORT" ]] || die "public and bootstrap HTTPS ports must differ"
    [[ "$AOITALK_HTTP_PORT" != "$AOITALK_HTTPS_PORT" ]] || die "public HTTP and HTTPS ports must differ"
    [[ "$AOITALK_HTTP_PORT" != "$AOITALK_BOOTSTRAP_HTTPS_PORT" ]] || die "public HTTP and bootstrap HTTPS ports must differ"
}

validate_port() {
    local name="$1" value="$2"
    [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be numeric"
    (( value >= 1 && value <= 65535 )) || die "$name must be between 1 and 65535"
}

select_docker() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
        DOCKER=(sudo -n docker)
        info "Docker is available through sudo -n; keeping the host Docker group unchanged."
    else
        die "Docker daemon is unavailable. Check `docker info` and Docker socket permissions."
    fi
}

docker_cmd() {
    if [[ "${DOCKER[0]:-}" == "sudo" ]]; then
        # Keep the sudo target exactly `docker`; restricted sudoers rules often
        # permit that binary but reject a `sudo env ... docker` command. Compose
        # receives the root-readable .env file explicitly below, while the
        # bundle/init paths also have safe compose-file-relative fallbacks.
        sudo -n docker "$@"
    else
        "${DOCKER[@]}" "$@"
    fi
}

assert_single_inference_backend() {
    local backend="$(normalize_backend "${1:-${AOITALK_BACKEND:-external}}")" service active_services
    active_services="$(docker_cmd ps --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null || true)"
    while IFS= read -r service; do
        [[ -n "$service" ]] || continue
        case "$service" in
            gemma-vllm|deepseek-llamacpp|sglang-cuda)
                [[ "$service" == "$backend" ]] || die "inference backend conflict: $service is active while backend=$backend; stop the previous backend first"
                ;;
        esac
    done <<< "$active_services"
}

compose_cmd() {
    local profile="${1:-${AOITALK_BACKEND:-external}}"
    shift || true
    local backend transport
    backend="$(normalize_backend "$profile")"
    transport="$(normalize_transport "${AOITALK_TRANSPORT:-https}")"
    local files=(-f "$SCRIPT_DIR/compose.yml")
    case "$backend" in
        external) files+=(-f "$SCRIPT_DIR/compose.external.yml") ;;
        gemma-vllm) files+=(-f "$SCRIPT_DIR/compose.gemma-vllm.yml") ;;
        deepseek-llamacpp) files+=(-f "$SCRIPT_DIR/compose.deepseek-llamacpp.yml") ;;
        sglang-cuda)
            if [[ -f "$SCRIPT_DIR/compose.sglang.cuda.yml" ]]; then
                files+=(-f "$SCRIPT_DIR/compose.sglang.cuda.yml")
            else
                files+=(-f "$SCRIPT_DIR/compose.sglang.yml")
            fi
            ;;
        *) die "unknown Compose backend: $backend" ;;
    esac
    [[ "$transport" == http-redirect ]] && files+=(-f "$SCRIPT_DIR/compose.http.yml")
    (cd "$SCRIPT_DIR" && docker_cmd compose --env-file "$ENV_FILE" "${files[@]}" "$@")
}

secret_file() {
    printf '%s/%s' "$AOITALK_SECRETS_DIR" "$1"
}

generate_secret_file() {
    local name="$1" path tmp
    path="$(secret_file "$name")"
    [[ ! -L "$path" ]] || die "secret path must not be a symlink: $path"
    if [[ -e "$path" && ! -f "$path" ]]; then
        die "secret path is not a regular file: $path"
    fi
    if [[ -e "$path" ]]; then
        assert_root_owned_file "$path"
    fi
    if [[ -s "$path" ]]; then
        chown root:root "$path"
        chmod 0600 "$path"
        return
    fi
    tmp="$(mktemp "$AOITALK_SECRETS_DIR/.${name}.XXXXXX")"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 48 > "$tmp"
    else
        umask 077
        dd if=/dev/urandom bs=48 count=1 2>/dev/null | base64 > "$tmp"
    fi
    chmod 0600 "$tmp"
    mv -f "$tmp" "$path"
    chown root:root "$path"
}

ensure_optional_file() {
    local name="$1" path
    path="$(secret_file "$name")"
    [[ ! -L "$path" ]] || die "optional secret path must not be a symlink: $path"
    if [[ -e "$path" && ! -f "$path" ]]; then
        die "optional secret path is not a regular file: $path"
    fi
    if [[ -e "$path" ]]; then
        assert_root_owned_file "$path"
    fi
    if [[ ! -e "$path" ]]; then
        : > "$path"
    fi
    chown root:root "$path"
    chmod 0600 "$path"
}

ensure_directories() {
    ensure_root_directory "$AOITALK_INSTALL_ROOT" 0755
    ensure_root_directory "$AOITALK_CONFIG_ROOT" 0755
    ensure_root_directory "$AOITALK_DATA_ROOT" 0755
    ensure_root_directory "$AOITALK_SECRETS_DIR" 0700
    ensure_root_directory "$(dirname "$AOITALK_RUNTIME_CONFIG_FILE")" 0700
    ensure_root_directory "$AOITALK_CADDY_CERTS_DIR" 0755
    local subdir
    for subdir in postgres qdrant qdrant-snapshots caddy caddy/data caddy/config workspaces cache logs tmp huggingface; do
        [[ ! -L "$AOITALK_DATA_ROOT/$subdir" ]] || die "persistent data path must not be a symlink: $AOITALK_DATA_ROOT/$subdir"
        if [[ -e "$AOITALK_DATA_ROOT/$subdir" ]]; then
            [[ -d "$AOITALK_DATA_ROOT/$subdir" ]] || die "persistent data path is not a directory: $AOITALK_DATA_ROOT/$subdir"
        else
            (umask 077; mkdir -p "$AOITALK_DATA_ROOT/$subdir")
            chown root:root "$AOITALK_DATA_ROOT/$subdir"
            chmod 0755 "$AOITALK_DATA_ROOT/$subdir"
        fi
    done
}

validate_persistent_directories() {
    assert_secure_ancestors "$AOITALK_INSTALL_ROOT"
    assert_secure_ancestors "$AOITALK_CONFIG_ROOT"
    assert_secure_ancestors "$AOITALK_DATA_ROOT"
    assert_root_owned_directory "$AOITALK_DATA_ROOT"
    assert_secure_ancestors "$AOITALK_BUNDLE_ROOT"
    assert_root_owned_directory "$AOITALK_BUNDLE_ROOT"
    assert_secure_ancestors "$AOITALK_SECRETS_DIR"
    assert_root_owned_directory "$AOITALK_SECRETS_DIR"
    assert_secure_ancestors "$(dirname "$AOITALK_RUNTIME_CONFIG_FILE")"
    assert_secure_ancestors "$AOITALK_CADDY_CERTS_DIR"
    assert_root_owned_directory "$AOITALK_INSTALL_ROOT"
    assert_root_owned_directory "$AOITALK_CONFIG_ROOT"
    assert_root_owned_directory "$AOITALK_CADDY_CERTS_DIR"
    local subdir
    for subdir in postgres qdrant qdrant-snapshots caddy caddy/data caddy/config workspaces cache logs tmp huggingface; do
        [[ -d "$AOITALK_DATA_ROOT/$subdir" && ! -L "$AOITALK_DATA_ROOT/$subdir" ]] || \
            die "persistent data path is missing or symlinked: $AOITALK_DATA_ROOT/$subdir"
        validate_data_tree_symlinks "$AOITALK_DATA_ROOT/$subdir"
    done
}

validate_data_tree_symlinks() {
    local root="$1" link resolved
    while IFS= read -r link; do
        resolved="$(readlink -f "$link" 2>/dev/null || true)"
        [[ -n "$resolved" && ( "$resolved" == "$AOITALK_DATA_ROOT" || "$resolved" == "$AOITALK_DATA_ROOT"/* ) ]] || \
            die "persistent data symlink must resolve inside AOITALK_DATA_ROOT: $link"
    done < <(find "$root" -type l -print 2>/dev/null)
}

secret_schema_records() {
    # secret-schema.json is the sole source of truth for Enterprise secret
    # file names, requiredness, *_FILE environment names, and provider links.
    # Missing/invalid schema is a hard failure; silently using a hand-written
    # fallback would allow Compose and the launcher to drift apart.
    local schema="$SCRIPT_DIR/secret-schema.json"
    [[ -f "$schema" && ! -L "$schema" ]] || die "canonical secret schema is missing or symlinked: $schema"
    command -v python3 >/dev/null 2>&1 || die "python3 is required to read the canonical secret schema"
    python3 - "$schema" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    document = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"canonical secret schema cannot be parsed: {exc}")
if not isinstance(document, dict) or document.get("schema_version") != 1:
    raise SystemExit("canonical secret schema must declare schema_version=1")
rows = document.get("secrets")
if not isinstance(rows, list) or not rows:
    raise SystemExit("canonical secret schema must contain a non-empty secrets array")
seen_files = set()
seen_envs = set()
for row in rows:
    if not isinstance(row, dict):
        raise SystemExit("canonical secret schema contains a non-object row")
    file_name = row.get("file")
    env_name = row.get("env")
    required = row.get("required")
    provider = row.get("provider", "")
    if not isinstance(file_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,127}", file_name):
        raise SystemExit(f"canonical secret file name is invalid: {file_name!r}")
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_name):
        raise SystemExit(f"canonical secret env name is invalid: {env_name!r}")
    if not isinstance(required, bool):
        raise SystemExit(f"canonical secret required flag is invalid: {file_name}")
    if provider != "" and (not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,127}", provider)):
        raise SystemExit(f"canonical secret provider is invalid: {file_name}")
    if file_name in seen_files or env_name in seen_envs:
        raise SystemExit(f"canonical secret schema contains duplicate names: {file_name}")
    seen_files.add(file_name)
    seen_envs.add(env_name)
    # Tab/newline delimiters are part of this metadata stream and therefore
    # cannot be accepted in a canonical name.
    print(f"{file_name}\t{'true' if required else 'false'}\t{env_name}\t{provider}")
providers = document.get("providers", {})
if not isinstance(providers, dict):
    raise SystemExit("canonical provider metadata must be an object")
for provider_name, details in providers.items():
    if not isinstance(provider_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,127}", provider_name):
        raise SystemExit(f"canonical provider name is invalid: {provider_name!r}")
    if not isinstance(details, dict):
        raise SystemExit(f"canonical provider metadata is invalid: {provider_name}")
    names = [details.get("env"), *(details.get("aliases", []) or [])]
    if any(name not in seen_envs for name in names if isinstance(name, str)):
        raise SystemExit(f"canonical provider metadata references an unknown env: {provider_name}")
PY
}

canonical_secret_names() {
    local records
    records="$(secret_schema_records)" || return 1
    awk -F '\t' 'NF >= 1 && $1 != "" { print $1 }' <<< "$records"
}

canonical_required_secret_names() {
    local records
    records="$(secret_schema_records)" || return 1
    awk -F '\t' '$2 == "true" { print $1 }' <<< "$records"
}

canonical_optional_secret_names() {
    local records
    records="$(secret_schema_records)" || return 1
    awk -F '\t' '$2 == "false" { print $1 }' <<< "$records"
}

secret_env_var_name() {
    local name="$1" records env_name
    records="$(secret_schema_records)" || return 1
    env_name="$(awk -F '\t' -v wanted="$name" '$1 == wanted { print $3; found=1; exit } END { if (!found) exit 1 }' <<< "$records")" || return 1
    printf '%s_FILE\n' "$env_name"
}

validate_secret_files() {
    local name
    mapfile -t names < <(canonical_secret_names)
    for name in "${names[@]}"; do
        [[ -n "$name" ]] || continue
        assert_root_owned_file "$(secret_file "$name")"
    done
}

validate_bundle_files() {
    local file directory
    assert_secure_ancestors "$AOITALK_BUNDLE_ROOT"
    [[ -z "$(find "$AOITALK_BUNDLE_ROOT" -type l -print -quit 2>/dev/null)" ]] || \
        die "bundle contains a symlink; refusing to start from an untrusted tree"
    while IFS= read -r directory; do
        assert_root_owned_directory "$directory"
    done < <(find "$AOITALK_BUNDLE_ROOT" -type d -print 2>/dev/null)
    while IFS= read -r file; do
        assert_root_owned_file "$file"
    done < <(find "$AOITALK_BUNDLE_ROOT" -type f -print 2>/dev/null)
}

yaml_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

write_runtime_config() {
    local mode="${1:-$AOITALK_BACKEND}" provider model base backend
    backend="$(normalize_backend "$mode")"
    [[ ! -L "$AOITALK_RUNTIME_CONFIG_FILE" ]] || die "runtime config must not be a symlink: $AOITALK_RUNTIME_CONFIG_FILE"
    if [[ -s "$AOITALK_RUNTIME_CONFIG_FILE" && "${2:-preserve}" != "regenerate" ]]; then
        assert_root_owned_file "$AOITALK_RUNTIME_CONFIG_FILE"
        return
    fi
    provider="$AOITALK_EXTERNAL_PROVIDER"
    model="$AOITALK_EXTERNAL_MODEL"
    base="$AOITALK_EXTERNAL_BASE_URL"
    case "$backend" in
        gemma-vllm)
            provider="openai_compatible_local"
            model="$AOITALK_GEMMA_SERVED_MODEL"
            base="$AOITALK_GEMMA_VLLM_BASE_URL"
            cat > "$AOITALK_RUNTIME_CONFIG_FILE" <<EOF
# Generated by deploy-compose.sh. Existing database settings remain authoritative.
llm_provider: openai_compatible_local
llm_model: $(yaml_quote "$model")
openai_compatible_local_base_url: $(yaml_quote "$base")
openai_compatible_local:
  server_profile: vllm
  model: $(yaml_quote "$model")
  base_url: $(yaml_quote "$base")
  tools: true
enterprise_deployment:
  backend: gemma-vllm
  active_backend: gemma-vllm
  transport: $(yaml_quote "$AOITALK_TRANSPORT")
  persisted_provider: openai_compatible_local
  persisted_model: $(yaml_quote "$model")
  persisted_base_url: $(yaml_quote "$base")
  effective_provider: openai_compatible_local
  effective_model: $(yaml_quote "$model")
  effective_base_url: $(yaml_quote "$base")
  server_profile: vllm
EOF
            ;;
        deepseek-llamacpp)
            provider="openai_compatible_local"
            model="$AOITALK_DEEPSEEK_SERVED_MODEL"
            base="$AOITALK_DEEPSEEK_BASE_URL"
            cat > "$AOITALK_RUNTIME_CONFIG_FILE" <<EOF
# Generated by deploy-compose.sh. DeepSeek llama.cpp is experimental.
llm_provider: openai_compatible_local
llm_model: $(yaml_quote "$model")
openai_compatible_local_base_url: $(yaml_quote "$base")
openai_compatible_local:
  server_profile: llama.cpp
  model: $(yaml_quote "$model")
  base_url: $(yaml_quote "$base")
  tools: true
enterprise_deployment:
  backend: deepseek-llamacpp
  active_backend: deepseek-llamacpp
  transport: $(yaml_quote "$AOITALK_TRANSPORT")
  persisted_provider: openai_compatible_local
  persisted_model: $(yaml_quote "$model")
  persisted_base_url: $(yaml_quote "$base")
  effective_provider: openai_compatible_local
  effective_model: $(yaml_quote "$model")
  effective_base_url: $(yaml_quote "$base")
  server_profile: llama.cpp
EOF
            ;;
        sglang-cuda)
            cat > "$AOITALK_RUNTIME_CONFIG_FILE" <<EOF
# Generated by deploy-compose.sh. Existing database settings remain authoritative.
llm_provider: openai_compatible_local
llm_model: $(yaml_quote "${SGLANG_MODEL:-google/gemma-4-E4B-it}")
openai_compatible_local_base_url: "http://sglang:30000/v1"
openai_compatible_local:
  server_profile: sglang
  model: $(yaml_quote "${SGLANG_MODEL:-google/gemma-4-E4B-it}")
  base_url: "http://sglang:30000/v1"
  tools: true
enterprise_deployment:
  backend: sglang-cuda
  active_backend: sglang-cuda
  transport: $(yaml_quote "$AOITALK_TRANSPORT")
  persisted_provider: openai_compatible_local
  persisted_model: $(yaml_quote "${SGLANG_MODEL:-google/gemma-4-E4B-it}")
  persisted_base_url: "http://sglang:30000/v1"
  effective_provider: openai_compatible_local
  effective_model: $(yaml_quote "${SGLANG_MODEL:-google/gemma-4-E4B-it}")
  effective_base_url: "http://sglang:30000/v1"
  server_profile: sglang
sglang_base_url: "http://sglang:30000/v1"
sglang:
  model: $(yaml_quote "${SGLANG_MODEL:-google/gemma-4-E4B-it}")
  base_url: "http://sglang:30000/v1"
  host: "sglang"
EOF
            ;;
        external|core)
            if [[ "$provider" == openai_compatible_local ]]; then
                cat > "$AOITALK_RUNTIME_CONFIG_FILE" <<EOF
# Generated by deploy-compose.sh. Existing database settings remain authoritative.
llm_provider: openai_compatible_local
llm_model: $(yaml_quote "$model")
openai_compatible_local_base_url: $(yaml_quote "$base")
openai_compatible_local:
  server_profile: llama.cpp
  model: $(yaml_quote "$model")
  base_url: $(yaml_quote "$base")
  tools: true
  enable_tools: true
  enable_extra_body: true
  llama_cpp:
    auto_start: false
enterprise_deployment:
  backend: external
  active_backend: external
  transport: $(yaml_quote "$AOITALK_TRANSPORT")
  persisted_provider: openai_compatible_local
  persisted_model: $(yaml_quote "$model")
  persisted_base_url: $(yaml_quote "$base")
  effective_provider: openai_compatible_local
  effective_model: $(yaml_quote "$model")
  effective_base_url: $(yaml_quote "$base")
  server_profile: llama.cpp
EOF
            else
                cat > "$AOITALK_RUNTIME_CONFIG_FILE" <<EOF
# Generated by deploy-compose.sh. Existing database settings remain authoritative.
llm_provider: $(yaml_quote "$provider")
llm_model: $(yaml_quote "$model")
enterprise_deployment:
  backend: external
  active_backend: external
  transport: $(yaml_quote "$AOITALK_TRANSPORT")
  persisted_provider: $(yaml_quote "$provider")
  persisted_model: $(yaml_quote "$model")
  persisted_base_url: $(yaml_quote "$base")
  effective_provider: $(yaml_quote "$provider")
  effective_model: $(yaml_quote "$model")
  effective_base_url: $(yaml_quote "$base")
  server_profile: external
${provider}:
  base_url: $(yaml_quote "$base")
  model: $(yaml_quote "$model")
EOF
            fi
            ;;
    esac
    # This file contains no secret values and is mounted read-only into the
    # uid-1000 application container; root-only mode would make startup fail.
    chmod 0644 "$AOITALK_RUNTIME_CONFIG_FILE"
}

init_runtime() {
    require_root init
    validate_settings
    ensure_directories
    command -v flock >/dev/null 2>&1 || die "flock is required to serialize init and first startup"
    local init_lock_file="$AOITALK_SECRETS_DIR/.init.lock" init_lock_fd
    exec {init_lock_fd}>"$init_lock_file"
    chown root:root "$init_lock_file"
    chmod 0600 "$init_lock_file"
    flock -x "$init_lock_fd"
    local required_records optional_records required_names optional_names name
    required_records="$(canonical_required_secret_names)" || die "canonical secret schema required rows could not be read"
    optional_records="$(canonical_optional_secret_names)" || die "canonical secret schema optional rows could not be read"
    mapfile -t required_names <<< "$required_records"
    mapfile -t optional_names <<< "$optional_records"
    for name in "${required_names[@]}"; do
        [[ -n "$name" ]] || continue
        generate_secret_file "$name"
    done
    for name in "${optional_names[@]}"; do
        [[ -n "$name" ]] || continue
        ensure_optional_file "$name"
    done
    [[ -f "$AOITALK_INIT_DB_SQL" ]] || die "missing PostgreSQL init script: $AOITALK_INIT_DB_SQL"
    [[ -d "$AOITALK_BUNDLE_ROOT/config" ]] || die "missing config directory: $AOITALK_BUNDLE_ROOT/config"
    write_runtime_config "$AOITALK_BACKEND" "${1:-preserve}"
    flock -u "$init_lock_fd"
    eval "exec ${init_lock_fd}>&-"
    info "Initialized state under $AOITALK_DATA_ROOT and secret files under $AOITALK_SECRETS_DIR."
}

image_refs() {
    local backend
    backend="$(normalize_backend "${1:-${AOITALK_BACKEND:-external}}")"
    printf '%s\n' \
        "${AOITALK_IMAGE:?AOITALK_IMAGE is required; run the target-side local BuildKit build first}" \
        "${AOITALK_POSTGRES_IMAGE:?AOITALK_POSTGRES_IMAGE is required and must be repo@sha256:<64hex>}" \
        "${AOITALK_QDRANT_IMAGE:?AOITALK_QDRANT_IMAGE is required and must be repo@sha256:<64hex>}" \
        "${AOITALK_CADDY_IMAGE:?AOITALK_CADDY_IMAGE is required and must be repo@sha256:<64hex>}" \
        "${AOITALK_BUSYBOX_IMAGE:?AOITALK_BUSYBOX_IMAGE is required and must be repo@sha256:<64hex>}" \
        "${AOITALK_CURL_IMAGE:?AOITALK_CURL_IMAGE is required and must be repo@sha256:<64hex>}"
    case "$backend" in
        gemma-vllm) printf '%s\n' "${AOITALK_GEMMA_VLLM_IMAGE:?AOITALK_GEMMA_VLLM_IMAGE is required}" ;;
        deepseek-llamacpp) printf '%s\n' "${AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE:?AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE is required}" ;;
        sglang-cuda) printf '%s\n' "${AOITALK_SGLANG_IMAGE:?AOITALK_SGLANG_IMAGE is required}" ;;
    esac
}

verify_image_pins() {
    local manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}"
    [[ -f "$manifest" && ! -L "$manifest" ]] || die "handoff bundle-manifest.json is missing or symlinked: $manifest"
    command -v python3 >/dev/null 2>&1 || die "python3 is required to validate handoff image pins"
    python3 - "$manifest" "$ENV_FILE" "${AOITALK_IMAGE:-}" "${AOITALK_IMAGE_ID:-}" "$HANDOFF_ROOT/source/Dockerfile" <<'PY'
import json, os, re, sys
manifest_path, env_path, app_ref, app_id, dockerfile_path = sys.argv[1:]
try:
    m=json.loads(open(manifest_path, encoding='utf-8').read())
except Exception as exc:
    raise SystemExit(f'invalid handoff manifest: {exc}')
if m.get('format') != 'aoitalk-enterprise-handoff' or int(m.get('version', 0)) != 1:
    raise SystemExit('handoff manifest format/version is unsupported')
pins=m.get('image_pins')
if not isinstance(pins, list) or not pins:
    raise SystemExit('handoff manifest image_pins is missing')
allowed=set(m.get('allowed_image_repositories') or [])
if not allowed: raise SystemExit('handoff manifest allowed_image_repositories is missing')
values={}
for line in open(env_path, encoding='utf-8'):
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,v=line.split('=',1); values[k]=v.strip('"\'')
if not app_ref: app_ref=values.get('AOITALK_IMAGE','')
if not app_id: app_id=values.get('AOITALK_IMAGE_ID','')
expected_keys={'postgres':'AOITALK_POSTGRES_IMAGE','qdrant':'AOITALK_QDRANT_IMAGE','caddy':'AOITALK_CADDY_IMAGE','busybox':'AOITALK_BUSYBOX_IMAGE','curl':'AOITALK_CURL_IMAGE','gemma-vllm':'AOITALK_GEMMA_VLLM_IMAGE','sglang':'AOITALK_SGLANG_IMAGE','deepseek-llamacpp':'AOITALK_DEEPSEEK_LLAMA_CPP_IMAGE'}
expected_bases={'dockerfile-node':'node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436','dockerfile-python':'python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2'}
dependency_count=0
base_count=0
for row in pins:
    if not isinstance(row, dict) or not row.get('name') or not row.get('ref'):
        raise SystemExit('malformed image pin')
    name=row['name']; ref=row['ref']; kind=row.get('kind','dependency')
    if kind == 'local-build':
        if name != 'aoitalk' or not row.get('build_from_source') or not row.get('local_image_id_required') or row.get('image_id_env') != 'AOITALK_IMAGE_ID':
            raise SystemExit('application image pin must use the local-image-id contract')
        if not re.fullmatch(r'aoitalk/enterprise:handoff-[0-9a-f]{12}', ref):
            raise SystemExit('application image local tag is not commit-bound')
        if app_ref and app_ref != ref: raise SystemExit(f'AOITALK_IMAGE must equal the commit-bound local ref {ref}')
        if app_id and not re.fullmatch(r'sha256:[0-9a-f]{64}', app_id): raise SystemExit('AOITALK_IMAGE_ID must be a Docker image ID')
        continue
    if kind == 'build-base':
        if name not in expected_bases or row.get('immutable_digest_required') is not True or ref != expected_bases[name]:
            raise SystemExit(f'dockerfile build base pin mismatch: {name}')
        base_count += 1
        continue
    if kind != 'dependency' or not row.get('immutable_digest_required'):
        raise SystemExit(f'dependency image pin {name} is not immutable')
    if not re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}', ref): raise SystemExit(f'dependency pin is not repository@sha256: {ref}')
    repository=ref.rsplit('@',1)[0]
    if repository not in allowed: raise SystemExit(f'image repository is outside allowlist: {repository}')
    key=expected_keys.get(name)
    if not key: raise SystemExit(f'unknown dependency image pin: {name}')
    value=os.environ.get(key) or values.get(key,'')
    if value != ref: raise SystemExit(f'{key} does not exactly match bundle-manifest.json')
    dependency_count += 1
if dependency_count != len(expected_keys): raise SystemExit('manifest is missing a required dependency image pin')
if base_count != len(expected_bases): raise SystemExit('manifest is missing a required Dockerfile base pin')
try:
    dockerfile=open(dockerfile_path, encoding='utf-8').read()
except Exception as exc:
    raise SystemExit(f'sanitized Dockerfile is missing: {exc}')
for image in expected_bases.values():
    repository, digest = image.split('@', 1)
    if not re.search(rf'^FROM\s+{re.escape(repository)}@{re.escape(digest)}(?:\s+AS\s+[^\s]+)?\s*$', dockerfile, re.MULTILINE | re.IGNORECASE):
        raise SystemExit(f'Dockerfile FROM is not pinned to manifest: {image}')
if re.search(r'^FROM\s+(?:node|python)(?::|\s)', dockerfile, re.MULTILINE | re.IGNORECASE):
    raise SystemExit('Dockerfile contains a mutable node/python FROM tag')
if app_ref and not app_id: raise SystemExit('AOITALK_IMAGE_ID is required for the locally built application image')
print('image pins: dependency RepoDigest/env equality, Dockerfile base digests, and local app image-ID boundary OK')
PY
}

manifest_source_commit() {
    local manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}"
    python3 - "$manifest" <<'PY'
import json, pathlib, re, sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
c=m.get('source_commit','')
if not re.fullmatch(r'[0-9a-f]{40}',str(c)): raise SystemExit('manifest source_commit is invalid')
print(c)
PY
}

set_env_value() {
    local key="$1" value="$2" tmp line found=0
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid generated env key"
    validate_value "$key" "$value"
    tmp="$(mktemp "${ENV_FILE}.handoff.XXXXXX")"
    chmod 0600 "$tmp"; chown root:root "$tmp"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^${key}= ]]; then
            printf '%s=%s\n' "$key" "$value" >> "$tmp"
            found=1
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$ENV_FILE"
    if [[ "$found" == 0 ]]; then printf '%s=%s\n' "$key" "$value" >> "$tmp"; fi
    mv -f -- "$tmp" "$ENV_FILE"
    export "$key=$value"
}

build_app_image() {
    local backend="$(normalize_backend "${1:-$AOITALK_BACKEND}")" commit local_ref secret_path image_id platform
    select_docker
    [[ -f "$AOITALK_BUNDLE_ROOT/Dockerfile" && ! -L "$AOITALK_BUNDLE_ROOT/Dockerfile" ]] || die "sanitized source Dockerfile is missing or symlinked"
    secret_path="$(secret_file nextauth_secret)"
    assert_root_owned_file "$secret_path"
    [[ -s "$secret_path" ]] || die "nextauth_secret must be materialized by init before the BuildKit build"
    commit="$(manifest_source_commit)"
    local_ref="aoitalk/enterprise:handoff-${commit:0:12}"
    # The token is passed only through BuildKit's secret mount. It is never an
    # ARG, shell-expanded command value, build log line, or ZIP input.
    info "Building local Enterprise application image from sanitized source (BuildKit secret mount)"
    docker_cmd build --platform linux/amd64 --pull=false --progress=plain \
        --secret "id=nextauth_secret,src=$secret_path" \
        --tag "$local_ref" "$AOITALK_BUNDLE_ROOT" >/dev/null
    image_id="$(docker_cmd image inspect "$local_ref" --format '{{.Id}}')"
    [[ "$image_id" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || die "local application image did not return a Docker image ID"
    platform="$(docker_cmd image inspect "$local_ref" --format '{{.Os}}/{{.Architecture}}')"
    [[ "$platform" == "linux/amd64" ]] || die "local application image platform is $platform; expected linux/amd64"
    export AOITALK_IMAGE="$local_ref" AOITALK_IMAGE_ID="$image_id"
    set_env_value AOITALK_IMAGE "$local_ref"
    set_env_value AOITALK_IMAGE_ID "$image_id"
    info "Local application image built and recorded by image ID"
}

verify_loaded_image_pins() {
    local backend="$(normalize_backend "${1:-$AOITALK_BACKEND}")" ref repo_digests expected app_id actual_id
    [[ -n "${AOITALK_IMAGE:-}" && -n "${AOITALK_IMAGE_ID:-}" ]] || die "local application image must be built before dependency verification"
    actual_id="$(docker_cmd image inspect "$AOITALK_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
    [[ "$actual_id" == "$AOITALK_IMAGE_ID" ]] || die "local application image ID changed after build"
    while IFS= read -r ref; do
        [[ -n "$ref" ]] || continue
        repo_digests="$(docker_cmd image inspect "$ref" --format '{{json .RepoDigests}}' 2>/dev/null || true)"
        [[ "$repo_digests" == *"$ref"* ]] || die "pulled image RepoDigests do not contain the manifest pin: $ref"
    done < <(image_refs "$backend" | tail -n +2)
}

verify_gemma_model() {
    local backend="$(normalize_backend "${1:-$AOITALK_BACKEND}")" manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}"
    [[ "$backend" == gemma-vllm ]] || return 0
    [[ -f "$manifest" && ! -L "$manifest" ]] || die "handoff manifest is required for Gemma model verification: $manifest"
    [[ -d "$AOITALK_GEMMA_MODEL_DIR" && ! -L "$AOITALK_GEMMA_MODEL_DIR" ]] || die "Gemma model directory is missing or symlinked (download/activate it from the handoff manifest): $AOITALK_GEMMA_MODEL_DIR"
    command -v python3 >/dev/null 2>&1 || die "python3 is required to verify the pinned Gemma model"
    python3 - "$manifest" "$AOITALK_GEMMA_MODEL_DIR" <<'PY'
import hashlib, json, pathlib, re, sys
manifest_path, model_root_text = sys.argv[1:]
m=json.loads(pathlib.Path(manifest_path).read_text(encoding='utf-8'))
d=m.get('model_download') or {}
if d.get('repository') != 'google/gemma-4-E4B-it' or d.get('revision') != 'ee0ef6023621cff504d758262d4e04895a5af4a2':
    raise SystemExit('Gemma repository/revision is not the pinned handoff contract')
if d.get('allow_implicit_download') is not False or d.get('https_required') is not True or d.get('expected_file_count') != 9 or d.get('total_size_bytes') != 16024823729:
    raise SystemExit('Gemma runtime must be offline with implicit download disabled')
root=pathlib.Path(model_root_text)
records=d.get('files')
canonical={
 '.gitattributes': (1570,'34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930'),
 'README.md': (27956,'b21e4f69614ccd77baa2f3797d05311040dee07b989cb9f0d25111aa4b605b2c'),
 'chat_template.jinja': (18569,'0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5'),
 'config.json': (5145,'33b10c02df3c2e8536cf323d29d53262aaa2f4d11dbe19bc729373fbe90295d4'),
 'generation_config.json': (208,'d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de'),
 'model.safetensors': (15992595884,'cfbd3d2f1cd71bd471c37fe2bf8546d5028d41e5736f64e1ca6c6b8893125503'),
 'processor_config.json': (1689,'32bdf45d2ad4cc29a0822ddd157a182de76644f0419a6228d151495256e9813c'),
 'tokenizer.json': (32169626,'cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f'),
 'tokenizer_config.json': (3082,'9f4fec4b1dc6ecddf8f4a92e9caea5971c0e67d81309f3f9066a2bee8c362633'),
}
if not isinstance(records,list) or len(records)!=len(canonical): raise SystemExit('Gemma model contract must contain exactly nine files')
expected={}
expected_casefold={}
for row in records:
    rel=row.get('path'); size=row.get('size_bytes'); sha=row.get('sha256')
    if not isinstance(rel,str) or rel not in canonical or not rel or '\\' in rel or '\x00' in rel or '//' in rel or pathlib.PurePosixPath(rel).is_absolute() or '..' in pathlib.PurePosixPath(rel).parts or any(x in ('','.','..') for x in rel.split('/')): raise SystemExit(f'unsafe/non-canonical model path: {rel!r}')
    key=rel.casefold()
    canonical_size,canonical_sha=canonical[rel]
    if rel in expected or key in expected_casefold or size != canonical_size or str(sha).lower() != canonical_sha: raise SystemExit(f'invalid/duplicate/non-canonical model metadata: {rel!r}')
    expected_casefold[key]=rel
    expected[rel]=(size,sha.lower())
if set(expected) != set(canonical): raise SystemExit('Gemma model path set is not canonical')
actual={}
for p in root.rglob('*'):
    if p.is_symlink(): raise SystemExit(f'model directory contains a symlink: {p}')
    if p.is_file():
        rel=p.relative_to(root).as_posix()
        if '//' in rel or rel.casefold() in actual: raise SystemExit(f'duplicate/case-colliding model path: {rel}')
        actual[rel]=p
if set(actual)!=set(expected): raise SystemExit(f'exact model file coverage mismatch: missing={sorted(set(expected)-set(actual))} extra={sorted(set(actual)-set(expected))}')
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()
for rel,(size,sha) in expected.items():
    p=actual[rel]
    if p.stat().st_size != size: raise SystemExit(f'model size mismatch: {rel}')
    if digest(p) != sha: raise SystemExit(f'model SHA256 mismatch: {rel}')
if expected.get('model.safetensors',(0,''))[0] != 15992595884: raise SystemExit('model.safetensors size pin mismatch')
print('Gemma model: exact coverage/size/SHA256 OK')
PY
}


check_target_prerequisites() {
    local missing=()
    for command_name in python3 curl mktemp flock sha256sum; do
        command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
    done
    if [[ "${1:-runtime}" == "build" ]]; then
        command -v docker >/dev/null 2>&1 || command -v sudo >/dev/null 2>&1 || missing+=(docker)
        if command -v docker >/dev/null 2>&1; then
            docker build --help 2>&1 | grep -q -- '--secret' || missing+=("BuildKit-secret-support")
        fi
        command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || missing+=("docker-compose-v2")
    fi
    ((${#missing[@]} == 0)) || die "target prerequisites are missing: ${missing[*]}"
    info "Target prerequisites: python3/curl/flock/atomic filesystem checks OK"
}

load_hf_token() {
    local token_file="${HF_TOKEN_FILE:-}" mode owner
    if [[ -n "$token_file" ]]; then
        [[ "$token_file" == /* && "$token_file" != *$'\n'* && "$token_file" != *$'\r'* ]] || die "HF_TOKEN_FILE must be an absolute path"
        [[ -f "$token_file" && ! -L "$token_file" ]] || die "HF_TOKEN_FILE must be a regular non-symlink file"
        mode="$(stat -c '%a' "$token_file" 2>/dev/null || true)"; owner="$(stat -c '%u' "$token_file" 2>/dev/null || true)"
        [[ "$mode" =~ ^[0-7]+$ && $((8#$mode & 0077)) -eq 0 ]] || die "HF_TOKEN_FILE must be mode 0600 or stricter"
        [[ "$owner" == "0" || "$owner" == "$(id -u)" ]] || die "HF_TOKEN_FILE must be owned by root or the invoking user"
        HF_TOKEN="$(python3 - "$token_file" <<'PY'
import pathlib, sys
data=pathlib.Path(sys.argv[1]).read_bytes()
if b'\x00' in data: raise SystemExit('HF_TOKEN_FILE contains NUL')
if data.endswith(b'\r\n'):
    data=data[:-2]
elif data.endswith(b'\n'):
    data=data[:-1]
if not data or b'\r' in data or b'\n' in data: raise SystemExit('HF_TOKEN_FILE must contain one token line (optional final LF/CRLF only)')
sys.stdout.buffer.write(data)
PY
)" || die "could not read HF_TOKEN_FILE safely"
    fi
    [[ -n "${HF_TOKEN:-}" && "$HF_TOKEN" != *$'\n'* && "$HF_TOKEN" != *$'\r'* ]] || die "provide HF_TOKEN in the environment or a mode-0600 HF_TOKEN_FILE"
    # The downloader passes the Authorization header through a temporary
    # curl header file. Reject characters that could alter that file rather
    # than ever placing the bearer value in curl's process argv.
    [[ "$HF_TOKEN" != *'"'* && "$HF_TOKEN" != *\\* ]] || die "HF_TOKEN contains unsupported header characters"
    export HF_TOKEN
}

validate_model_url() {
    local url="$1" base="${2:-}" manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}"
    python3 - "$manifest" "$url" "$base" <<'PY'
import ipaddress, json, pathlib, re, sys, urllib.parse
m=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
raw=sys.argv[2]; base=sys.argv[3]
if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in raw):
    raise SystemExit('model redirect URL contains control characters')
url=urllib.parse.urljoin(base, raw) if base else raw
u=urllib.parse.urlparse(url)
allowed=set(m.get('model_download',{}).get('allowed_hosts',[]))
suffixes=set(m.get('model_download',{}).get('allowed_host_suffixes',[]))
host=(u.hostname or '').casefold()
try: ipaddress.ip_address(host); is_ip=True
except ValueError: is_ip=False
host_shape=bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host)) and '..' not in host and not host.endswith('.')
host_ok=host in allowed or any(host.endswith(s) and host != s[1:] for s in suffixes)
if u.scheme != 'https' or not host_shape or is_ip or not host_ok or u.username or u.password or u.fragment or (u.port not in (None,443)):
    raise SystemExit('model URL host/scheme is outside the manifest allowlist')
if len(url) > 16384: raise SystemExit('model redirect URL exceeds response safety limit')
print(url)
PY
}

header_value() {
    local header="$1" name="$2"
    awk -v wanted="${name,,}" 'BEGIN{IGNORECASE=1} tolower($1)==wanted ":" {sub(/^[^:]*:[[:space:]]*/,""); sub(/[\r\n]+$/,""); value=$0} END{if(value!="") print value}' "$header"
}

response_status() {
    awk '/^HTTP\/[0-9.]+[[:space:]]+[0-9]+/ { code=$2 } END { print code+0 }' "$1"
}

resolve_hf_download_url() {
    local source_url="$1" header auth_file status location
    source_url="$(validate_model_url "$source_url")"
    # Keep both response metadata and the one-use bearer header inside the
    # model stage. The downloader's RETURN trap removes that stage on every
    # failure, so an interrupted request cannot leave a token in data roots.
    [[ -n "${stage:-}" && -d "$stage" && ! -L "$stage" ]] || die "model download stage is not ready"
    header="$(mktemp "${stage}/.hf-headers.XXXXXX")"; chmod 0600 "$header"
    auth_file="$(mktemp "${stage}/.hf-auth.XXXXXX")"; chmod 0600 "$auth_file"
    printf 'Authorization: Bearer %s\n' "$HF_TOKEN" > "$auth_file"
    # Authorization is sent exactly once to the pinned Hugging Face resolve
    # endpoint. Redirect locations are checked before any tokenless request.
    if ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --max-redirs 0 \
        --connect-timeout 20 --max-time 120 --header "@$auth_file" \
        -D "$header" -o /dev/null "$source_url"; then
        status="$(response_status "$header")"
    else
        status="$(response_status "$header")"
    fi
    [[ "$status" =~ ^3[0-9][0-9]$ ]] || { rm -f -- "$header" "$auth_file"; die "Hugging Face endpoint did not return a safe redirect (status=$status)"; }
    location="$(header_value "$header" location)"; rm -f -- "$header" "$auth_file"
    [[ -n "$location" ]] || die "Hugging Face redirect did not include Location"
    validate_model_url "$location" "$source_url"
}

bounded_stream_to_file() {
    local destination="$1" expected_size="$2"
    # curl's --max-filesize relies on Content-Length and is not sufficient for
    # chunked/unknown-length responses. This sink enforces expected_size while
    # streaming, using O_EXCL/O_NOFOLLOW and fsync; overflow or disk failure
    # removes the partial file and returns failure through pipefail.
    python3 -c '
import os, sys
destination=sys.argv[1]; maximum=int(sys.argv[2])
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
if hasattr(os,"O_NOFOLLOW"): flags |= os.O_NOFOLLOW
fd=os.open(destination, flags, 0o600)
total=0
try:
    with os.fdopen(fd,"wb") as out:
        while True:
            chunk=sys.stdin.buffer.read(1024*1024)
            if not chunk: break
            total += len(chunk)
            if total > maximum: raise SystemExit("bounded model stream exceeded expected size")
            out.write(chunk)
        out.flush(); os.fsync(out.fileno())
    os.chmod(destination,0o600)
except BaseException:
    try: os.close(fd)
    except OSError: pass
    try: os.unlink(destination)
    except OSError: pass
    raise
if total != maximum: raise SystemExit(f"bounded model stream size mismatch: {total} != {maximum}")
' "$destination" "$expected_size"
}

download_url_without_token() {
    local current="$1" destination="$2" expected_size="$3" hop header status location part
    part="${destination}.part.$$"
    for hop in 0 1 2 3 4 5; do
        header="$(mktemp "${destination}.headers.XXXXXX")"; chmod 0600 "$header"
        rm -f -- "$part"
        if ! curl --proto '=https' --tlsv1.2 --proto-redir '=https' --fail --silent --show-error --max-redirs 0 \
            --connect-timeout 20 --max-time 3600 --max-filesize "$expected_size" \
            -D "$header" -o - "$current" | bounded_stream_to_file "$part" "$expected_size"; then
            status="$(response_status "$header")"
        else
            status="$(response_status "$header")"
        fi
        if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
            [[ -f "$part" && ! -L "$part" ]] || die "bounded model stream did not produce a complete file"
            rm -f -- "$header"; mv -f -- "$part" "$destination"; return 0
        fi
        if [[ "$status" =~ ^3[0-9][0-9]$ ]]; then
            location="$(header_value "$header" location)"; rm -f -- "$header" "$part"
            [[ -n "$location" ]] || die "model redirect hop has no Location"
            current="$(validate_model_url "$location" "$current")"
            continue
        fi
        rm -f -- "$header" "$part"
        die "tokenless model download failed with status=$status"
    done
    rm -f -- "$part"
    die "model redirect chain exceeded six HTTPS hops"
}

verify_model_tree_path_contract() {
    local manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}" root="$1"
    python3 - "$manifest" "$root" <<'PY'
import hashlib, json, pathlib, re, sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
d=m.get('model_download') or {}; records=d.get('files')
if d.get('repository')!='google/gemma-4-E4B-it' or d.get('revision')!='ee0ef6023621cff504d758262d4e04895a5af4a2': raise SystemExit('model contract is not pinned')
if d.get('expected_file_count') != 9 or d.get('total_size_bytes') != 16024823729: raise SystemExit('model contract totals are not pinned')
canonical={
 '.gitattributes': (1570,'34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930'),
 'README.md': (27956,'b21e4f69614ccd77baa2f3797d05311040dee07b989cb9f0d25111aa4b605b2c'),
 'chat_template.jinja': (18569,'0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5'),
 'config.json': (5145,'33b10c02df3c2e8536cf323d29d53262aaa2f4d11dbe19bc729373fbe90295d4'),
 'generation_config.json': (208,'d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de'),
 'model.safetensors': (15992595884,'cfbd3d2f1cd71bd471c37fe2bf8546d5028d41e5736f64e1ca6c6b8893125503'),
 'processor_config.json': (1689,'32bdf45d2ad4cc29a0822ddd157a182de76644f0419a6228d151495256e9813c'),
 'tokenizer.json': (32169626,'cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f'),
 'tokenizer_config.json': (3082,'9f4fec4b1dc6ecddf8f4a92e9caea5971c0e67d81309f3f9066a2bee8c362633'),
}
if not isinstance(records,list) or len(records)!=len(canonical): raise SystemExit('model contract must contain nine files')
expected={}; folded=set()
for row in records:
    rel=row.get('path'); key=rel.casefold() if isinstance(rel,str) else ''
    if not isinstance(rel,str) or rel not in canonical or not rel or '\x00' in rel or '\\' in rel or '//' in rel or pathlib.PurePosixPath(rel).is_absolute() or any(x in ('','.','..') for x in rel.split('/')) or '..' in pathlib.PurePosixPath(rel).parts: raise SystemExit(f'unsafe/non-canonical model path: {rel!r}')
    expected_size,expected_sha=canonical[rel]
    if key in folded or row.get('size_bytes') != expected_size or str(row.get('sha256','')).lower() != expected_sha: raise SystemExit(f'duplicate/non-canonical model metadata: {rel!r}')
    folded.add(key); expected[rel]=(row['size_bytes'],row['sha256'].lower())
if set(expected) != set(canonical): raise SystemExit('model path set is not canonical')
root=pathlib.Path(root)
if root.is_symlink() or not root.is_dir(): raise SystemExit('model root is missing or symlinked')
actual={}; actual_folded=set()
for p in root.rglob('*'):
    if p.is_symlink(): raise SystemExit(f'model tree contains symlink: {p}')
    if p.is_file():
        rel=p.relative_to(root).as_posix()
        if rel.casefold() in actual_folded: raise SystemExit(f'model case-collision: {rel}')
        actual_folded.add(rel.casefold()); actual[rel]=p
if set(actual)!=set(expected): raise SystemExit(f'exact model coverage mismatch: missing={sorted(set(expected)-set(actual))} extra={sorted(set(actual)-set(expected))}')
for rel,(size,sha) in expected.items():
    p=actual[rel]
    if p.stat().st_size != size: raise SystemExit(f'model size mismatch: {rel}')
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    if h.hexdigest()!=sha: raise SystemExit(f'model SHA256 mismatch: {rel}')
print('model tree: exact POSIX coverage/size/SHA256 OK')
PY
}

download_gemma_model() {
    local manifest="${AOITALK_HANDOFF_MANIFEST:-$HANDOFF_ROOT/bundle-manifest.json}" target="$AOITALK_GEMMA_MODEL_DIR" parent leaf stage old records rel url size sha destination total_size available_kb required_kb
    require_root download-model
    check_target_prerequisites runtime
    load_hf_token
    [[ -f "$manifest" && ! -L "$manifest" ]] || die "handoff manifest is missing or symlinked"
    validate_safe_path AOITALK_GEMMA_MODEL_DIR "$target"
    parent="$(dirname "$target")"; leaf="$(basename "$target")"
    assert_secure_ancestors "$parent"
    ensure_root_directory "$parent" 0755
    [[ ! -L "$target" ]] || die "refusing a symlinked model target: $target"
    records="$(python3 - "$manifest" <<'PY'
import ipaddress, json, pathlib, re, sys, urllib.parse
m=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); d=m.get('model_download') or {}
if d.get('repository')!='google/gemma-4-E4B-it' or d.get('revision')!='ee0ef6023621cff504d758262d4e04895a5af4a2': raise SystemExit('model repository/revision is not pinned')
if d.get('total_size_bytes') != 16024823729 or d.get('expected_file_count') != 9: raise SystemExit('model contract totals are not pinned')
allowed=set(d.get('allowed_hosts',[])); suffixes=set(d.get('allowed_host_suffixes',[])); rows=d.get('files')
if allowed != {'huggingface.co','cdn-lfs.huggingface.co','hf.co'} or suffixes != {'.cdn.hf.co','.xethub.hf.co'}: raise SystemExit('model host allowlist is not pinned')
if not isinstance(rows,list) or len(rows)!=9: raise SystemExit('model contract must contain exactly nine files')
seen=set()
for row in rows:
    rel=row.get('path'); url=row.get('url',''); key=rel.casefold() if isinstance(rel,str) else ''
    if not isinstance(rel,str) or not rel or '\x00' in rel or '\\' in rel or '//' in rel or pathlib.PurePosixPath(rel).is_absolute() or any(x in ('','.','..') for x in rel.split('/')) or key in seen: raise SystemExit(f'unsafe/duplicate model path: {rel!r}')
    if not isinstance(row.get('size_bytes'),int) or row['size_bytes']<0 or not re.fullmatch(r'[0-9a-fA-F]{64}',str(row.get('sha256',''))): raise SystemExit(f'invalid model metadata: {rel!r}')
    u=urllib.parse.urlparse(url)
    host=(u.hostname or '').casefold()
    try: ipaddress.ip_address(host); is_ip=True
    except ValueError: is_ip=False
    host_shape=bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host)) and '..' not in host and not host.endswith('.')
    host_ok=host in allowed or any(host.endswith(s) and host != s[1:] for s in suffixes)
    if u.scheme!='https' or not host_shape or is_ip or not host_ok or u.username or u.password or u.port not in (None,443): raise SystemExit(f'unsafe model URL: {url!r}')
    canonical_url=f"https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/{urllib.parse.quote(rel,safe='')}?download=true"
    if url != canonical_url: raise SystemExit(f'non-canonical model URL: {rel!r}')
    seen.add(key); print(f"{rel}\t{url}\t{row['size_bytes']}\t{row['sha256'].lower()}")
PY
)"
    if [[ -d "$target" && ! -L "$target" ]]; then
        if verify_model_tree_path_contract "$target" >/dev/null 2>&1; then
            unset HF_TOKEN; info "Pinned Gemma model already activated; exact hash/size contract reused"; return 0
        fi
    elif [[ -e "$target" ]]; then
        die "model target exists but is not a directory: $target"
    fi
    total_size="$(python3 - "$manifest" <<'PY'
import json, pathlib
d=json.loads(pathlib.Path(__import__('sys').argv[1]).read_text(encoding='utf-8')).get('model_download') or {}
print(d.get('total_size_bytes',0))
PY
)"
    [[ "$total_size" =~ ^[0-9]+$ ]] || die "model total size metadata is invalid"
    available_kb="$(df -Pk "$parent" | awk 'NR==2 {print $4}')"
    required_kb=$(( (total_size + 64*1024*1024 + 1023) / 1024 ))
    [[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb >= required_kb )) || die "insufficient free space for pinned model stage (need at least ${required_kb}KiB)"
    stage="$(mktemp -d "$parent/.${leaf}.stage.XXXXXX")"; chmod 0700 "$stage"; chown root:root "$stage"
    trap 'rm -rf -- "${stage:-}"; unset HF_TOKEN' RETURN
    while IFS=$'\t' read -r rel url size sha; do
        [[ -n "$rel" ]] || continue
        destination="$stage/$rel"
        [[ "$destination" == "$stage"/* ]] || die "model path escaped stage"
        mkdir -p -- "$(dirname "$destination")"; assert_secure_ancestors "$destination"
        [[ ! -e "$destination" && ! -L "$destination" ]] || die "duplicate model destination"
        url="$(resolve_hf_download_url "$url")"
        download_url_without_token "$url" "$destination" "$size"
        [[ "$(stat -c %s "$destination")" == "$size" ]] || die "downloaded model size mismatch: $rel"
        sha256sum "$destination" | awk -v expected="$sha" '$1==expected{ok=1} END{exit ok?0:1}' || die "downloaded model SHA256 mismatch: $rel"
    done <<< "$records"
    verify_model_tree_path_contract "$stage" >/dev/null
    # ZIP/input modes are never trusted.  Set the target directory mode before
    # any replacement so a post-activation chmod failure cannot strand the old
    # model in quarantine.
    chmod 0755 "$stage"
    sync
    if [[ -e "$target" ]]; then
        old="$parent/.${leaf}.old.$$.${RANDOM}"
        [[ ! -e "$old" && ! -L "$old" ]] || die "model replacement quarantine already exists"
        mv -T -- "$target" "$old"
        if ! mv -T -- "$stage" "$target"; then
            # Keep the previous verified model active if activation fails.
            mv -T -- "$old" "$target" || die "model activation failed and previous model could not be restored"
            die "model activation failed; previous model restored"
        fi
        rm -rf -- "$old" || die "new model activated but old quarantine cleanup failed: $old"
    else
        mv -T -- "$stage" "$target"
    fi
    trap - RETURN
    unset HF_TOKEN
    info "Pinned Gemma model downloaded, verified, and atomically activated"
}

build_images() {
    local backend="$(normalize_backend "${1:-$AOITALK_BACKEND}")" services
    select_docker
    apply_manifest_image_defaults
    build_app_image "$backend"
    verify_image_pins "$backend"
    services="aoitalk-storage-init postgres qdrant qdrant-ready caddy"
    case "$backend" in
        gemma-vllm) services+=" gemma-vllm" ;;
        deepseek-llamacpp) services+=" deepseek-llamacpp" ;;
        sglang-cuda) services+=" sglang-cuda" ;;
    esac
    # Pull only immutable dependency services; the aoitalk image was built
    # locally above and must never trigger a registry pull by tag.
    # shellcheck disable=SC2086
    compose_cmd "$backend" pull --quiet $services
    verify_loaded_image_pins "$backend"
}

container_architecture_check() {
    local host_arch
    host_arch="$(docker_cmd info --format '{{.Architecture}}' 2>/dev/null || true)"
    case "$host_arch" in
        amd64|x86_64) ;;
        *) [[ "${AOITALK_ALLOW_NON_AMD64:-false}" == "true" ]] || die "Docker host architecture is $host_arch; this bundle is linux/amd64" ;;
    esac
}

compose_feature_check() {
    local version major minor
    version="$(docker_cmd compose version --short 2>/dev/null || true)"
    version="${version#v}"
    [[ "$version" =~ ^([0-9]+)\.([0-9]+) ]] || die "could not determine Docker Compose v2 version"
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    (( major > 2 || (major == 2 && minor >= 30) )) || \
        die "Docker Compose ${version} is too old; version 2.30+ is required for this bundle"
    docker_cmd compose up --help 2>/dev/null | grep -q -- "--wait" || \
        die "Docker Compose does not support --wait; upgrade Compose before startup"
}

docker_version_check() {
    local version major minor
    version="$(docker_cmd version --format '{{.Server.Version}}' 2>/dev/null || true)"
    version="${version#v}"
    [[ "$version" =~ ^([0-9]+)\.([0-9]+) ]] || die "could not determine Docker Engine version"
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    (( major > 24 || (major == 24 && minor >= 0) )) || \
        die "Docker Engine ${version} is too old; version 24.0+ is required for this bundle"
}

port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -H -ltn "( sport = :$port )" 2>/dev/null | grep -q .
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | grep -q .
    elif [[ -r /proc/net/tcp ]]; then
        local hex_port
        printf -v hex_port '%04X' "$port"
        awk -v needle=":$hex_port" '$2 ~ needle && $4 == "0A" {found=1} END {exit found ? 0 : 1}' \
            /proc/net/tcp /proc/net/tcp6 2>/dev/null
    else
        return 1
    fi
}

check_port_free() {
    local profile="$1" port="$2" label="$3" caddy_id container_port mappings
    case "$label" in
        "public HTTPS") container_port="$AOITALK_HTTPS_PORT" ;;
        "bootstrap HTTPS") container_port=8443 ;;
        "public HTTP") container_port=80 ;;
        *) die "unknown port label: $label" ;;
    esac
    caddy_id="$(compose_cmd "$profile" ps --status running -q caddy 2>/dev/null || true)"
    if [[ -n "$caddy_id" ]]; then
        mappings="$(docker_cmd port "$caddy_id" "${container_port}/tcp" 2>/dev/null || true)"
        if printf '%s\n' "$mappings" | grep -Eq ":${port}$"; then
            return
        fi
    fi
    if port_in_use "$port" && [[ "${AOITALK_ALLOW_PORT_IN_USE:-false}" != "true" ]]; then
        die "$label port $port is already in use (set AOITALK_ALLOW_PORT_IN_USE=true only after an administrator verifies the owner)"
    fi
}

verify_gemma_vllm_backend() {
    local model_file="$AOITALK_GEMMA_MODEL_DIR/$AOITALK_GEMMA_MODEL_FILE" gfx="${AOITALK_GFX_ARCH:-gfx1151}"
    verify_gemma_model gemma-vllm
    [[ "$gfx" == gfx1151 ]] || die "Gemma/vLLM backend requires AMD gfx1151 (reported $gfx)"
    [[ -e /dev/kfd ]] || die "AMD ROCm device /dev/kfd is missing"
    [[ -e /dev/dri ]] || die "AMD DRM device /dev/dri is missing"
    getent group video >/dev/null 2>&1 || die "video group is missing for ROCm device access"
    getent group render >/dev/null 2>&1 || die "render group is missing for ROCm device access"
    [[ -f "$AOITALK_GEMMA_MODEL_DIR/config.json" && ! -L "$AOITALK_GEMMA_MODEL_DIR/config.json" ]] || die "staged Gemma config.json is missing (implicit model download is disabled)"
    find -L "$AOITALK_GEMMA_MODEL_DIR" -type f -name '*.safetensors' -print -quit 2>/dev/null | grep -q . || die "staged Gemma snapshot has no .safetensors weights"
    [[ -f "$model_file" && ! -L "$model_file" ]] || die "staged Gemma model file is missing (implicit model download is disabled): $model_file"
    local actual_size
    actual_size="$(stat -c %s "$model_file" 2>/dev/null || true)"
    [[ "$actual_size" == "$AOITALK_GEMMA_MODEL_SIZE_BYTES" ]] || die "Gemma model file size mismatch: expected=$AOITALK_GEMMA_MODEL_SIZE_BYTES actual=$actual_size"
    if [[ -n "$AOITALK_GEMMA_MODEL_SHA256" ]]; then
        local actual_sha256
        actual_sha256="$(sha256sum "$model_file" | awk '{print $1}')"
        [[ "$actual_sha256" == "$AOITALK_GEMMA_MODEL_SHA256" ]] || die "Gemma model checksum mismatch"
    fi
    if command -v rocminfo >/dev/null 2>&1; then
        rocminfo 2>/dev/null | grep -Fq gfx1151 || die "ROCm does not report gfx1151"
    else
        warn "rocminfo is unavailable; gfx1151 hardware remains unverified until the physical GPU acceptance script runs"
    fi
    docker_cmd run --rm --pull never --network none --device /dev/kfd --device /dev/dri --group-add video --group-add render --entrypoint python "$AOITALK_GEMMA_VLLM_IMAGE" \
        -c 'import sys, torch; sys.exit(0 if torch.cuda.is_available() and torch.version.hip else 1)' >/dev/null 2>&1 || \
        die "ROCm/vLLM image cannot access an AMD GPU; refusing gemma-vllm startup"
}

verify_deepseek_llamacpp_backend() {
    local model_file="$AOITALK_DEEPSEEK_MODEL_DIR/$AOITALK_DEEPSEEK_MODEL_FILE"
    [[ -f "$model_file" && ! -L "$model_file" ]] || die "DeepSeek llama.cpp model is missing (auto-download is disabled): $model_file"
    [[ "$AOITALK_DEEPSEEK_BASE_URL" == http://deepseek-llamacpp:* ]] || die "DeepSeek llama.cpp must stay on the internal network"
    [[ "${AOITALK_DEEPSEEK_RESTART:-no}" != always && "${AOITALK_DEEPSEEK_RESTART:-no}" != unless-stopped ]] || die "DeepSeek llama.cpp restart policy must remain disabled"
    [[ "${AOITALK_DEEPSEEK_DEVICE_COUNT:-1}" != 999 && "${AOITALK_DEEPSEEK_DEVICE_COUNT:-1}" != all ]] || die "DeepSeek llama.cpp refuses unsafe device count"
}

verify_sglang_cuda_backend() {
    command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi is missing; sglang-cuda requires NVIDIA Container Toolkit"
    nvidia-smi >/dev/null 2>&1 || die "NVIDIA GPU is not visible; refusing optional sglang-cuda"
    docker_cmd run --rm --pull never --gpus all --network none --entrypoint python "${AOITALK_SGLANG_IMAGE:?AOITALK_SGLANG_IMAGE is required and must be repo@sha256:<64hex>}" \
        -c 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)' >/dev/null 2>&1 || \
        die "Docker NVIDIA runtime is unavailable to sglang-cuda image"
}

verify_runtime_backend_contract() {
    local backend="$(normalize_backend "${1:-$AOITALK_BACKEND}")" provider base
    provider="$(sed -n 's/^llm_provider:[[:space:]]*//p' "$AOITALK_RUNTIME_CONFIG_FILE" | head -n 1 | tr -d '"' | xargs || true)"
    base="$(sed -n 's/^openai_compatible_local_base_url:[[:space:]]*//p' "$AOITALK_RUNTIME_CONFIG_FILE" | head -n 1 | tr -d '"' | xargs || true)"
    case "$backend" in
        external)
            [[ "$provider" != sglang ]] || die "external backend cannot use stale SGLang runtime config"
            if [[ "$AOITALK_EXTERNAL_PROVIDER" == openai_compatible_local ]]; then
                [[ "$provider" == openai_compatible_local ]] || die "external local-router backend requires provider openai_compatible_local"
                [[ "$base" == "$AOITALK_EXTERNAL_BASE_URL" ]] || die "external local-router runtime base URL mismatch"
                grep -Eq '^  server_profile:[[:space:]]*llama\.cpp[[:space:]]*$' "$AOITALK_RUNTIME_CONFIG_FILE" || \
                    die "external local-router runtime profile must be llama.cpp"
                grep -Eq '^    auto_start:[[:space:]]*false[[:space:]]*$' "$AOITALK_RUNTIME_CONFIG_FILE" || \
                    die "external local-router runtime must set llama_cpp.auto_start=false"
            fi
            ;;
        gemma-vllm)
            [[ "$provider" == openai_compatible_local ]] || die "gemma-vllm requires effective provider openai_compatible_local"
            [[ -z "$base" || "$base" == "$AOITALK_GEMMA_VLLM_BASE_URL" ]] || die "gemma-vllm runtime base URL mismatch"
            ;;
        deepseek-llamacpp)
            [[ "$provider" == openai_compatible_local ]] || die "deepseek-llamacpp requires effective provider openai_compatible_local"
            [[ -z "$base" || "$base" == "$AOITALK_DEEPSEEK_BASE_URL" ]] || die "deepseek-llamacpp runtime base URL mismatch"
            ;;
        sglang-cuda)
            [[ "$provider" == openai_compatible_local || "$provider" == sglang ]] || die "sglang-cuda runtime provider mismatch"
            ;;
    esac
}

preflight() {
    local profile="${1:-${AOITALK_BACKEND:-external}}" ref free_kb min_kb install_free_kb backend transport image_platform
    require_root preflight
    validate_settings
    backend="$(normalize_backend "$profile")"
    transport="$(normalize_transport "${AOITALK_TRANSPORT:-https}")"
    select_docker
    assert_single_inference_backend "$backend"
    docker_cmd compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
    docker_version_check
    compose_feature_check
    container_architecture_check
    [[ -f "$AOITALK_INIT_DB_SQL" && ! -L "$AOITALK_INIT_DB_SQL" ]] || die "missing or symlinked init SQL: $AOITALK_INIT_DB_SQL"
    assert_root_owned_file "$AOITALK_INIT_DB_SQL"
    [[ -d "$AOITALK_BUNDLE_ROOT/config" && ! -L "$AOITALK_BUNDLE_ROOT/config" ]] || die "missing or symlinked config directory: $AOITALK_BUNDLE_ROOT/config"
    assert_root_owned_file "$AOITALK_BUNDLE_ROOT/caddy/Caddyfile.enterprise"
    validate_secret_files
    validate_bundle_files
    [[ -s "$(secret_file postgres_password)" ]] || die "run init before preflight (postgres_password is missing)"
    [[ -s "$AOITALK_RUNTIME_CONFIG_FILE" && ! -L "$AOITALK_RUNTIME_CONFIG_FILE" ]] || die "run init before preflight (runtime config is missing or symlinked)"
    assert_root_owned_file "$AOITALK_RUNTIME_CONFIG_FILE"
    verify_runtime_backend_contract "$backend"
    verify_gemma_model "$backend"
    while IFS= read -r ref; do
        docker_cmd image inspect "$ref" >/dev/null 2>&1 || die "image is not available: $ref (run `build`/`pull` from README.enterprise.md first)"
        image_platform="$(docker_cmd image inspect "$ref" --format '{{.Os}}/{{.Architecture}}')"
        [[ "$image_platform" == linux/amd64 ]] || die "image $ref has platform $image_platform; expected linux/amd64"
    done < <(image_refs "$profile")
    verify_image_pins "$profile"
    check_port_free "$profile" "$AOITALK_HTTPS_PORT" "public HTTPS"
    check_port_free "$profile" "$AOITALK_BOOTSTRAP_HTTPS_PORT" "bootstrap HTTPS"
    if [[ "$transport" == http-redirect ]]; then
        check_port_free "$profile" "$AOITALK_HTTP_PORT" "public HTTP"
    fi
    # Re-check the path boundary on every preflight, including subsequent
    # starts where init is skipped; never let Compose follow a replaced link.
    # Do not chown live application directories here: the running app owns
    # those bind mounts as uid 1000 after the storage-init service completes.
    validate_persistent_directories
    free_kb="$(df -Pk "$AOITALK_DATA_ROOT" | awk 'NR==2 {print $4}')"
    min_kb="$(( ${AOITALK_MIN_FREE_GB:-10} * 1024 * 1024 ))"
    [[ "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb >= min_kb )) || die "less than ${AOITALK_MIN_FREE_GB:-10}GiB is free on the data filesystem"
    install_free_kb="$(df -Pk "$AOITALK_INSTALL_ROOT" | awk 'NR==2 {print $4}')"
    [[ "$install_free_kb" =~ ^[0-9]+$ ]] && (( install_free_kb >= min_kb )) || \
        die "less than ${AOITALK_MIN_FREE_GB:-10}GiB is free on the release filesystem"
    case "$backend" in
        gemma-vllm) verify_gemma_vllm_backend ;;
        deepseek-llamacpp) verify_deepseek_llamacpp_backend ;;
        sglang-cuda) verify_sglang_cuda_backend ;;
    esac
    if [[ "$backend" == external ]]; then
        [[ "$AOITALK_EXTERNAL_BASE_URL" =~ ^https:// ]] || warn "external provider URL is not HTTPS: $AOITALK_EXTERNAL_BASE_URL"
    fi
    compose_cmd "$profile" config --quiet >/dev/null
    info "Preflight passed for profile=$profile, architecture=linux/amd64, data_root=$AOITALK_DATA_ROOT."
}

load_images() {
    require_root load
    validate_settings
    if [[ ! -s "$(secret_file nextauth_secret)" ]]; then init_runtime preserve; fi
    build_images "${1:-${AOITALK_BACKEND:-external}}"
    info "Pinned images built/pulled. Run preflight before startup."
}

up_project() {
    local profile="${1:-${AOITALK_BACKEND:-external}}" transport_arg="${2:-}" backend
    validate_profile "$profile"
    [[ -z "$transport_arg" ]] || AOITALK_TRANSPORT="$transport_arg"
    if [[ "$profile" == http ]]; then
        AOITALK_TRANSPORT=http-redirect
        profile="${AOITALK_BACKEND:-external}"
    fi
    backend="$(normalize_backend "$profile")"
    AOITALK_BACKEND="$backend"
    export AOITALK_BACKEND AOITALK_TRANSPORT
    require_root up
    validate_settings
    if [[ "$backend" == external ]]; then
        # The explicit external profile is a safe escape hatch from a stale
        # SGLang overlay; existing database settings remain authoritative.
        AOITALK_LLM_MODE=external
        if [[ "${AOITALK_PRESERVE_RUNTIME_CONFIG:-false}" == "true" ]]; then
            info "Preserving the existing runtime config for rollback/recovery"
        else
            init_runtime regenerate
        fi
    elif [[ ! -s "$AOITALK_RUNTIME_CONFIG_FILE" ]]; then
        init_runtime
    fi
    select_docker
    # Handoff setup is intentionally target-side: resolve immutable image pins,
    # build the application source, then preflight and start without pull/build
    # flags. No model download is performed here.
    build_images "$backend"
    preflight "$backend"
    info "Refreshing root-owned bind-mount permissions"
    compose_cmd "$backend" rm -f aoitalk-storage-init >/dev/null 2>&1 || true
    compose_cmd "$backend" run --rm --no-deps --pull never aoitalk-storage-init >/dev/null
    info "Starting verified images (no implicit pull/build) with backend=$backend transport=$(normalize_transport "$AOITALK_TRANSPORT")"
    compose_cmd "$backend" up -d --remove-orphans --no-build --pull never --wait
    verify_project "$backend"
}

https_external_smoke() {
    local public_host="${AOITALK_PUBLIC_HOST:-localhost}"
    local smoke_ip="${AOITALK_SMOKE_IP:-192.168.250.100}"
    local normal_code ip_code invalid_host_code sni_free_output sni_free_http sni_free_http_code
    command -v openssl >/dev/null 2>&1 || die "openssl is required for the SNI-less HTTPS smoke test"

    normal_code="$(curl --noproxy '*' -k -sS -o /dev/null -w '%{http_code}' \
        --resolve "${public_host}:${AOITALK_HTTPS_PORT}:127.0.0.1" \
        "https://${public_host}:${AOITALK_HTTPS_PORT}/login" || true)"
    case "$normal_code" in
        2[0-9][0-9]|3[0-9][0-9]|401|403) ;;
        *) die "normal-SNI HTTPS smoke test failed with HTTP $normal_code" ;;
    esac

    # A ClientHello without server_name must still complete TLS. The client
    # may reject the internal CA afterwards; that is a certificate-trust issue,
    # not permission to abort the handshake with an internal TLS error.
    sni_free_output="$(openssl s_client -connect "127.0.0.1:${AOITALK_HTTPS_PORT}" \
        -noservername -brief < /dev/null 2>&1 || true)"
    printf '%s\n' "$sni_free_output" | grep -Eiq 'internal error|unrecognized_name|handshake failure' && \
        die "SNI-less TLS ClientHello was rejected before certificate delivery"
    printf '%s\n' "$sni_free_output" | grep -Eiq 'Protocol([[:space:]]+version)?[[:space:]]*:|Cipher(suite)?[[:space:]]*:' || \
        die "SNI-less TLS ClientHello did not complete a TLS handshake"
    sni_free_http="$(printf 'GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n' "$smoke_ip" | \
        openssl s_client -connect "127.0.0.1:${AOITALK_HTTPS_PORT}" -noservername -quiet 2>&1 || true)"
    sni_free_http_code="$(printf '%s\n' "$sni_free_http" | sed -n 's/^HTTP\/[0-9.]*[[:space:]]\+\([0-9][0-9][0-9]\).*/\1/p' | head -n 1)"
    if [[ "$public_host" == "$smoke_ip" ]]; then
        case "$sni_free_http_code" in
            2[0-9][0-9]|3[0-9][0-9]|401|403) ;;
            *) die "SNI-less IP Host request failed with HTTP $sni_free_http_code" ;;
        esac
    else
        [[ "$sni_free_http_code" == "404" ]] || \
            die "SNI-less IP Host request was not rejected by the Caddy catch-all: HTTP $sni_free_http_code"
    fi

    # Use the requested IP URL while mapping the TCP connection locally. This
    # exercises the same IP/SNI path as an external curl client without making
    # the server test depend on the host's particular LAN address.
    ip_code="$(curl --noproxy '*' -k -sS -o /dev/null -w '%{http_code}' \
        --connect-to "${smoke_ip}:${AOITALK_HTTPS_PORT}:127.0.0.1:${AOITALK_HTTPS_PORT}" \
        "https://${smoke_ip}:${AOITALK_HTTPS_PORT}/login" || true)"
    if [[ "$public_host" == "$smoke_ip" ]]; then
        case "$ip_code" in
            2[0-9][0-9]|3[0-9][0-9]|401|403) ;;
            *) die "IP-address HTTPS curl smoke returned HTTP $ip_code" ;;
        esac
    else
        [[ "$ip_code" == "404" ]] || die "IP-address HTTPS curl was not rejected by the Caddy catch-all: HTTP $ip_code"
    fi

    invalid_host_code="$(curl --noproxy '*' -k -sS -o /dev/null -w '%{http_code}' \
        --resolve "${public_host}:${AOITALK_HTTPS_PORT}:127.0.0.1" \
        -H 'Host: invalid-enterprise-host.invalid' \
        "https://${public_host}:${AOITALK_HTTPS_PORT}/login" || true)"
    [[ "$invalid_host_code" == "404" ]] || \
        die "invalid Host header was not rejected by the Caddy catch-all: HTTP $invalid_host_code"
    info "External HTTPS smoke passed: normal SNI=$normal_code, SNI-less TLS/IP Host=$sni_free_http_code, IP URL=$ip_code, invalid Host=404."
}

verify_external_router() {
    local profile="$1"
    [[ "$AOITALK_EXTERNAL_PROVIDER" == openai_compatible_local ]] || return 0
    # Execute the probe in the application container so Docker DNS, the
    # host-gateway mapping, and the mounted *_FILE secret are tested together.
    # The key is read from the mounted file inside Python; it is never a shell
    # argument, environment value, command log entry, or exception body.
    compose_cmd "$profile" exec -T aoitalk python - <<'PY'
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

base = (os.environ.get("OPENAI_COMPATIBLE_LOCAL_BASE_URL") or "").rstrip("/")
if base != "http://host.docker.internal:18080/v1":
    raise SystemExit("configured local-router base URL is not the Enterprise host endpoint")

required_raw = os.environ.get("AOITALK_EXTERNAL_REQUIRED_MODELS") or ""
required = [item.strip() for item in required_raw.split(",") if item.strip()]
if not required:
    raise SystemExit("AOITALK_EXTERNAL_REQUIRED_MODELS is empty")

headers = {"Accept": "application/json"}
secret_path = os.environ.get("OPENAI_COMPATIBLE_LOCAL_API_KEY_FILE") or ""
if secret_path:
    candidate = Path(secret_path)
    if candidate.is_file():
        token = candidate.read_text(encoding="utf-8").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

# GET <configured-base>/models (the OpenAI-compatible /v1/models endpoint).
# The router endpoint is fixed and the Authorization header is secret-backed;
# fail closed on every redirect so urllib cannot forward that header to a
# different host or scheme.
class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        raise RuntimeError("redirects are disabled for the local-router probe")

request = Request(f"{base}/models", headers=headers, method="GET")
try:
    opener = build_opener(RejectRedirects)
    with opener.open(request, timeout=10) as response:
        payload = json.load(response)
except HTTPError as exc:
    raise SystemExit(f"local-router /v1/models returned HTTP {exc.code}") from None
except (RuntimeError, URLError, OSError, ValueError) as exc:
    raise SystemExit(f"local-router /v1/models probe failed: {type(exc).__name__}") from None

rows = payload.get("data") if isinstance(payload, dict) else None
served = {str(row.get("id")) for row in rows or [] if isinstance(row, dict) and row.get("id")}
missing = [model for model in required if model not in served]
if missing:
    raise SystemExit("local-router is missing required model IDs: " + ",".join(missing))
print("local-router /v1/models OK: " + ",".join(required))
PY
}

verify_project() {
    local profile="${1:-${AOITALK_BACKEND:-external}}" bootstrap_code public_code public_host running_services service container_id health qdrant_id qdrant_ready_id state http_code http_redirect backend transport
    require_root verify
    validate_settings
    backend="$(normalize_backend "$profile")"
    transport="$(normalize_transport "${AOITALK_TRANSPORT:-https}")"
    select_docker
    assert_single_inference_backend "$backend"
    command -v curl >/dev/null 2>&1 || die "curl is required for runtime verification"
    running_services="$(compose_cmd "$profile" ps --status running --services 2>/dev/null || true)"
    for service in postgres aoitalk caddy; do
        printf '%s\n' "$running_services" | grep -Fxq "$service" || die "Compose service is not running: $service"
        container_id="$(compose_cmd "$profile" ps -q "$service" 2>/dev/null || true)"
        [[ -n "$container_id" ]] || die "Compose container ID is missing: $service"
        health="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
        [[ "$health" == "healthy" ]] || die "Compose service is not healthy: $service ($health)"
    done
    qdrant_id="$(compose_cmd "$profile" ps --status running -q qdrant 2>/dev/null || true)"
    [[ -n "$qdrant_id" ]] || die "Compose service is not running: qdrant"
    state="$(docker_cmd inspect --format '{{.State.Status}}' "$qdrant_id" 2>/dev/null || true)"
    [[ "$state" == "running" ]] || die "Compose service is not running: qdrant ($state)"
    printf '%s\n' "$running_services" | grep -Fxq qdrant-ready || die "Qdrant readiness sidecar is not running"
    qdrant_ready_id="$(compose_cmd "$profile" ps --status running -q qdrant-ready 2>/dev/null || true)"
    [[ -n "$qdrant_ready_id" ]] || die "Qdrant readiness sidecar container is missing"
    health="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$qdrant_ready_id" 2>/dev/null || true)"
    [[ "$health" == "healthy" ]] || die "Qdrant readiness sidecar is not healthy: $health"
    case "$backend" in
        gemma-vllm) service=gemma-vllm ;;
        deepseek-llamacpp) service=deepseek-llamacpp ;;
        sglang-cuda) service=sglang-cuda ;;
        *) service="" ;;
    esac
    if [[ -n "$service" ]]; then
        printf '%s\n' "$running_services" | grep -Fxq "$service" || die "inference service is not running: $service"
        container_id="$(compose_cmd "$backend" ps -q "$service" 2>/dev/null || true)"
        health="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
        [[ "$health" == "healthy" ]] || die "inference service is not healthy: $service ($health)"
    fi
    if [[ "$backend" == external && "$AOITALK_EXTERNAL_PROVIDER" == openai_compatible_local ]]; then
        verify_external_router "$profile"
    fi
    bootstrap_code="$(curl -k -sS -o /dev/null -w '%{http_code}' \
        --resolve "localhost:${AOITALK_BOOTSTRAP_HTTPS_PORT}:127.0.0.1" \
        "https://localhost:${AOITALK_BOOTSTRAP_HTTPS_PORT}/login" || true)"
    [[ "$bootstrap_code" =~ ^2[0-9][0-9]$ ]] || die "bootstrap HTTPS check failed with HTTP $bootstrap_code"
    public_host="${AOITALK_PUBLIC_HOST:-localhost}"
    public_code="$(curl -k -sS -o /dev/null -w '%{http_code}' \
        --resolve "${public_host}:${AOITALK_HTTPS_PORT}:127.0.0.1" \
        "https://${public_host}:${AOITALK_HTTPS_PORT}/login" || true)"
    case "$public_code" in
        2[0-9][0-9]|3[0-9][0-9]|401|403) ;;
        *) die "public HTTPS check failed with HTTP $public_code" ;;
    esac
    https_external_smoke
    if [[ "$transport" == http-redirect ]]; then
        http_code="$(curl -k -sS -o /dev/null -w '%{http_code}' \
            --resolve "${public_host}:${AOITALK_HTTP_PORT}:127.0.0.1" \
            "http://${public_host}:${AOITALK_HTTP_PORT}/login" || true)"
        case "$http_code" in
            30[0-9])
                http_redirect="$(curl -k -sS -o /dev/null -w '%{redirect_url}' \
                    --resolve "${public_host}:${AOITALK_HTTP_PORT}:127.0.0.1" \
                    "http://${public_host}:${AOITALK_HTTP_PORT}/login" || true)"
                [[ "$http_redirect" == https://* ]] || die "public HTTP redirect target is not HTTPS: $http_redirect"
                ;;
            401|403) ;;
            *) die "public HTTP redirect check failed with HTTP $http_code" ;;
        esac
    fi
    for path in postgres qdrant qdrant-snapshots caddy/data caddy/config workspaces cache logs huggingface; do
        [[ -d "$AOITALK_DATA_ROOT/$path" ]] || die "persistence directory is missing: $AOITALK_DATA_ROOT/$path"
    done
    info "Runtime verification passed: Compose state, bootstrap TLS, public TLS, external HTTPS clients, and persistence paths."
}

status_project() {
    local profile="${1:-core}"
    select_docker
    compose_cmd "$profile" ps
}

logs_project() {
    local profile="${1:-core}"
    shift || true
    select_docker
    compose_cmd "$profile" logs "$@"
}

down_project() {
    local profile="${1:-core}"
    require_root down
    select_docker
    compose_cmd "$profile" down --remove-orphans
    info "Containers stopped; data under $AOITALK_DATA_ROOT was retained."
}

update_project() {
    local staged_bundle="${1:-}" profile="${2:-external}"
    [[ -n "$staged_bundle" ]] || die "update requires a handoff ZIP path"
    [[ "$staged_bundle" == /* ]] || die "update bundle path must be absolute"
    [[ "${staged_bundle,,}" == *.zip ]] || die "update accepts only the canonical handoff ZIP; directory input is disabled"
    [[ -f "$staged_bundle" && ! -L "$staged_bundle" ]] || die "update ZIP is missing or symlinked"
    validate_profile "$profile"
    require_root update
    [[ -x "$BUNDLE_ROOT/deploy/enterprise/update-on-server.sh" ]] || \
        die "update-on-server.sh is missing from the current release"
    info "Starting Enterprise handoff update: safe ZIP extraction/checksum/manifest -> atomic source activation -> target BuildKit image/dependency pins -> Compose up/preflight/HTTPS smoke"
    "$BUNDLE_ROOT/deploy/enterprise/update-on-server.sh" apply "$staged_bundle" "$AOITALK_INSTALL_ROOT" "$profile"
    local current_root="$(readlink -f "$AOITALK_CURRENT_LINK")" next_launcher="$current_root/source/deploy/enterprise/deploy-compose.sh"
    [[ -x "$next_launcher" ]] || die "activated handoff launcher is missing or not executable"
    # The newly activated release is the only script allowed to build/start it;
    # this keeps source paths, manifest pins, and Compose files consistent.
    AOITALK_ENV_FILE="$ENV_FILE" AOITALK_INSTALL_ROOT="$AOITALK_INSTALL_ROOT" \
        "$next_launcher" up "$profile"
}

rollback_project() {
    local release_id="${1:-}" profile="${2:-external}"
    [[ -n "$release_id" ]] || die "rollback requires a release_id"
    validate_profile "$profile"
    require_root rollback
    "$BUNDLE_ROOT/deploy/enterprise/update-on-server.sh" rollback "$AOITALK_INSTALL_ROOT" "$release_id" "$profile"
}

runtime_yaml_value() {
    local key="$1"
    [[ -f "$AOITALK_RUNTIME_CONFIG_FILE" && ! -L "$AOITALK_RUNTIME_CONFIG_FILE" ]] || return 0
    sed -n "s/^${key}:[[:space:]]*//p" "$AOITALK_RUNTIME_CONFIG_FILE" | head -n 1 | tr -d '"' | xargs || true
}

runtime_yaml_nested_value() {
    local section="$1" key="$2"
    [[ -f "$AOITALK_RUNTIME_CONFIG_FILE" && ! -L "$AOITALK_RUNTIME_CONFIG_FILE" ]] || return 0
    awk -v section="$section" -v key="$key" '
        $0 ~ "^" section ":" {inside=1; next}
        inside && $0 ~ "^[^[:space:]]" {inside=0}
        inside && $0 ~ "^[[:space:]]+" key ":[[:space:]]*" {value=$0; sub("^[[:space:]]+" key ":[[:space:]]*", "", value); gsub("\"", "", value); print value; exit}
    ' "$AOITALK_RUNTIME_CONFIG_FILE" | xargs || true
}

url_safe_parts() {
    local value="$1" scheme host port authority
    scheme="${value%%://*}"
    authority="${value#*://}"
    authority="${authority%%/*}"
    host="$authority"
    if [[ "$host" == *:* ]]; then
        port="${host##*:}"
        host="${host%:*}"
    else
        port=""
    fi
    printf '%s\t%s\t%s\n' "$scheme" "$host" "$port"
}

diagnose_secret_files() {
    local name path owner mode materialized file_env
    mapfile -t names < <(canonical_secret_names)
    for name in "${names[@]}"; do
        [[ -n "$name" ]] || continue
        path="$(secret_file "$name")"
        owner="$(stat -c %u "$path" 2>/dev/null || printf '%s' missing)"
        mode="$(stat -c %a "$path" 2>/dev/null || printf '%s' missing)"
        materialized=false
        [[ -s "$path" ]] && materialized=true
        file_env=false
        # Child containers must receive only the materialized value.  Inspect
        # the Compose config for the `${NAME}_FILE` contract without printing
        # any value or secret content.
        local env_name="${name^^}"
        file_env=true
        printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$owner" "$mode" "$materialized" "$file_env"
    done
}

secret_env_var_name() {
    local name="$1" upper
    upper="${name^^}"
    case "$name" in
        postgres_password) printf 'POSTGRES_PASSWORD_FILE\n' ;;
        nextauth_secret) printf 'NEXTAUTH_SECRET_FILE\n' ;;
        web_auth_secret) printf 'AOITALK_WEB_AUTH_SECRET_FILE\n' ;;
        jwt_secret) printf 'AOITALK_JWT_SECRET_FILE\n' ;;
        app_bridge_secret) printf 'AOITALK_APP_BRIDGE_SECRET_FILE\n' ;;
        caddy_gate_key) printf 'AOITALK_CADDY_GATE_KEY_FILE\n' ;;
        bootstrap_admin_password) printf 'AOITALK_BOOTSTRAP_ADMIN_PASSWORD_FILE\n' ;;
        field_crypto_key_b64) printf 'AOITALK_FIELD_CRYPTO_KEY_B64_FILE\n' ;;
        openai_compatible_local_api_key) printf 'OPENAI_COMPATIBLE_LOCAL_API_KEY_FILE\n' ;;
        *) printf '%s_FILE\n' "$upper" ;;
    esac
}

diagnose_secret_json() {
    local backend="${1:-${AOITALK_BACKEND:-external}}" name path owner mode materialized file_env first=true env_name container_id env_dump
    container_id=""
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        container_id="$(compose_cmd "$backend" ps -q aoitalk 2>/dev/null || true)"
    fi
    env_dump=""
    [[ -n "$container_id" ]] && env_dump="$(compose_cmd "$backend" exec -T aoitalk sh -c 'tr "\0" "\n" < /proc/1/environ' 2>/dev/null || true)"
    mapfile -t names < <(canonical_secret_names)
    for name in "${names[@]}"; do
        [[ -n "$name" ]] || continue
        path="$(secret_file "$name")"
        owner="$(stat -c %u "$path" 2>/dev/null || printf '%s' missing)"
        mode="$(stat -c %a "$path" 2>/dev/null || printf '%s' missing)"
        materialized=false; [[ -s "$path" ]] && materialized=true
        env_name="$(secret_env_var_name "$name")"
        if [[ -z "$container_id" ]]; then
            file_env=unknown
        elif [[ -z "$env_dump" ]]; then
            file_env=unknown
        elif printf '%s\n' "$env_dump" | grep -Fq -- "$env_name="; then
            file_env=present
        elif printf '%s\n' "$env_dump" | grep -Fq -- "${env_name%_FILE}="; then
            file_env=absent
        else
            file_env=unknown
        fi
        [[ "$first" == true ]] || printf ','
        first=false
        printf '{"name":"%s","owner":"%s","mode":"%s","materialized":%s,"file_env_child":"%s"}' "$name" "$owner" "$mode" "$materialized" "$file_env"
    done
}

diagnose() {
    local json=false arg backend transport persisted_provider persisted_model persisted_base effective_provider effective_model effective_base url_parts scheme host port persisted_url_parts persisted_scheme persisted_host persisted_port release_state release_id source_commit db_status db_version alembic_status alembic_current alembic_heads character_status qdrant_status qdrant_version caddy_status caddy_version app_status rocm_status gfx_status vllm_status vllm_models uid gid home schema_line secret_json router_required_models
    [[ "${1:-}" == --json ]] && json=true
    backend="$(normalize_backend "${AOITALK_BACKEND:-$AOITALK_LLM_MODE}")"
    transport="$(normalize_transport "${AOITALK_TRANSPORT:-https}")"
    persisted_provider="$(runtime_yaml_value llm_provider)"
    persisted_model="$(runtime_yaml_value llm_model)"
    persisted_base="$(runtime_yaml_value openai_compatible_local_base_url)"
    [[ -n "$persisted_base" ]] || persisted_base="$(runtime_yaml_nested_value openai_compatible_local base_url)"
    effective_provider="${AOITALK_EFFECTIVE_LLM_PROVIDER:-$persisted_provider}"
    effective_model="$persisted_model"
    effective_base="$persisted_base"
    case "$backend" in
        gemma-vllm) effective_base="$AOITALK_GEMMA_VLLM_BASE_URL"; effective_model="$AOITALK_GEMMA_SERVED_MODEL"; effective_provider=openai_compatible_local ;;
        deepseek-llamacpp) effective_base="$AOITALK_DEEPSEEK_BASE_URL"; effective_model="$AOITALK_DEEPSEEK_SERVED_MODEL"; effective_provider=openai_compatible_local ;;
        sglang-cuda) effective_base="http://sglang:30000/v1"; effective_model="${SGLANG_MODEL:-google/gemma-4E4B-it}"; effective_provider=openai_compatible_local ;;
        external) effective_base="$AOITALK_EXTERNAL_BASE_URL"; effective_model="$AOITALK_EXTERNAL_MODEL"; effective_provider="$AOITALK_EXTERNAL_PROVIDER" ;;
    esac
    router_required_models="${AOITALK_EXTERNAL_REQUIRED_MODELS:-}"
    url_parts="$(url_safe_parts "$effective_base")"
    IFS=$'\t' read -r scheme host port <<< "$url_parts"
    persisted_url_parts="$(url_safe_parts "$persisted_base")"
    IFS=$'\t' read -r persisted_scheme persisted_host persisted_port <<< "$persisted_url_parts"
    release_state="$AOITALK_DATA_ROOT/release-state/current.json"
    release_id="$(if [[ -f "$AOITALK_INSTALL_ROOT/active-release" ]]; then head -n 1 "$AOITALK_INSTALL_ROOT/active-release"; fi)"
    [[ -n "$release_id" ]] || release_id="$(basename "$AOITALK_BUNDLE_ROOT")"
    source_commit="$(if [[ -f "$release_state" ]]; then sed -n 's/.*"source_commit"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$release_state" | head -n 1; fi)"
    [[ -n "$source_commit" ]] || source_commit="$(python3 - "$HANDOFF_ROOT/bundle-manifest.json" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1],encoding="utf-8")).get("source_commit", ""))
except Exception: print("")
PY
)"
    [[ -n "$source_commit" ]] || source_commit="unknown"
    db_status=unknown; db_version=unknown; alembic_status=unknown; alembic_current=unknown; alembic_heads=unknown; character_status=unknown; qdrant_status=unknown; qdrant_version=unknown; caddy_status=unknown; caddy_version=unknown; app_status=unknown; rocm_status=unverified; gfx_status=unverified; vllm_status=unverified; vllm_models=unknown
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        local service_id db_probe alembic_probe character_probe qdrant_probe app_id
        service_id="$(compose_cmd "$backend" ps -q postgres 2>/dev/null || true)"
        if [[ -n "$service_id" ]]; then
            db_probe="$(compose_cmd "$backend" exec -T postgres pg_isready -U aoitalk -d aoitalk_memory 2>/dev/null || true)"
            [[ -n "$db_probe" ]] && db_status=pass || db_status=fail
            db_version="$(compose_cmd "$backend" exec -T postgres psql -U aoitalk -d aoitalk_memory -Atqc 'select current_setting('"'"'server_version'"'"');' 2>/dev/null | head -n 1 | tr -cd '0-9.\n' || printf unknown)"
            [[ -n "$db_version" ]] || db_version=unknown
        fi
        service_id="$(compose_cmd "$backend" ps -q qdrant 2>/dev/null || true)"
        if [[ -n "$service_id" ]]; then
            qdrant_probe="$(compose_cmd "$backend" exec -T aoitalk python -c 'import json,urllib.request; json.load(urllib.request.urlopen("http://qdrant:6333/readyz", timeout=5)); print("ready")' 2>/dev/null || true)"
            [[ "$qdrant_probe" == ready ]] && qdrant_status=pass || qdrant_status=fail
            qdrant_version="$(compose_cmd "$backend" exec -T aoitalk python -c 'import json,urllib.request; print(json.load(urllib.request.urlopen("http://qdrant:6333/", timeout=5)).get("version", "unknown"))' 2>/dev/null | head -n 1 | tr -cd '0-9.\n' || printf unknown)"
            [[ -n "$qdrant_version" ]] || qdrant_version=unknown
        fi
        service_id="$(compose_cmd "$backend" ps -q caddy 2>/dev/null || true)"
        if [[ -n "$service_id" ]]; then
            caddy_status="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$service_id" 2>/dev/null || printf unknown)"
            caddy_version="$(docker_cmd inspect --format '{{.Config.Image}}' "$service_id" 2>/dev/null || printf unknown)"
        fi
        app_id="$(compose_cmd "$backend" ps -q aoitalk 2>/dev/null || true)"
        if [[ -n "$app_id" ]]; then
            app_status="$(compose_cmd "$backend" exec -T aoitalk sh -c 'curl -fsS http://127.0.0.1:3000/health >/dev/null && printf pass || printf fail' 2>/dev/null || printf unknown)"
        fi
        alembic_probe="$(compose_cmd "$backend" exec -T aoitalk python -m alembic current 2>/dev/null || true)"
        [[ -n "$alembic_probe" ]] && alembic_status=pass || alembic_status=fail
        alembic_current="$(printf '%s' "$alembic_probe" | grep -Eo '[0-9]{8}_[0-9]{4}|[0-9a-f]{12,}' | head -n 1 || printf unknown)"
        alembic_heads="$(compose_cmd "$backend" exec -T aoitalk python -m alembic heads 2>/dev/null | grep -Eo '[0-9]{8}_[0-9]{4}|[0-9a-f]{12,}' | head -n 1 || printf unknown)"
        character_probe="$(compose_cmd "$backend" exec -T aoitalk python -c 'import asyncio,sys; from src.services.character_service import get_character_for_prompt; value=asyncio.run(get_character_for_prompt("project_manager")); print("pass" if value else "not_found")' 2>&1 || true)"
        if printf '%s' "$character_probe" | grep -Fxq pass; then character_status=pass
        elif printf '%s' "$character_probe" | grep -Fxq not_found; then character_status=not_found
        elif [[ -n "$character_probe" ]]; then character_status=db_error
        else character_status=fail
        fi
        if [[ "$backend" != external ]]; then
            local inference_service
            inference_service=gemma-vllm
            [[ "$backend" == deepseek-llamacpp ]] && inference_service=deepseek-llamacpp
            [[ "$backend" == sglang-cuda ]] && inference_service=sglang-cuda
            service_id="$(compose_cmd "$backend" ps -q "$inference_service" 2>/dev/null || true)"
            if [[ -n "$service_id" ]]; then
                vllm_status="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$service_id" 2>/dev/null || printf unknown)"
                vllm_models="$(compose_cmd "$backend" exec -T "$inference_service" python -c 'import json,urllib.request; print("pass" if any(x.get("id")=="'"$effective_model"'" for x in json.load(urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5)).get("data", [])) else "mismatch")' 2>/dev/null || printf unknown)"
            fi
        fi
    fi
    [[ -d "$AOITALK_DATA_ROOT/huggingface" ]] && [[ "$backend" != gemma-vllm || -e /dev/kfd ]] && rocm_status=available
    [[ "$backend" == gemma-vllm && "$rocm_status" == available ]] && gfx_status="${AOITALK_GFX_ARCH:-gfx1151}"
    uid="${AOITALK_CONTAINER_UID:-1000}"; gid="${AOITALK_CONTAINER_GID:-1000}"; home="${AOITALK_CONTAINER_HOME:-/home/aoitalk}"
    secret_json="$(diagnose_secret_json "$backend")"
    if [[ "$json" == true ]]; then
        # Every value below is derived metadata; secret values and complete URL
        # paths/query strings are intentionally omitted.
        cat <<EOF
{
  "release": {"id": "$(printf '%s' "$release_id" | sed 's/"/\\"/g')", "source_commit": "$(printf '%s' "$source_commit" | sed 's/"/\\"/g')"},
  "backend": "$(printf '%s' "$backend" | sed 's/"/\\"/g')",
  "transport": "$(printf '%s' "$transport" | sed 's/"/\\"/g')",
  "model": {"directory": "$(printf '%s' "$AOITALK_GEMMA_MODEL_DIR" | sed 's/"/\\"/g')", "required_for_backend": "gemma-vllm", "offline": true},
  "router": {"required_models": "$(printf '%s' "$router_required_models" | sed 's/"/\\"/g')", "operator_owned": true, "auto_start": false},
  "persisted": {"provider": "$(printf '%s' "$persisted_provider" | sed 's/"/\\"/g')", "model": "$(printf '%s' "$persisted_model" | sed 's/"/\\"/g')", "base_url": {"scheme": "$(printf '%s' "$persisted_scheme")", "host": "$(printf '%s' "$persisted_host")", "port": "$(printf '%s' "$persisted_port")"}},
  "effective": {"provider": "$(printf '%s' "$effective_provider" | sed 's/"/\\"/g')", "model": "$(printf '%s' "$effective_model" | sed 's/"/\\"/g')", "base_url": {"scheme": "$(printf '%s' "$scheme")", "host": "$(printf '%s' "$host")", "port": "$(printf '%s' "$port")"}},
  "checks": {"postgres": "${db_status}", "postgres_version": "${db_version}", "alembic": "${alembic_status}", "alembic_current": "${alembic_current}", "alembic_heads": "${alembic_heads}", "character_read": "${character_status}", "qdrant": "${qdrant_status}", "qdrant_version": "${qdrant_version}", "caddy": "${caddy_status}", "caddy_version": "${caddy_version}", "app": "${app_status}", "rocm": "${rocm_status}", "gfx": "${gfx_status}", "vllm": "${vllm_status}", "vllm_models": "${vllm_models}"},
  "runtime_identity": {"uid": $uid, "gid": $gid, "home": "$(printf '%s' "$home" | sed 's/"/\\"/g')"},
  "secret_files": [$secret_json]
}
EOF
    else
        printf 'release=%s source_commit=%s\n' "$release_id" "$source_commit"
        printf 'backend=%s transport=%s\n' "$backend" "$transport"
        if [[ "$backend" == external && "$effective_provider" == openai_compatible_local ]]; then
            printf 'router required_models=%s operator_owned=true auto_start=false\n' "$router_required_models"
        else
            printf 'model directory=%s required_for_backend=gemma-vllm offline=true\n' "$AOITALK_GEMMA_MODEL_DIR"
        fi
        printf 'persisted provider=%s model=%s\n' "$persisted_provider" "$persisted_model"
        printf 'effective provider=%s model=%s url=%s://%s:%s\n' "$effective_provider" "$effective_model" "$scheme" "$host" "$port"
        printf 'checks postgres=%s version=%s alembic=%s current=%s heads=%s character_read=%s qdrant=%s qdrant_version=%s caddy=%s caddy_version=%s app=%s rocm=%s gfx=%s vllm=%s vllm_models=%s\n' "$db_status" "$db_version" "$alembic_status" "$alembic_current" "$alembic_heads" "$character_status" "$qdrant_status" "$qdrant_version" "$caddy_status" "$caddy_version" "$app_status" "$rocm_status" "$gfx_status" "$vllm_status" "$vllm_models"
        printf 'runtime_identity uid=%s gid=%s HOME=%s\n' "$uid" "$gid" "$home"
        printf 'secret_files=materialized-only (values redacted); *_FILE child status=checked\n'
    fi
}

usage() {
    cat <<'EOF'
Usage: deploy-compose.sh <command> [backend|service] [transport]

Commands:
  init [--regenerate-config]  create state, secret files, and runtime overlay
  load                        build/pull images using manifest digest pins
  preflight [backend]         check Docker, immutable image pins, router/model, ports, GPU, disk, and paths
  up [external|gemma-vllm|deepseek-llamacpp|sglang-cuda|http] [https|http-redirect]
                               build/pull pinned images, then start Compose offline
  verify [backend]            check Compose, TLS bootstrap/public endpoints, state, and local-router models
  diagnose [--json]           secret-free release/backend/health diagnostic
  status [backend]            show Compose state
  logs [backend] [service...] show logs
  down [backend]              stop containers without deleting persistent data
  download-model              legacy target-side HF_TOKEN downloader (Gemma/vLLM only)
  prerequisites [runtime|build]
                               fail-closed check of curl/python3, safe filesystem, Docker BuildKit/Compose
  update <handoff.zip> [profile]
                               safe-extract/checksum/manifest, activate, build/pull, preflight, Compose up, HTTPS smoke
  rollback <release-id> [profile]
                              activate an immutable previous release; migrations are not reversed automatically
EOF
}

main() {
    ensure_env_file
    load_env_file
    set_defaults
    apply_manifest_image_defaults
    local command="${1:-}" arg="${2:-}"
    case "$command" in
        init|load|preflight|up|verify|down|download-model|update|rollback)
            if [[ "${AOIT_INTERNAL_OPERATION_LOCK_HELD:-}" != "$AOITALK_INSTALL_ROOT/.operation.lock" ]]; then
                acquire_operation_lock
            fi
            ;;
    esac
    trap 'release_operation_lock' EXIT
    case "$command" in
        init)
            if [[ "$arg" == "--regenerate-config" ]]; then
                init_runtime regenerate
            elif [[ -z "$arg" ]]; then
                init_runtime preserve
            else
                die "init accepts only --regenerate-config"
            fi
            ;;
        load)
            load_images
            ;;
        preflight)
            preflight "${arg:-${AOITALK_BACKEND:-external}}"
            ;;
        up)
            up_project "${arg:-${AOITALK_BACKEND:-external}}" "${3:-}"
            ;;
        verify)
            verify_project "${arg:-${AOITALK_BACKEND:-external}}"
            ;;
        status)
            status_project "${arg:-${AOITALK_BACKEND:-external}}"
            ;;
        logs)
            if [[ -n "$arg" && ( "$arg" == core || "$arg" == external || "$arg" == gemma-vllm || "$arg" == deepseek-llamacpp || "$arg" == sglang || "$arg" == sglang-cuda || "$arg" == http ) ]]; then
                logs_project "$arg" "${@:3}"
            else
                logs_project "${AOITALK_BACKEND:-external}" "${@:2}"
            fi
            ;;
        down)
            down_project "${arg:-${AOITALK_BACKEND:-external}}"
            ;;
        download-model)
            [[ -z "$arg" || "$arg" == gemma-vllm ]] || die "download-model supports only gemma-vllm"
            download_gemma_model
            ;;
        prerequisites)
            check_target_prerequisites "${arg:-runtime}"
            ;;
        diagnose)
            diagnose "${arg:-}"
            ;;
        update)
            update_project "$arg" "${3:-external}"
            ;;
        rollback)
            rollback_project "$arg" "${3:-external}"
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
