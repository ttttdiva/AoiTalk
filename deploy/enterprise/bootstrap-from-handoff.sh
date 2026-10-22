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

# Keep the authenticated archives off /tmp: a companion can exceed tmpfs.
# Do not trust TMPDIR, which may be user-writable or memory-backed.
[[ "$(id -u)" == 0 ]] || die "the trusted bootstrap must run as root"
for parent in /var /var/tmp; do
    [[ -d "$parent" && ! -L "$parent" && "$(stat -c '%u' "$parent")" == 0 ]] || die "$parent must be a root-owned directory"
    mode=$((8#$(stat -c '%a' "$parent")))
    if [[ "$parent" == /var/tmp ]] && (( mode & 01000 )); then
        :
    else
        (( (mode & 0022) == 0 )) || die "$parent must be private or sticky"
    fi
done
case "$(stat -f -c '%T' /var/tmp)" in
    tmpfs|ramfs) die "/var/tmp must use persistent disk-backed storage" ;;
esac
umask 077
tmp="$(mktemp -d /var/tmp/enterprise-handoff-bootstrap.XXXXXX)"
tmp_identity="$(stat -c '%d:%i' -- "$tmp")"
cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ "$tmp" == /var/tmp/enterprise-handoff-bootstrap.* && -d "$tmp" && ! -L "$tmp" && "$(stat -c '%d:%i' -- "$tmp")" == "$tmp_identity" ]]; then
        rm -rf -- "$tmp"
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
offline_snapshot=""
if [[ "$offline_flag" == "--offline-inputs" ]]; then
    offline_name="$(basename -- "$offline_zip")"
    [[ "$offline_name" =~ ^[A-Za-z0-9_.-]+\.zip$ && "$offline_name" != handoff.zip ]] || die "companion ZIP filename is not canonical"
    offline_snapshot="$tmp/$offline_name"
fi
# Pin each input through O_NOFOLLOW directory descriptors. Capacity is checked
# against full logical sizes before any archive copy, not sparse allocation.
python3 - "$tmp" "$zip" "$expected_sha" "$offline_zip" "$offline_sha" "$offline_snapshot" <<'PY'
import hashlib, os, pathlib, stat, sys
root, handoff, handoff_sha, companion, companion_sha, snapshot = sys.argv[1:]

def open_regular(path):
    parts = pathlib.PurePosixPath(path).parts
    if not parts or parts[0] != "/" or any(part in {".", ".."} for part in parts):
        raise SystemExit("archive path is not absolute and canonical")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(source).st_mode):
        os.close(source)
        raise SystemExit("archive must be a regular file")
    return source

inputs = [(handoff, handoff_sha.lower(), os.path.join(root, "handoff.zip"))]
if companion:
    inputs.append((companion, companion_sha.lower(), snapshot))
opened = []
try:
    for source, expected, destination in inputs:
        fd = open_regular(source)
        opened.append((fd, os.fstat(fd), expected, destination))
    required = sum(info.st_size for _, info, _, _ in opened) + 64 * 1024**2
    capacity = os.statvfs(root)
    available = capacity.f_bavail * capacity.f_frsize
    if available < required:
        raise SystemExit(f"insufficient persistent bootstrap capacity: need {required}, available {available}")
    for fd, before, expected, destination in opened:
        target = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(target, "wb") as output:
            while chunk := os.read(fd, 1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SystemExit("archive changed while taking the authenticated snapshot")
        if digest.hexdigest() != expected:
            raise SystemExit("archive SHA-256 does not match the trusted release capsule")
        copied = hashlib.sha256()
        with open(destination, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                copied.update(chunk)
        if copied.hexdigest() != expected:
            raise SystemExit("authenticated snapshot SHA-256 mismatch")
finally:
    for fd, _, _, _ in opened:
        os.close(fd)
PY
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
