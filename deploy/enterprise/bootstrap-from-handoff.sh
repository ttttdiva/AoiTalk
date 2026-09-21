#!/usr/bin/env bash
set -Eeuo pipefail

# Trusted outer-hash bootstrap for a host whose active release still contains
# the historical suffix identity.  This helper is not a ZIP-provided trust
# root: the operator must compare the handoff SHA-256 with the signed/delivered
# release capsule before invoking it.  Once the bytes are authenticated, the
# adoption-capable updater inside the handoff performs its own central-directory,
# manifest, checksum and legacy-lineage checks.

die() { printf '[Enterprise handoff bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

zip="${1:-}"
expected_sha="${2:-}"
install_root="${3:-}"
backend="${4:-external}"
fresh_flag=""
offline_flag=""
offline_zip=""
offline_sha=""
case "${5:-}" in
    "") ;;
    --fresh-install)
        fresh_flag=--fresh-install
        offline_flag="${6:-}"
        offline_zip="${7:-}"
        offline_sha="${8:-}"
        ;;
    --offline-inputs)
        offline_flag=--offline-inputs
        offline_zip="${6:-}"
        offline_sha="${7:-}"
        ;;
    *) die "the fifth argument must be --fresh-install or --offline-inputs" ;;
esac
[[ "$zip" == /* && -f "$zip" && ! -L "$zip" ]] || die "handoff ZIP must be an absolute regular file"
[[ "$expected_sha" =~ ^[0-9a-fA-F]{64}$ ]] || die "expected handoff SHA-256 must be 64 hexadecimal characters"
[[ "$install_root" == /* && "$install_root" != / && "$install_root" != *'//' ]] || die "install root must be an absolute narrow path"
actual_sha="$(sha256sum -- "$zip" | awk '{print $1}')"
[[ "$actual_sha" == "${expected_sha,,}" ]] || die "handoff ZIP SHA-256 does not match the trusted release capsule"
case "$fresh_flag" in
    "") ;;
    --fresh-install) ;;
    *) die "invalid fresh-install bootstrap option" ;;
esac
case "$offline_flag" in
    "")
        [[ -z "$offline_zip" && -z "$offline_sha" ]] || die "offline companion arguments are incomplete"
        ;;
    --offline-inputs)
        [[ "$offline_zip" == /* && -f "$offline_zip" && ! -L "$offline_zip" ]] || die "offline companion ZIP must be an absolute regular file"
        [[ "$offline_sha" =~ ^[0-9a-fA-F]{64}$ ]] || die "expected offline companion SHA-256 must be 64 hexadecimal characters"
        ;;
    *) die "only --offline-inputs may be supplied as the sixth argument" ;;
esac

[[ -d /tmp && ! -L /tmp && "$(stat -c '%u' /tmp 2>/dev/null || true)" == 0 ]] || die "/tmp must be a root-owned directory"
tmp_mode=$((8#$(stat -c '%a' /tmp 2>/dev/null || printf 0)))
if (( tmp_mode & 01000 )); then
    :
elif (( (tmp_mode & 0022) == 0 )); then
    :
else
    die "/tmp must be a root-owned sticky/private directory"
fi
tmp="$(mktemp -d /tmp/enterprise-handoff-bootstrap.XXXXXX)"
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT
cp -- "$zip" "$tmp/handoff.zip"
[[ "$(sha256sum -- "$tmp/handoff.zip" | awk '{print $1}')" == "${expected_sha,,}" ]] || die "handoff ZIP changed while taking the authenticated snapshot"
if [[ "$offline_flag" == "--offline-inputs" ]]; then
    current="$(dirname -- "$offline_zip")"
    while [[ "$current" != "/" ]]; do
        [[ ! -L "$current" ]] || die "offline companion path contains a symlink ancestor: $current"
        current="$(dirname -- "$current")"
    done
    actual_offline_sha="$(sha256sum -- "$offline_zip" | awk '{print $1}')"
    [[ "$actual_offline_sha" == "${offline_sha,,}" ]] || die "offline companion ZIP SHA-256 does not match the trusted release capsule"
    offline_name="$(basename -- "$offline_zip")"
    [[ "$offline_name" =~ ^[A-Za-z0-9_.-]+\.zip$ ]] || die "companion ZIP filename is not canonical"
    offline_snapshot="$tmp/$offline_name"
    cp -- "$offline_zip" "$offline_snapshot"
    [[ "$(sha256sum -- "$offline_snapshot" | awk '{print $1}')" == "${offline_sha,,}" ]] || die "offline companion ZIP changed while taking the authenticated snapshot"
fi
python3 - "$tmp/handoff.zip" "$tmp/update-on-server.sh" <<'PY'
import os, pathlib, re, stat, sys, zipfile
archive = pathlib.Path(sys.argv[1]); output = pathlib.Path(sys.argv[2])
wanted = "source/deploy/enterprise/update-on-server.sh"
seen = set(); payload = None
with zipfile.ZipFile(archive) as bundle:
    for info in bundle.infolist():
        name = info.filename.replace("\\", "/")
        parts = name.rstrip("/").split("/")
        if not parts or name.startswith("/") or re.match(r"^[A-Za-z]:/", name) or any(part in {"", ".", ".."} for part in parts):
            raise SystemExit("unsafe handoff ZIP entry")
        key = "/".join(parts).casefold()
        if key in seen:
            raise SystemExit("duplicate/case-colliding handoff ZIP entry")
        seen.add(key)
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise SystemExit("handoff ZIP contains a symlink or special file")
        if key == wanted.casefold():
            if mode == stat.S_IFDIR or not info.filename.endswith(".sh"):
                raise SystemExit("handoff updater entry is not a regular shell file")
            payload = bundle.read(info)
if payload is None:
    raise SystemExit("handoff updater entry is missing")
fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o700)
with os.fdopen(fd, "wb") as stream:
    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
PY
chmod 0755 "$tmp/update-on-server.sh"
apply_args=(apply)
[[ -n "$fresh_flag" ]] && apply_args+=("$fresh_flag")
if [[ "$offline_flag" == "--offline-inputs" ]]; then
    apply_args+=(--offline-inputs "$offline_snapshot")
fi
apply_args+=("$tmp/handoff.zip" "$install_root" "$backend")
if [[ -n "$fresh_flag" ]]; then
    bash "$tmp/update-on-server.sh" "${apply_args[@]}"
else
    [[ "$offline_flag" == --offline-inputs ]] || die "normal offline update requires its companion ZIP"
    # Invoke only the canonical validator authenticated above. It validates
    # the complete source tree before the candidate launcher can execute.
    stage="$(bash -c 'source "$1"; stage_handoff "$2" "$3"' _ "$tmp/update-on-server.sh" "$tmp/handoff.zip" "$tmp/verified")"
    [[ "$stage" == "$tmp/verified/stage" && -d "$stage/source" && ! -L "$stage" ]] || die "invalid staged handoff path"
    AOITALK_INSTALL_ROOT="$install_root" bash "$stage/source/deploy/enterprise/deploy-compose.sh"         update "$tmp/handoff.zip" "$backend" "$offline_snapshot"
fi
