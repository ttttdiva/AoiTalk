#!/bin/sh
set -eu

# The JSON file is the one canonical provider/core secret contract.  Parsing
# metadata with the image's Python interpreter avoids maintaining a second
# hand-written list in this root-only launcher.  The parser prints names and
# paths only; secret bytes are never sent to stdout/stderr.
# secret-contract: generated from secret-schema.json (canonical file at deploy/enterprise/secret-schema.json).
SCHEMA_PATH="${AOITALK_SECRET_SCHEMA_FILE:-/app/deploy/enterprise/secret-schema.json}"
if [ ! -f "$SCHEMA_PATH" ] || [ -L "$SCHEMA_PATH" ]; then
    echo "Enterprise secret schema is missing" >&2
    exit 1
fi

if ! schema_rows="$(PYTHONNOUSERSITE=1 python -S - "$SCHEMA_PATH" <<'PY'
import json
import re
import sys

try:
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as stream:
        document = json.load(stream)
    entries = document["secrets"]
    if not isinstance(entries, list) or not entries:
        raise ValueError
    env_re = re.compile(r"^[A-Z][A-Z0-9_]*$")
    file_re = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
    seen_env = set()
    seen_file = set()
    for entry in entries:
        env_name = entry["env"]
        file_name = entry["file"]
        required = entry.get("required", False)
        if (
            not isinstance(env_name, str)
            or not env_re.fullmatch(env_name)
            or env_name in seen_env
            or not isinstance(file_name, str)
            or not file_re.fullmatch(file_name)
            or file_name in seen_file
            or not isinstance(required, bool)
        ):
            raise ValueError
        seen_env.add(env_name)
        seen_file.add(file_name)
        print("\t".join((env_name, file_name, "1" if required else "0")))
except Exception:
    # Deliberately do not expose the schema path, parser exception, or any
    # environment value in a launcher error.
    raise SystemExit(1)
PY
)"; then
    echo "Enterprise secret schema is invalid" >&2
    exit 1
fi

# Docker secrets are mounted before this process starts. Read them while the
# process is root, then drop privileges before starting the application.
# Docker/local secret files are required to be regular, root-owned 0600 files;
# this also rejects symlink swaps and writable group/other paths.
load_secret_file() {
    name="$1"
    file_name="$2"
    required="$3"
    default_path="/run/secrets/$file_name"
    env_file_name="${name}_FILE"
    configured_path="$(printenv "$env_file_name" 2>/dev/null || true)"
    if [ -n "$configured_path" ] && [ "$configured_path" != "$default_path" ]; then
        echo "Enterprise secret path is outside the managed boundary: $name" >&2
        exit 1
    fi
    path="$default_path"

    if [ ! -e "$path" ]; then
        if [ "$required" = "1" ] || [ -n "$configured_path" ]; then
            echo "Unable to read Enterprise secret: $name" >&2
            exit 1
        fi
        unset "$env_file_name"
        return 0
    fi
    if [ -L "$path" ] || [ ! -f "$path" ] || [ ! -r "$path" ]; then
        echo "Unable to read Enterprise secret: $name" >&2
        exit 1
    fi

    metadata="$(stat -c '%u:%g:%a' "$path" 2>/dev/null || true)"
    if [ "$metadata" != "0:0:600" ]; then
        echo "Enterprise secret file has unsafe ownership or mode: $name" >&2
        exit 1
    fi
    size="$(wc -c < "$path" | tr -d '[:space:]')"
    case "$size" in
        ''|*[!0-9]*)
            echo "Unable to inspect Enterprise secret: $name" >&2
            exit 1
            ;;
    esac
    if [ "$size" -gt 8192 ]; then
        echo "Enterprise secret file exceeds the size limit: $name" >&2
        exit 1
    fi
    # od is used only as a quiet byte check, so NUL bytes cannot be silently
    # discarded by POSIX command substitution.
    if LC_ALL=C od -An -tx1 -v "$path" | grep -Eq '(^|[[:space:]])00([[:space:]]|$)'; then
        echo "Enterprise secret file contains an invalid value: $name" >&2
        exit 1
    fi
    if ! value="$(cat "$path")"; then
        echo "Unable to read Enterprise secret: $name" >&2
        exit 1
    fi
    # Command substitution strips terminal newlines. Any remaining newline or
    # carriage return is an unsafe multi-line value and is rejected silently.
    if printf '%s' "$value" | LC_ALL=C grep -q '[\r\n]'; then
        echo "Enterprise secret file contains an invalid value: $name" >&2
        exit 1
    fi
    if [ "$required" = "1" ] && [ -z "$value" ]; then
        echo "Required Enterprise secret is missing: $name" >&2
        exit 1
    fi
    export "$name=$value"
    # The root entrypoint is the only secret-file reader. Python, Next.js, and
    # descendants receive values but cannot retry or discover root-only paths.
    unset "$env_file_name"
}

while IFS="$(printf '\t')" read -r secret_name secret_file secret_required; do
    [ -n "$secret_name" ] || continue
    load_secret_file "$secret_name" "$secret_file" "$secret_required"
done <<EOF
$schema_rows
EOF

# Remove any launcher-specific *_FILE values not present in the canonical
# schema as well. This prevents a future provider addition from leaking a
# root-only path to a descendant before the schema is updated.
for file_env_name in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*_FILE\)=.*/\1/p'); do
    case "$file_env_name" in
        # These are non-secret operator/configuration paths despite their
        # historical *_FILE suffix and must remain available to the app.
        AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE|\
        AOITALK_FIELD_CRYPTO_LOCAL_KEY_FILE|\
        AOITALK_RUNTIME_CONFIG_FILE|\
        AOITALK_ENV_FILE)
            ;;
        *) unset "$file_env_name" ;;
    esac
done

# gosu resolves the named account to uid/gid 1000. Set identity and writable
# user caches before the privilege drop so Python imports (including asyncpg)
# never fall back to /root or a root-owned cache.
export HOME=/home/aoitalk
export USER=aoitalk
export LOGNAME=aoitalk
export XDG_CACHE_HOME=/app/cache/.cache
export XDG_CONFIG_HOME=/app/cache/.config
export XDG_DATA_HOME=/app/cache/.local/share
umask 077
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
chown 1000:1000 "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
chmod 0700 "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"

exec gosu aoitalk python main.py "$@"
