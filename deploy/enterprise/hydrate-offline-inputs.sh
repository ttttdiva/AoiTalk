#!/usr/bin/env bash
set -Eeuo pipefail

# Hydrate a source-bound Enterprise offline companion into an immutable input
# directory.  The companion is never merged into /opt or /var/lib state and
# this script does not touch the application, database, cache, or secrets.

die() {
    printf '[Enterprise offline inputs] ERROR: %s\n' "$*" >&2
    exit 1
}

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VALIDATOR="$SCRIPT_DIR/offline_inputs.py"
[[ -f "$VALIDATOR" && ! -L "$VALIDATOR" ]] || die "offline input validator is missing or symlinked"

usage() {
    printf 'Usage: %s <companion.zip> <destination-directory> <handoff-bundle-manifest.json>\n' "$0" >&2
}

archive="${1:-}"
destination="${2:-}"
handoff="${3:-}"
[[ -n "$archive" && -n "$destination" && -n "$handoff" ]] || { usage; exit 2; }
[[ "$archive" == /* && "$destination" == /* && "$handoff" == /* ]] || die "archive, destination, and handoff paths must be absolute"
[[ -f "$archive" && ! -L "$archive" ]] || die "companion archive is missing or symlinked: $archive"
[[ -f "$handoff" && ! -L "$handoff" ]] || die "handoff manifest is missing or symlinked: $handoff"

# Hydration creates a new directory atomically.  Existing destinations are
# rejected rather than replaced, so operators can retain a previous verified
# companion for rollback/audit.
python3 "$VALIDATOR" hydrate --archive "$archive" --destination "$destination"
python3 "$VALIDATOR" verify --root "$destination" --handoff "$handoff" --archive "$archive"
printf '[Enterprise offline inputs] verified: %s\n' "$destination"

