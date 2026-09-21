#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical Enterprise handoff updater.  It accepts only the checksum-covered
# aoitalk-enterprise-handoff ZIP emitted by
# scripts/build_enterprise_handoff.ps1.  model weights and secrets never travel
# through this path; model download is a separate HF_TOKEN operation described
# by README.enterprise.md.

log() { printf '[AoiTalk Enterprise] %s\n' "$*"; }
MANAGED_INSTALLATION_ERROR_HINT=""
die() {
    local message="$*"
    if [[ -n "${MANAGED_INSTALLATION_ERROR_HINT:-}" ]]; then
        message="$message; $MANAGED_INSTALLATION_ERROR_HINT"
    fi
    printf '[AoiTalk Enterprise] ERROR: %s\n' "$message" >&2
    exit 1
}

OPERATION_LOCK_FD=""

# ``apply`` is intentionally a two-state operation.  The ordinary form is a
# source-only update of an already managed installation; destructive cleanup
# is reachable only through the explicit option immediately following
# ``apply``.  Keep the canonical production roots here rather than accepting
# arbitrary /etc or /var/lib paths from the public CLI.
CANONICAL_INSTALL_ROOT="/opt/aoitalk"
CANONICAL_CONFIG_ROOT="/etc/aoitalk"
CANONICAL_DATA_ROOT="/var/lib/aoitalk"
CANONICAL_COMPOSE_PROJECT="aoitalk-enterprise"

APPLY_FRESH_INSTALL=0
APPLY_INPUT=""
APPLY_INSTALL_ROOT=""
APPLY_BACKEND="external"
APPLY_OFFLINE_INPUTS=""

# One historical target-only Enterprise rollout is accepted by the updater as
# a narrowly-scoped compatibility bridge.  This is an allowlist, not a
# wildcard: the source identity, generated branding, manifest schema and
# checksums are validated before any normalization is attempted.
LEGACY_APPROVED_COMMIT="5a36866fa0b70c024d0c2558df90d72ad21d5857"
LEGACY_OVERLAY_SUFFIX="qwen-flash-offlineapt-python-v1-rev10"
LEGACY_RELEASE_PATH=""
LEGACY_RELEASE_COMMIT=""
APPROVED_LEGACY_SOURCE_COMMIT="$LEGACY_APPROVED_COMMIT"
APPROVED_LEGACY_RELEASE_SUFFIX="$LEGACY_OVERLAY_SUFFIX"
# Historical repository tree evidence is retained as an audit constant.  The
# target proves the release with the exact source commit/schema/checksum; it
# never trusts a directory name or an unverified GitHub lookup.
APPROVED_LEGACY_SOURCE_GIT_TREE="85f30781f3ca603c883c1a3562b35c2adb8c0027"
LEGACY_ADOPTION_MARKER_NAME=".enterprise-legacy-adoption-v1.json"
LEGACY_ADOPTION_MARKER_FORMAT="aoitalk-enterprise-legacy-adoption-v1"

require_root_state() {
    [[ "$(id -u)" == 0 ]] || die "state-changing Enterprise updater operations must run as root"
}

assert_secure_ancestors() {
    local path="$1" current owner mode mode_num parent
    current="$(dirname -- "$path")"
    while [[ "$current" != "/" ]]; do
        [[ ! -L "$current" ]] || die "path ancestor is a symlink: $current"
        if [[ -e "$current" ]]; then
            [[ -d "$current" ]] || die "path ancestor is not a directory: $current"
            owner="$(stat -c '%u' -- "$current" 2>/dev/null || true)"
            mode="$(stat -c '%a' -- "$current" 2>/dev/null || true)"
            [[ "$owner" == 0 ]] || die "path ancestor must be root-owned: $current"
            mode_num=$((8#$mode))
            # /tmp is an explicitly supported sticky system scratch parent for
            # tests; all production install ancestors must be non-writable.
            if [[ "$current" == "/tmp" && $((mode_num & 01000)) -ne 0 ]]; then
                :
            else
                (( (mode_num & 0022) == 0 )) || die "path ancestor is group/world writable: $current"
            fi
        fi
        parent="$(dirname -- "$current")"
        [[ "$parent" != "$current" ]] || break
        current="$parent"
    done
}

ensure_secure_directory() {
    local path="$1" mode="$2" owner mode_num
    assert_secure_ancestors "$path"
    [[ ! -L "$path" ]] || die "directory must not be a symlink: $path"
    if [[ -e "$path" ]]; then
        [[ -d "$path" ]] || die "path is not a directory: $path"
        owner="$(stat -c '%u' -- "$path" 2>/dev/null || true)"
        mode_num=$((8#$(stat -c '%a' -- "$path" 2>/dev/null || printf 0)))
        [[ "$owner" == 0 ]] || die "directory must be root-owned: $path"
        (( (mode_num & 0022) == 0 )) || die "directory is group/world writable: $path"
    else
        (umask 077; mkdir -p -- "$path")
        chown root:root -- "$path"
    fi
    chown root:root -- "$path"
    chmod "$mode" -- "$path"
}

assert_current_link_safe() {
    local install_root="$1" current="$1/current" resolved releases="$1/releases"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    [[ ! -e "$current" || -L "$current" ]] || die "current pointer must be a symlink or absent: $current"
    [[ -L "$current" ]] || return 0
    resolved="$(readlink -f -- "$current" 2>/dev/null || true)"
    [[ -n "$resolved" && "$resolved" == "$releases"/* ]] || die "current pointer escapes releases: $current"
    [[ -d "$resolved" && ! -L "$resolved" ]] || die "current target is missing/symlinked: $resolved"
    assert_secure_ancestors "$resolved"
    [[ "$(stat -c '%u' -- "$resolved" 2>/dev/null || true)" == 0 ]] || die "current target must be root-owned: $resolved"
    local resolved_mode=$((8#$(stat -c '%a' -- "$resolved" 2>/dev/null || printf 0)))
    (( (resolved_mode & 0022) == 0 )) || die "current target is group/world writable: $resolved"
}

acquire_operation_lock() {
    local install_root="$1" lock_file
    require_root_state
    is_abs_safe "$install_root" || die "install root must be an absolute narrow path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    assert_secure_ancestors "$install_root"
    ensure_secure_directory "$install_root" 0755
    lock_file="$install_root/.operation.lock"
    if [[ "${AOIT_INTERNAL_OPERATION_LOCK_HELD:-}" == "$lock_file" ]]; then
        [[ -f "$lock_file" && ! -L "$lock_file" ]] || die "inherited operation lock is missing/symlinked"
        [[ "$(stat -c '%u' -- "$lock_file" 2>/dev/null || true)" == 0 ]] || die "inherited operation lock must be root-owned"
        local inherited_mode=$((8#$(stat -c '%a' -- "$lock_file" 2>/dev/null || printf 0)))
        (( (inherited_mode & 0077) == 0 )) || die "inherited operation lock must not be group/world accessible"
        return 0
    fi
    [[ ! -L "$lock_file" ]] || die "operation lock must not be a symlink"
    if [[ -e "$lock_file" ]]; then
        [[ -f "$lock_file" ]] || die "operation lock must be a regular file"
        [[ "$(stat -c '%u' -- "$lock_file" 2>/dev/null || true)" == 0 ]] || die "operation lock must be root-owned"
        local lock_mode=$((8#$(stat -c '%a' -- "$lock_file" 2>/dev/null || printf 0)))
        (( (lock_mode & 0077) == 0 )) || die "operation lock must not be group/world accessible"
    fi
    command -v flock >/dev/null 2>&1 || die "flock is required for state-changing updater operations"
    exec {OPERATION_LOCK_FD}>"$lock_file"
    chown root:root -- "$lock_file"
    chmod 0600 "$lock_file"
    flock -n "$OPERATION_LOCK_FD" || die "another Enterprise updater operation is already running"
}

release_operation_lock() {
    if [[ -n "$OPERATION_LOCK_FD" ]]; then
        flock -u "$OPERATION_LOCK_FD" || true
        eval "exec ${OPERATION_LOCK_FD}>&-"
        OPERATION_LOCK_FD=""
    fi
}

assert_operation_lock_held() {
    local install_root="$1" lock_file="$1/.operation.lock" fd_target lock_mode
    [[ -n "${OPERATION_LOCK_FD:-}" ]] || die "fresh cleanup requires the active operation lock"
    [[ -f "$lock_file" && ! -L "$lock_file" ]] || die "fresh cleanup requires the operation lock"
    [[ "$(stat -c '%u' -- "$lock_file" 2>/dev/null || true)" == 0 ]] || die "fresh cleanup operation lock must be root-owned"
    lock_mode=$((8#$(stat -c '%a' -- "$lock_file" 2>/dev/null || printf 0)))
    (( (lock_mode & 0077) == 0 )) || die "fresh cleanup operation lock must not be group/world accessible"
    [[ -e "/proc/$$/fd/$OPERATION_LOCK_FD" ]] || die "fresh cleanup requires an active operation lock"
    fd_target="$(readlink -f -- "/proc/$$/fd/$OPERATION_LOCK_FD" 2>/dev/null || true)"
    [[ "$fd_target" == "$lock_file" ]] || die "fresh cleanup operation lock is not held for install root"
}

is_abs_safe() {
    local value="$1"
    [[ "$value" == /* && "$value" != / && "$value" != /bin && "$value" != /etc && "$value" != /home && "$value" != /opt && "$value" != /root && "$value" != /tmp && "$value" != /usr && "$value" != /var && "$value" != *'//'* && "$value" != *$'\n'* ]]
}

assert_no_reparse_tree() {
    local root="$1" p
    [[ -d "$root" && ! -L "$root" ]] || die "handoff path is missing or symlinked: $root"
    while IFS= read -r p; do die "handoff contains a symlink/reparse path: $p"; done < <(find -P "$root" -type l -print 2>/dev/null)
}

safe_rel() {
    local path="$1" component
    [[ -n "$path" && "$path" != /* && "$path" != *'\\'* && "$path" != *$'\r'* && "$path" != *$'\n'* ]] || return 1
    IFS='/' read -r -a parts <<< "$path"
    for component in "${parts[@]}"; do [[ -n "$component" && "$component" != . && "$component" != .. ]] || return 1; done
}

is_forbidden_handoff_path() {
    local path="${1//\\//}" lower segment base
    lower="${path,,}"; base="${lower##*/}"
    IFS='/' read -r -a _parts <<< "$lower"
    for segment in "${_parts[@]}"; do
        case "$segment" in
            .git|node_modules|venv|mobile|keys|certs|tokens|data|logs|private|model|secret) return 0 ;;
            .env) return 0 ;;
            .env.*) [[ "$segment" == .env.sample || "$segment" == .env.example ]] || return 0 ;;
        esac
    done
    case "$base" in
        *.key|*.pem|*.p12|*.pfx|*.crt|*.cer|*.token|*.secret) return 0 ;;
    esac
    return 1
}

verify_zip_entries() {
    local archive="$1"
    [[ -f "$archive" && ! -L "$archive" ]] || die "handoff ZIP is missing or symlinked: $archive"
    python3 - "$archive" <<'PY'
import pathlib, re, stat, sys, zipfile
archive=pathlib.Path(sys.argv[1])
seen=set(); total=0
for info in zipfile.ZipFile(archive).infolist():
    raw=info.filename
    name=raw.replace('\\','/')
    if '\x00' in name or name.startswith('/') or re.match(r'^[A-Za-z]:/',name) or '//' in name:
        raise SystemExit(f'unsafe ZIP entry: {raw!r}')
    parts=name.rstrip('/').split('/')
    if any(not part or part in ('.','..') for part in parts): raise SystemExit(f'unsafe ZIP traversal entry: {raw!r}')
    key=name.casefold()
    if key in seen: raise SystemExit(f'duplicate/case-colliding ZIP entry: {raw!r}')
    seen.add(key)
    lower=name.casefold(); segments=lower.rstrip('/').split('/')
    base=segments[-1]
    forbidden={' .git'}
    if any(x in {'.git','node_modules','venv','mobile','keys','certs','tokens','data','logs','private','model','secret','.env'} for x in segments):
        raise SystemExit(f'ZIP entry violates secret boundary: {raw!r}')
    if any(x.startswith('.env.') and x not in {'.env.sample','.env.example'} for x in segments): raise SystemExit(f'ZIP entry violates .env boundary: {raw!r}')
    if re.search(r'\.(key|pem|p12|pfx|crt|cer|token|secret)$',base): raise SystemExit(f'ZIP entry violates secret boundary: {raw!r}')
    mode=(info.external_attr >> 16) & 0o170000
    allowed={0, stat.S_IFREG, stat.S_IFDIR}
    if mode not in allowed: raise SystemExit(f'ZIP entry is a symlink/special file: {raw!r}')
    if raw.endswith('/') and mode not in (0,stat.S_IFDIR): raise SystemExit(f'ZIP directory has regular-file mode: {raw!r}')
    if info.flag_bits & 0x1: raise SystemExit(f'encrypted ZIP entry is not allowed: {raw!r}')
    total += int(info.file_size)
    if total > 8*1024*1024*1024: raise SystemExit('ZIP uncompressed size exceeds safety limit')
if not seen: raise SystemExit('handoff ZIP is empty')
PY
}

safe_extract_zip() {
    local archive="$1" destination="$2"
    python3 - "$archive" "$destination" <<'PY'
import os, pathlib, re, stat, sys, zipfile
archive=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]).resolve()
root.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(root,0o700)
seen=set(); total=0

def reject(path): raise SystemExit(f'unsafe ZIP entry: {path!r}')
def safe_name(raw):
    name=raw.replace('\\','/')
    if '\x00' in name or name.startswith('/') or re.match(r'^[A-Za-z]:/',name) or '//' in name: reject(raw)
    is_dir=name.endswith('/'); trimmed=name.rstrip('/')
    parts=trimmed.split('/') if trimmed else []
    if not parts or any(not x or x in ('.','..') for x in parts): reject(raw)
    key='/'.join(parts).casefold()
    if key in seen: raise SystemExit(f'duplicate/case-colliding ZIP entry: {raw!r}')
    seen.add(key); lower_parts=[x.casefold() for x in parts]; base=lower_parts[-1]
    if any(x in {'.git','node_modules','venv','mobile','keys','certs','tokens','data','logs','private','model','secret','.env'} for x in lower_parts): reject(raw)
    if any(x.startswith('.env.') and x not in {'.env.sample','.env.example'} for x in lower_parts): reject(raw)
    if re.search(r'\.(key|pem|p12|pfx|crt|cer|token|secret)$',base): reject(raw)
    target=root.joinpath(*parts)
    if root not in target.parents and target != root: reject(raw)
    if pathlib.Path(os.path.realpath(target.parent)) != root and root not in pathlib.Path(os.path.realpath(target.parent)).parents: reject(raw)
    return target,is_dir

def ensure_parent(parent):
    current=root
    for part in parent.relative_to(root).parts:
        current=current/part
        if current.exists() and current.is_symlink(): reject(str(current))
        if current.exists() and not current.is_dir(): reject(str(current))
        current.mkdir(mode=0o700,exist_ok=True)
        os.chmod(current,0o700)

with zipfile.ZipFile(archive) as z:
    for info in z.infolist():
        mode=(info.external_attr >> 16) & 0o170000
        if mode not in {0,stat.S_IFREG,stat.S_IFDIR}: reject(info.filename)
        target,is_dir=safe_name(info.filename); ensure_parent(target.parent)
        if is_dir:
            if target.exists() and not target.is_dir(): reject(info.filename)
            target.mkdir(mode=0o700,exist_ok=True); os.chmod(target,0o700); continue
        if target.exists() or target.is_symlink(): reject(info.filename)
        total += int(info.file_size)
        if total > 8*1024*1024*1024: raise SystemExit('ZIP uncompressed size exceeds safety limit')
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
        if hasattr(os,'O_NOFOLLOW'): flags |= os.O_NOFOLLOW
        fd=os.open(target,flags,0o600)
        try:
            with os.fdopen(fd,'wb') as out, z.open(info,'r') as src:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk: break
                    out.write(chunk)
                out.flush(); os.fsync(out.fileno())
        except Exception:
            try: os.close(fd)
            except OSError: pass
            target.unlink(missing_ok=True); raise
        os.chmod(target,0o600)
if any(p.is_symlink() for p in root.rglob('*')): raise SystemExit('extracted ZIP contains a reparse/symlink path')
PY
}

handoff_archive_identity() {
    local archive="$1"
    stat -c '%d:%i:%s:%Y' -- "$archive" 2>/dev/null || true
}

assert_no_symlink_ancestors() {
    local path="$1" current
    current="$(dirname -- "$path")"
    while [[ "$current" != "/" ]]; do
        [[ ! -L "$current" ]] || die "path ancestor is a symlink: $current"
        current="$(dirname -- "$current")"
    done
}

handoff_archive_digest() {
    local archive="$1"
    sha256sum -- "$archive" 2>/dev/null | awk '{print $1}'
}

move_noreplace() {
    local source="$1" destination="$2"
    # Linux renameat2(RENAME_NOREPLACE) is the only operation in this path
    # that guarantees an absent release destination cannot be replaced by a
    # concurrent creator between our check and the rename.  Do not fall back
    # to `mv -T`: it replaces an empty destination directory and can turn a
    # race into a nested/foreign release tree.
    python3 - "$source" "$destination" <<'PY'
import ctypes, errno, os, platform, sys
source, destination = sys.argv[1:]
RENAME_NOREPLACE = 1
AT_FDCWD = -100
libc = ctypes.CDLL(None, use_errno=True)
fn = getattr(libc, 'renameat2', None)
if fn is not None:
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    result = fn(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE)
else:
    syscall_numbers = {'x86_64': 316, 'amd64': 316, 'aarch64': 276, 'arm64': 276, 'armv7l': 382, 'ppc64le': 357, 's390x': 345}
    number = syscall_numbers.get(platform.machine().lower())
    if number is None:
        raise SystemExit('renameat2(RENAME_NOREPLACE) is unavailable on this architecture')
    result = libc.syscall(number, AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE)
if result != 0:
    error = ctypes.get_errno()
    raise SystemExit(f'renameat2(RENAME_NOREPLACE) failed: {os.strerror(error)}')
parent = os.path.dirname(os.path.abspath(destination))
try:
    fd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
except OSError:
    # The rename itself is still atomic; fail closed if durability cannot be
    # confirmed for the release parent.
    raise SystemExit('could not fsync release parent after atomic rename')
PY
}

verify_checksum_file() {
    local root="$1" line hash rel checked
    [[ -f "$root/SHA256SUMS" && ! -L "$root/SHA256SUMS" ]] || die "SHA256SUMS is missing or symlinked"
    local -A expected=() actual=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"; [[ -n "$line" ]] || continue
        [[ "$line" =~ ^([0-9a-fA-F]{64})[[:space:]][[:space:]](.+)$ ]] || die "malformed SHA256SUMS record"
        hash="${BASH_REMATCH[1],,}"; rel="${BASH_REMATCH[2]}"
        safe_rel "$rel" || die "unsafe checksum path: $rel"
        [[ "$rel" != SHA256SUMS ]] || die "SHA256SUMS must not checksum itself"
        [[ -z "${expected[$rel]+present}" ]] || die "duplicate checksum path: $rel"
        expected["$rel"]="$hash"
    done < "$root/SHA256SUMS"
    while IFS= read -r checked; do
        checked="${checked#./}"
        [[ "$checked" != SHA256SUMS ]] || continue
        actual["$checked"]="$(sha256sum "$root/$checked" | awk '{print $1}')"
    done < <(cd "$root" && find . -type f -print | LC_ALL=C sort)
    ((${#expected[@]} == ${#actual[@]})) || die "SHA256SUMS coverage mismatch"
    for rel in "${!expected[@]}"; do
        [[ -n "${actual[$rel]+present}" ]] || die "SHA256SUMS references missing file: $rel"
        [[ "${actual[$rel]}" == "${expected[$rel]}" ]] || die "SHA256SUMS mismatch: $rel"
    done
}

verify_manifest_internal() {
    # Internal validator.  The legacy mode is deliberately not exposed through
    # the public verify_manifest wrapper below; only the exact approved legacy
    # release path can call it after the incoming handoff has passed strict
    # validation.
    local root="$1" mode="${2:-strict}"
    case "$mode" in
        strict|existing-platform-compat|legacy-overlay) ;;
        *) die "internal verify_manifest mode is invalid" ;;
    esac
    [[ -f "$root/bundle-manifest.json" && ! -L "$root/bundle-manifest.json" ]] || die "bundle-manifest.json is missing or symlinked"
    python3 - "$root/bundle-manifest.json" "$root" "$mode" <<'PY'
import ipaddress, json, pathlib, re, sys, urllib.parse
manifest_path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); validation_mode=sys.argv[3]; m=json.loads(manifest_path.read_text(encoding='utf-8'))
def fail(msg): raise SystemExit(msg)
legacy_overlay = validation_mode == 'legacy-overlay'
format_name = m.get('format')
if legacy_overlay:
    format_match = re.fullmatch(r'([a-z][a-z0-9]*)-enterprise-handoff', str(format_name or ''))
    if format_match is None: fail('legacy overlay manifest format is not generated Enterprise branding')
    brand_slug = format_match.group(1)
else:
    if format_name != 'aoitalk-enterprise-handoff': fail('unsupported Enterprise handoff format/version')
    brand_slug = 'aoitalk'
if int(m.get('version',0)) != 1: fail('unsupported Enterprise handoff format/version')
commit=m.get('source_commit','')
if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit): fail('manifest source_commit is invalid')
if legacy_overlay and commit != '5a36866fa0b70c024d0c2558df90d72ad21d5857': fail('legacy overlay source identity is not approved')
if m.get('source_dirty') is not False: fail('handoff source_dirty must be false')
target=m.get('target_build') or {}
if target.get('os') != 'linux' or target.get('source_directory') != 'source' or target.get('build_required_on_target') is not True: fail('target_build contract is not linux/amd64 sanitized-source')
if validation_mode == 'strict':
    if target.get('architecture') != 'linux/amd64': fail('target_build contract is not linux/amd64 sanitized-source')
elif validation_mode == 'legacy-overlay':
    if target.get('architecture') != 'linux/amd64': fail('target_build contract is not linux/amd64 sanitized-source')
else:
    # Only the architecture field is grandfathered for an already activated
    # release. Every other target-build invariant remains strict, and an
    # explicitly present foreign/null/empty architecture is never accepted.
    if 'architecture' in target and target.get('architecture') != 'linux/amd64':
        fail('target_build contract is not linux/amd64 sanitized-source')
source_tree=m.get('source_tree') or {}
if source_tree.get('kind')!='git-archive' or source_tree.get('path')!='source' or source_tree.get('commit')!=commit or source_tree.get('dirty') is not False or source_tree.get('sanitized') is not True or source_tree.get('import_closure_verified') is not True or source_tree.get('build_context_safe') is not True: fail('source tree HEAD/sanitization evidence is missing')
backend=m.get('backend') or {}
if backend.get('default') not in {'external','gemma-vllm','deepseek-llamacpp','sglang-cuda'}: fail('backend default is invalid')
if not set(backend.get('supported') or {}) >= {'external','gemma-vllm','deepseek-llamacpp','sglang-cuda'}: fail('backend supported set is incomplete')
transport=m.get('transport') or {}
if transport.get('default') not in {'https','http-redirect'} or not set(transport.get('supported') or {}) >= {'https','http-redirect'}: fail('transport contract is invalid')
# The timezone contract is required for every newly generated handoff.  An
# existing-platform compatibility check deliberately allows an older release
# with no runtime_contract.timezone entry to remain active; if that older
# manifest does declare the entry, validate it rather than silently accepting a
# contradictory value.  The approved legacy-overlay path is grandfathered by
# design and follows its historical manifest contract.
if validation_mode == 'strict':
    producer=m.get('offline_producer')
    durable=m.get('durable_state')
    if not isinstance(producer,dict) or type(producer.get('version')) is not int or producer.get('version') != 1:
        fail('formal offline producer contract is missing')
    if producer.get('entrypoint') != 'source/deploy/enterprise/produce_offline_inputs.py' or producer.get('network_proof') != 'none' or producer.get('dockerfile_frontend') != 'dockerfile.v0':
        fail('formal offline producer interface is unsupported')
    if not isinstance(durable,dict) or type(durable.get('version')) is not int or durable.get('version') != 1 or durable.get('pointer_only_rollback') is not False:
        fail('formal durable rollback contract is missing')
    if durable.get('entrypoint') != 'source/deploy/enterprise/durable_state.py' or set(durable.get('stores', [])) != {'postgres','qdrant','release-state'}:
        fail('formal durable rollback interface is unsupported')
    for name in ('enterprise_release_common.py','offline_registry_source.py','collect_offline_deps.py','collect_offline_npm.mjs','produce_offline_inputs.py','durable_state.py','durable-lifecycle.sh','finalize_offline_handoff.py'):
        helper='deploy/enterprise/'+name
        if helper not in (m.get('required_files') or []) or not (root/'source'/helper).is_file() or (root/'source'/helper).is_symlink():
            fail('formal release interface is not required/present: '+helper)
runtime_contract=m.get('runtime_contract')
if validation_mode == 'strict' and not isinstance(runtime_contract,dict):
    fail('runtime timezone contract is missing')
if runtime_contract is not None and not isinstance(runtime_contract,dict):
    fail('runtime_contract is invalid')
timezone_contract=runtime_contract.get('timezone') if isinstance(runtime_contract,dict) else None
if validation_mode == 'strict' and not isinstance(timezone_contract,dict):
    fail('runtime timezone contract is missing')
if not legacy_overlay and timezone_contract is not None:
    expected_timezone_contract={
        'env':'AOITALK_TIMEZONE',
        'default':'Asia/Tokyo',
        'format':'IANA',
        'python':'TZ',
        'node':'TZ',
        'postgres':'timezone',
        'tzdata_required':True,
        'data_timestamp_migration':False,
    }
    if timezone_contract != expected_timezone_contract:
        fail('runtime timezone contract is invalid')
router=m.get('router') or {}
default_model=router.get('default_model')
if (router.get('ownership') != 'operator-owned' or router.get('provider') != 'openai_compatible_local' or router.get('base_url') != 'http://host.docker.internal:18080/v1' or router.get('auto_start') is not False or router.get('model_artifacts_included') is not False): fail('router ownership contract is invalid')
if default_model != 'qwen3.8-27b': fail('router ownership contract is invalid')
required_router=router.get('required_models')
if legacy_overlay:
    if required_router != ['qwen3.8-27b','gemma-4-26b-a4b-it-qat-q4-0']: fail('legacy router required model contract is not qwen3.8-27b,gemma-4-26b-a4b-it-qat-q4-0')
elif required_router != ['qwen3.8-27b','qwen3.8-flash-next']:
    fail('router required model contract must be qwen3.8-27b,qwen3.8-flash-next')
router_models=router.get('models')
if not isinstance(router_models,list): fail('router model metadata is missing')
router_model_by_id={}
for row in router_models:
    if not isinstance(row,dict): fail('router model metadata row is invalid')
    model_id=row.get('id')
    if not isinstance(model_id,str) or not model_id or model_id in router_model_by_id: fail('router model metadata contains an invalid or duplicate model ID')
    router_model_by_id[model_id]=row
current_router_models={
    'qwen3.8-27b': {'repository':'unsloth/Qwen3.8-27B-GGUF','quantization':'UD-Q4_K_XL','filename':'Qwen3.8-27B-UD-Q4_K_XL.gguf','context':32768,'role':'main','required':True},
    'qwen3.8-flash-next': {'repository':'unsloth/Qwen3.8-Flash-Next-GGUF','quantization':'UD-IQ4_XS','filename':'Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf','context':16384,'role':'fast','required':True},
    'gemma-4-26b-a4b-it-qat-q4-0': {'repository':'google/gemma-4-26B-A4B-it-qat-q4_0-gguf','quantization':'QAT Q4_0','filename':'gemma-4-26B_q4_0-it.gguf','context':32768,'role':'optional','required':False},
}

historical_router_models={
    'qwen3.8-27b': {'repository':'unsloth/Qwen3.8-27B-GGUF','quantization':'UD-Q4_K_XL','filename':'Qwen3.8-27B-UD-Q4_K_XL.gguf','context':32768,'role':'main'},
    'gemma-4-26b-a4b-it-qat-q4-0': {'repository':'google/gemma-4-26B-A4B-it-qat-q4_0-gguf','quantization':'QAT Q4_0','filename':'gemma-4-26B_q4_0-it.gguf','context':32768,'role':'fast'},
}
expected_router_models = historical_router_models if legacy_overlay else current_router_models
if set(router_model_by_id) != set(expected_router_models): fail('router model catalog set is invalid')
for model_id, expected in expected_router_models.items():
    actual=router_model_by_id[model_id]
    for key, value in expected.items():
        if actual.get(key) != value: fail(f'router model metadata mismatch: {model_id}.{key}')
    if legacy_overlay and set(actual) != {'id','repository','quantization','filename','context','role'}:
        fail(f'legacy router model metadata has unexpected fields: {model_id}')
model=m.get('model_download') or {}
if model.get('repository')!='google/gemma-4-E4B-it' or model.get('revision')!='ee0ef6023621cff504d758262d4e04895a5af4a2': fail('model repository/revision is not pinned')
if model.get('token_env')!='HF_TOKEN' or model.get('allow_implicit_download') is not False or model.get('https_required') is not True or model.get('exact_file_coverage') is not True: fail('model download contract is not offline/HTTPS-bound')
hosts=model.get('allowed_hosts')
suffixes=model.get('allowed_host_suffixes')
redirect_hosts=model.get('redirect_host_allowlist')
redirect_suffixes=model.get('redirect_host_suffix_allowlist')
if not isinstance(hosts,list) or set(hosts)!={'huggingface.co','cdn-lfs.huggingface.co','hf.co'}: fail('model exact host allowlist is not pinned')
if not isinstance(suffixes,list) or set(suffixes)!={'.cdn.hf.co','.xethub.hf.co'}: fail('model host suffix allowlist is not pinned')
if not isinstance(redirect_hosts,list) or set(redirect_hosts)!=set(hosts): fail('model redirect exact host allowlist is not pinned')
if not isinstance(redirect_suffixes,list) or set(redirect_suffixes)!=set(suffixes): fail('model redirect host suffix allowlist is not pinned')
if model.get('required') is not True or model.get('offline_runtime') is not True or model.get('expected_file_count') != 9 or model.get('total_size_bytes') != 16024823729: fail('model download metadata is incomplete')
if model.get('revision_url') != 'https://huggingface.co/google/gemma-4-E4B-it/tree/ee0ef6023621cff504d758262d4e04895a5af4a2': fail('model revision URL is not canonical')
files=model.get('files',[])
canonical_model={
 '.gitattributes': (1570,'34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/.gitattributes?download=true'),
 'README.md': (27956,'b21e4f69614ccd77baa2f3797d05311040dee07b989cb9f0d25111aa4b605b2c','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/README.md?download=true'),
 'chat_template.jinja': (18569,'0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/chat_template.jinja?download=true'),
 'config.json': (5145,'33b10c02df3c2e8536cf323d29d53262aaa2f4d11dbe19bc729373fbe90295d4','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/config.json?download=true'),
 'generation_config.json': (208,'d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/generation_config.json?download=true'),
 'model.safetensors': (15992595884,'cfbd3d2f1cd71bd471c37fe2bf8546d5028d41e5736f64e1ca6c6b8893125503','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/model.safetensors?download=true'),
 'processor_config.json': (1689,'32bdf45d2ad4cc29a0822ddd157a182de76644f0419a6228d151495256e9813c','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/processor_config.json?download=true'),
 'tokenizer.json': (32169626,'cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/tokenizer.json?download=true'),
 'tokenizer_config.json': (3082,'9f4fec4b1dc6ecddf8f4a92e9caea5971c0e67d81309f3f9066a2bee8c362633','https://huggingface.co/google/gemma-4-E4B-it/resolve/ee0ef6023621cff504d758262d4e04895a5af4a2/tokenizer_config.json?download=true'),
}
if not isinstance(files,list) or len(files)!=len(canonical_model): fail('model file contract must contain exactly nine files')
seen=set()
for row in files:
    rel=row.get('path'); key=rel.casefold() if isinstance(rel,str) else ''
    if not isinstance(rel,str) or rel not in canonical_model or key in seen: fail(f'unsafe/duplicate/non-canonical model path: {rel!r}')
    size,sha,url=canonical_model[rel]
    if row.get('size_bytes') != size or str(row.get('sha256','')).lower() != sha or row.get('url') != url: fail(f'model metadata mismatch: {rel!r}')
    u=urllib.parse.urlparse(str(row.get('url','')))
    host=(u.hostname or '').casefold()
    try: ipaddress.ip_address(host); is_ip=True
    except ValueError: is_ip=False
    host_shape=bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host)) and '..' not in host and not host.endswith('.')
    host_ok=host in set(hosts) or any(host.endswith(s) and host != s[1:] for s in suffixes)
    if u.scheme!='https' or not host_shape or is_ip or not host_ok or u.username or u.password or u.port not in (None,443): fail(f'unsafe model URL: {rel!r}')
    seen.add(key)
if set(seen) != {x.casefold() for x in canonical_model}: fail('model file path set is not canonical')
allowed=set(m.get('allowed_image_repositories') or [])
required_allowed={'pgvector/pgvector','qdrant/qdrant','caddy','busybox','curlimages/curl','rocm/vllm','lmsysorg/sglang','ghcr.io/ggml-org/llama.cpp','node','python'}
if allowed != required_allowed: fail('allowed image repositories are not the canonical set')
repro=m.get('build_reproducibility') or {}; bases=repro.get('dockerfile_base_images') or {}; node_setup=repro.get('nodesource_setup') or {}
if bases.get('node')!='node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436' or bases.get('python')!='python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2': fail('Dockerfile base image reproducibility pins are missing')
if node_setup.get('url')!='https://deb.nodesource.com/setup_22.x' or node_setup.get('sha256')!='575583bbac2fccc0b5edd0dbc03e222d9f9dc8d724da996d22754d6411104fd1' or node_setup.get('https_required') is not True: fail('NodeSource setup HTTPS/hash contract is missing')
expected_apt_policy='HTTPS apt repositories only; package indexes are resolved inside the pinned Debian base and no mutable Docker FROM tags are accepted'
if repro.get('apt_policy') != expected_apt_policy:
    fail('APT HTTPS-only reproducibility policy is missing or changed')

build_command=target.get('build_command')
if not isinstance(build_command,str):
    fail('target build command is missing')
if legacy_overlay:
    # The approved 5a36866 handoff predates the --no-cache hardening and used
    # a literal commit-prefix tag.  Keep this exception exact: accepting a
    # command merely because it contains the right flags would turn any
    # self-asserted 5a tree into a trusted legacy release.
    expected_legacy_build_command = (
        f'docker build --platform linux/amd64 --pull=false --progress=plain '
        f'--secret id=nextauth_secret,src=/etc/{brand_slug}/secrets/nextauth_secret '
        f'--tag {brand_slug}/enterprise:handoff-<commit12> source'
    )
    if build_command != expected_legacy_build_command:
        fail('approved legacy target build command does not match the exact 5a36866 contract')
else:
    required_tokens = [
        '--platform linux/amd64',
        '--pull=false',
        '--no-cache',
        '--progress=plain',
        '--secret id=nextauth_secret',
    ]
    for required_token in required_tokens:
        if required_token not in build_command:
            fail(f'target build command is missing required token: {required_token}')
if '--network' in build_command:
    fail('target build command must not contain a network bypass')
expected_refs={'postgres':'pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b','qdrant':'qdrant/qdrant@sha256:94728574965d17c6485dd361aa3c0818b325b9016dac5ea6afec7b4b2700865f','caddy':'caddy@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d','busybox':'busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662','curl':'curlimages/curl@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b','gemma-vllm':'rocm/vllm@sha256:394194d36edcf9b36bcb563e143b21b80e64e7d04f33a447b448c0c0c00c04a8','sglang':'lmsysorg/sglang@sha256:16aba8925507e631e1dc1e23d95d026533602591775f6a8db68b74ee99746155','deepseek-llamacpp':'ghcr.io/ggml-org/llama.cpp@sha256:5a7d34c5a378b6f3b542e71690bd82db7b5bf31fd77d9d1582cc7f2c9043ad8c'}
expected_bases={'dockerfile-node':'node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436','dockerfile-python':'python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2'}
pins=m.get('image_pins'); seen=set()
if not isinstance(pins,list): fail('handoff image_pins is missing')
app_name = brand_slug
app_ref_full = f'{brand_slug}/enterprise:handoff-{commit}'
app_ref_short = f'{brand_slug}/enterprise:handoff-{commit[:12]}'
if legacy_overlay:
    app_refs = {app_ref_short}
elif validation_mode == 'strict':
    app_refs = {app_ref_full}
else:
    # Existing canonical releases may have been generated before the full-SHA
    # local tag change.  This compatibility allowance does not alter release
    # identity (the directory/current pointer is still the full SHA).
    app_refs = {app_ref_short, app_ref_full}
app_env = f'{brand_slug.upper()}_IMAGE_ID'
for pin in pins:
    if pin.get('name') in seen: fail('duplicate image pin name')
    seen.add(pin.get('name')); name=pin.get('name'); ref=pin.get('ref',''); kind=pin.get('kind','dependency')
    if kind=='local-build':
        if name!=app_name or pin.get('immutable_digest_required') is not False or pin.get('build_from_source') is not True or pin.get('local_image_id_required') is not True or pin.get('image_id_env') != app_env or ref not in app_refs: fail('application local image contract is invalid')
    elif kind=='build-base':
        if name not in expected_bases or pin.get('immutable_digest_required') is not True or ref != expected_bases[name]: fail(f'dockerfile build base pin mismatch: {name}')
    else:
        if name not in expected_refs or kind!='dependency' or pin.get('immutable_digest_required') is not True or ref != expected_refs[name]: fail(f'dependency image pin mismatch: {name}')
if seen != {app_name,*expected_refs,*expected_bases}: fail('image pin set is incomplete')
dockerfile=root/'source'/'Dockerfile'
if not dockerfile.is_file() or dockerfile.is_symlink(): fail('sanitized Dockerfile is missing/symlinked')
docker_text=dockerfile.read_text(encoding='utf-8')
if validation_mode == 'strict' and not re.search(r'^FROM\s+node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436\s+AS\s+enterprise-node-base\s*$', docker_text, re.MULTILINE|re.IGNORECASE):
    fail('Dockerfile is missing the pinned enterprise-node-base stage')
for image in expected_bases.values():
    repo,digest=image.split('@',1)
    if not re.search(rf'^FROM\s+{re.escape(repo)}@{re.escape(digest)}(?:\s+AS\s+[^\s]+)?\s*$',docker_text,re.MULTILINE|re.IGNORECASE): fail(f'Dockerfile FROM is not pinned to manifest: {image}')
if re.search(r'^FROM\s+(?:node|python)(?::|\s)',docker_text,re.MULTILINE|re.IGNORECASE): fail('Dockerfile contains a mutable node/python FROM tag')
required=m.get('required_files')
if not isinstance(required,list) or not required: fail('required_files is missing')
for rel in required:
    if not isinstance(rel,str) or '\x00' in rel or '\\' in rel or pathlib.PurePosixPath(rel).is_absolute() or any(x in ('','.','..') for x in rel.split('/')): fail(f'unsafe required file: {rel!r}')
    p=root/'source'/rel
    if not p.is_file() or p.is_symlink(): fail(f'required file is missing/symlinked: {rel}')
apt_helper_rel='docker/ensure-https-apt-sources.sh'
if apt_helper_rel not in required:
    fail('APT HTTPS enforcement helper is not required by the manifest')

apt_helper=root/'source'/apt_helper_rel
if not apt_helper.is_file() or apt_helper.is_symlink():
    fail('APT HTTPS enforcement helper is missing/symlinked')

helper_text=apt_helper.read_text(encoding='utf-8')
for marker in (
    'https://deb.debian.org',
    'https://security.debian.org',
    'HTTP APT repository is forbidden by the Enterprise HTTPS-only build contract',
):
    if marker not in helper_text:
        fail(f'APT HTTPS enforcement helper is missing contract marker: {marker}')

python_digest='4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2'
if validation_mode == 'strict' and not re.search(rf'^FROM\s+python@sha256:{python_digest}\s+AS\s+enterprise-python-base\s*$', docker_text, re.MULTILINE|re.IGNORECASE):
    fail('Dockerfile is missing the pinned enterprise-python-base stage')
if validation_mode == 'strict':
    python_stage_pattern=re.compile(r'(?ms)^FROM\s+enterprise-python-base\s+AS\s+(builder|runtime)\s*$.*?(?=^FROM\s|\Z)')
else:
    # Existing canonical releases and the approved historical overlay used
    # direct pinned Python FROM lines before the named-stage improvement.
    python_stage_pattern=re.compile(rf'(?ms)^FROM\s+python@sha256:{python_digest}(?:\s+AS\s+[^\n]+)?\s*$.*?(?=^FROM\s|\Z)')
python_stages=list(python_stage_pattern.finditer(docker_text))
if validation_mode == 'strict':
    if {match.group(1).lower() for match in python_stages} != {'builder','runtime'}:
        fail('Dockerfile must contain builder and runtime stages derived from the pinned Python base')
elif len(python_stages) != 2:
    alias_pattern=re.compile(r'(?ms)^FROM\s+enterprise-python-base\s+AS\s+(builder|runtime)\s*$.*?(?=^FROM\s|\Z)')
    python_stages=list(alias_pattern.finditer(docker_text))
    if {match.group(1).lower() for match in python_stages} != {'builder','runtime'}:
        fail('existing Dockerfile must contain two pinned Python stages')

helper_copy='COPY --chmod=0755 docker/ensure-https-apt-sources.sh /usr/local/sbin/ensure-https-apt-sources'
guard_run='RUN /usr/local/sbin/ensure-https-apt-sources'

for stage_match in python_stages:
    stage=stage_match.group(0)
    if helper_copy not in stage:
        fail('Python Docker stage does not copy the APT HTTPS enforcement helper')
    guard_index=stage.find(guard_run)
    apt_index=stage.find('apt-get update')
    if guard_index < 0 or apt_index < 0 or guard_index > apt_index:
        fail('Python Docker stage runs apt-get update before HTTPS enforcement')

runtime_stage=python_stages[1].group(0)
nodesource_index=runtime_stage.find('bash /tmp/nodesource_setup.sh')
nodejs_install_index=runtime_stage.find('apt-get install -y nodejs')
if nodesource_index < 0 or nodejs_install_index < 0:
    fail('runtime Docker stage is missing the pinned NodeSource/nodejs flow')

post_nodesource_guard=runtime_stage.find(
    '/usr/local/sbin/ensure-https-apt-sources',
    nodesource_index + len('bash /tmp/nodesource_setup.sh'),
)
if post_nodesource_guard < 0 or not (
    nodesource_index < post_nodesource_guard < nodejs_install_index
):
    fail('runtime Docker stage does not re-check HTTPS APT sources after NodeSource setup')
if (m.get('no_secrets') or {}).get('enforced') is not True: fail('no_secrets boundary is not enforced')
offline=m.get('offline_build')
if offline is not None:
    if not isinstance(offline,dict): fail('offline_build contract must be an object')
    offline_format=offline.get('format')
    if offline_format != f'{brand_slug}-enterprise-offline-inputs' or int(offline.get('version',0)) != 1:
        fail('offline_build format/version is unsupported')
    if offline.get('source_commit') != commit: fail('offline_build source_commit does not match handoff source_commit')
    mode=offline.get('mode')
    enabled=offline.get('enabled')
    if mode not in {'none','external-directory','embedded'} or not isinstance(enabled,bool):
        fail('offline_build mode/enabled contract is invalid')
    if enabled != (mode != 'none'):
        fail('offline_build enabled flag does not match mode')
    required_inputs=offline.get('required_inputs')
    if not isinstance(required_inputs,list) or set(required_inputs) != {'oci-images','apt-debs','apt-indexes','nodesource','python-wheels','npm-cache'}:
        fail('offline_build required_inputs are incomplete')
    if enabled:
        if offline.get('network') != 'none': fail('offline_build must disable network fallback')
        if not re.fullmatch(r'[0-9a-f]{64}', str(offline.get('source_tree_sha256') or '')): fail('offline_build source tree binding is missing')
        if not re.fullmatch(r'[0-9a-f]{64}', str(offline.get('npm_lock_sha256') or '')): fail('offline_build npm lock binding is missing')
        if not re.fullmatch(r'[0-9a-f]{64}', str(offline.get('pyproject_sha256') or '')): fail('offline_build pyproject binding is missing')
        archive=offline.get('companion_archive')
        if not isinstance(archive,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.zip',archive): fail('offline_build companion archive is invalid')
        for key in ('companion_sha256','manifest_sha256'):
            value=offline.get(key)
            if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value): fail(f'offline_build {key} is invalid')
    else:
        if mode != 'none' or offline.get('network') not in {'online-required-unless-companion','none'}:
            fail('disabled offline_build contract is invalid')
print(commit)
PY
}

verify_manifest() {
    # Public callers may validate only the current strict contract or an
    # already-activated canonical release with the narrow platform-compat
    # grandfathering. Legacy schema validation is intentionally unreachable
    # from this entry point.
    local root="$1" mode="${2:-strict}"
    case "$mode" in
        strict|existing-platform-compat) ;;
        *) die "verify_manifest mode must be strict or existing-platform-compat" ;;
    esac
    verify_manifest_internal "$root" "$mode"
}

# Keep the historical schema exception structurally separate from the public
# strict verifier.  Callers use this only after the exact approved suffix and
# source identity have been established.
verify_approved_legacy_manifest_5a() {
    local root="$1"
    verify_manifest_internal "$root" legacy-overlay
}

verify_handoff_tree() {
    local root="$1" p rel
    assert_no_reparse_tree "$root"
    for rel in README.enterprise.md bundle-manifest.json SHA256SUMS source; do [[ -e "$root/$rel" ]] || die "handoff root is missing $rel"; done
    [[ -d "$root/source" && ! -L "$root/source" ]] || die "handoff source is missing or symlinked"
    for p in "$root"/source/.env "$root"/source/.git "$root"/source/mobile "$root"/source/node_modules "$root"/source/venv; do [[ ! -e "$p" && ! -L "$p" ]] || die "handoff source contains forbidden path: $p"; done
    while IFS= read -r p; do
        rel="${p#"$root"/}"
        # Use an explicit if so an allowed path leaves the while body with a
        # success status.  `predicate && die` would return 1 for every safe
        # final entry and make command-substitution callers reject valid ZIPs.
        if is_forbidden_handoff_path "$rel"; then
            die "handoff source contains secret/data path: $rel"
        fi
        if [[ -f "$p" && "${rel,,}" =~ \.(gguf|ggml|safetensors|bin|pt|pth|ckpt|onnx)$ ]]; then
            die "handoff source contains a model artifact: $rel"
        fi
    done < <(find "$root" -mindepth 1 -print)
}

assert_managed_tree_ownership() {
    local root="$1" p owner mode_num
    [[ -d "$root" && ! -L "$root" ]] || die "managed release tree is missing or symlinked: $root"
    while IFS= read -r p; do
        [[ ! -L "$p" ]] || die "managed release tree contains a symlink: $p"
        owner="$(stat -c '%u' -- "$p" 2>/dev/null || true)"
        [[ "$owner" == 0 ]] || die "managed release tree entry must be root-owned: $p"
        mode_num=$((8#$(stat -c '%a' -- "$p" 2>/dev/null || printf 0)))
        (( (mode_num & 0022) == 0 )) || die "managed release tree entry is group/world writable: $p"
    done < <(find -P "$root" -print)
}

# Extract the generated Enterprise slug from the release's own manifest.  The
# tracked source uses the AoiTalk token, while generated handoffs use the
# target slug (normally enterpriseassistant); validation must follow the
# generated artifact rather than hard-coding either value here.
legacy_manifest_slug() {
    local root="$1"
    python3 - "$root/bundle-manifest.json" <<'PY'
import json, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding='utf-8')).get('format', '')
except Exception as exc:
    raise SystemExit(f'legacy overlay manifest is not valid JSON: {exc}')
match = re.fullmatch(r'([a-z][a-z0-9]*)-enterprise-handoff', str(value))
if match is None or match.group(1) == 'aoitalk':
    raise SystemExit('legacy overlay manifest does not contain generated Enterprise branding')
print(match.group(1))
PY
}

verify_generated_branding() {
    local root="$1" slug
    [[ -d "$root" && ! -L "$root" ]] || die "legacy overlay branding root is missing or symlinked: $root"
    slug="$(legacy_manifest_slug "$root")" || die "legacy overlay generated branding marker is invalid"
    python3 - "$root" "$slug" <<'PY'
import json, pathlib, re, sys
root = pathlib.Path(sys.argv[1]); slug = sys.argv[2]
manifest = json.loads((root / 'bundle-manifest.json').read_text(encoding='utf-8'))
if manifest.get('format') != f'{slug}-enterprise-handoff':
    raise SystemExit('legacy overlay manifest format does not match generated branding slug')
if not re.fullmatch(r'[a-z][a-z0-9]*', slug) or slug == 'aoitalk':
    raise SystemExit('legacy overlay generated branding slug is invalid')

# Assemble the private token so source-side branding replacement cannot rewrite
# this compatibility probe into the generated slug.
private_token = ('aoi' + 'talk').encode('ascii')
required = {
    root / 'source' / 'deploy' / 'enterprise' / 'update-on-server.sh': (
        f'/opt/{slug}', f'/etc/{slug}', f'/var/lib/{slug}', f'{slug}-enterprise'
    ),
    root / 'source' / 'deploy' / 'enterprise' / 'deploy-compose.sh': (
        f'/opt/{slug}', f'/etc/{slug}', f'/var/lib/{slug}', f'{slug}-enterprise'
    ),
}
for path, markers in required.items():
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f'legacy overlay generated runtime file is missing: {path.name}')
    payload = path.read_bytes()
    for marker in markers:
        if marker.encode('utf-8') not in payload:
            raise SystemExit(f'legacy overlay generated runtime file is not branded: {path.name}')

for path in root.rglob('*'):
    rel = path.relative_to(root).as_posix()
    if private_token in rel.encode('utf-8').lower():
        raise SystemExit(f'legacy overlay private brand leaked into path: {rel}')
    if path.is_symlink():
        raise SystemExit(f'legacy overlay branding tree contains a symlink: {rel}')
    if path.is_file() and private_token in path.read_bytes().lower():
        raise SystemExit(f'legacy overlay private brand leaked into content: {rel}')
PY
}


legacy_overlay_commit_for_name() {
    local name="${1##*/}"
    [[ "$name" == "${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" ]] || return 1
    printf '%s\n' "$LEGACY_APPROVED_COMMIT"
}

legacy_adoption_marker_path() {
    local install_root="$1"
    printf '%s/%s\n' "$install_root" "$LEGACY_ADOPTION_MARKER_NAME"
}

assert_legacy_adoption_marker() {
    local install_root="$1" marker canonical manifest_sha checksum_sha marker_mode
    marker="$(legacy_adoption_marker_path "$install_root")"
    [[ -f "$marker" && ! -L "$marker" ]] || return 1
    [[ "$(stat -c '%u' -- "$marker" 2>/dev/null || true)" == 0 ]] || return 1
    marker_mode=$((8#$(stat -c '%a' -- "$marker" 2>/dev/null || printf 0)))
    (( (marker_mode & 0077) == 0 )) || return 1
    canonical="$install_root/releases/$LEGACY_APPROVED_COMMIT"
    [[ -d "$canonical" && ! -L "$canonical" ]] || return 1
    manifest_sha="$(sha256sum "$canonical/bundle-manifest.json" 2>/dev/null | awk '{print $1}')"
    checksum_sha="$(sha256sum "$canonical/SHA256SUMS" 2>/dev/null | awk '{print $1}')"
    [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ && "$checksum_sha" =~ ^[0-9a-f]{64}$ ]] || return 1
    assert_legacy_adoption_marker_phase "$install_root" completed "$manifest_sha" "$checksum_sha"
}

assert_legacy_adoption_marker_phase() {
    local install_root="$1" expected_phase="$2" expected_manifest_sha="${3:-}" expected_checksum_sha="${4:-}"
    local marker
    marker="$(legacy_adoption_marker_path "$install_root")"
    [[ -f "$marker" && ! -L "$marker" ]] || return 1
    [[ "$(stat -c '%u' -- "$marker" 2>/dev/null || true)" == 0 ]] || return 1
    local marker_mode=$((8#$(stat -c '%a' -- "$marker" 2>/dev/null || printf 0)))
    (( (marker_mode & 0077) == 0 )) || return 1
    python3 - "$marker" "$expected_phase" "$LEGACY_ADOPTION_MARKER_FORMAT" "$LEGACY_APPROVED_COMMIT" "$LEGACY_OVERLAY_SUFFIX" "$APPROVED_LEGACY_SOURCE_GIT_TREE" "$expected_manifest_sha" "$expected_checksum_sha" <<'PY'
import json, pathlib, re, sys

path, expected_phase, marker_format, commit, suffix, source_tree, expected_manifest, expected_checksum = sys.argv[1:]
try:
    value = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
if value.get('format') != marker_format or value.get('version') != 1 or value.get('phase') != expected_phase:
    raise SystemExit(1)
if value.get('source_commit') != commit or value.get('source_git_tree') != source_tree:
    raise SystemExit(1)
if value.get('overlay_suffix') != suffix:
    raise SystemExit(1)
if value.get('legacy_release') != f'releases/{commit}-{suffix}' or value.get('canonical_release') != f'releases/{commit}':
    raise SystemExit(1)
for key in ('legacy_identity', 'legacy_manifest_sha256', 'legacy_checksum_sha256', 'current_raw'):
    if not isinstance(value.get(key), str) or not value[key]:
        raise SystemExit(1)
if not re.fullmatch(r'[^:]+:[0-9]+', value['legacy_identity']):
    raise SystemExit(1)
for key in ('legacy_manifest_sha256', 'legacy_checksum_sha256'):
    if not re.fullmatch(r'[0-9a-f]{64}', value[key]):
        raise SystemExit(1)
if expected_manifest and value['legacy_manifest_sha256'] != expected_manifest:
    raise SystemExit(1)
if expected_checksum and value['legacy_checksum_sha256'] != expected_checksum:
    raise SystemExit(1)
if expected_phase == 'prepared':
    raw = value['current_raw']
    allowed = {
        f'releases/{commit}-{suffix}',
        f'./releases/{commit}-{suffix}',
    }
    if raw not in allowed and not raw.endswith(f'/releases/{commit}-{suffix}'):
        raise SystemExit(1)
PY
}

legacy_adoption_marker_fields() {
    local install_root="$1" marker
    marker="$(legacy_adoption_marker_path "$install_root")"
    python3 - "$marker" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
print('\t'.join((
    str(value['legacy_identity']),
    str(value['current_raw']),
    str(value['legacy_manifest_sha256']),
    str(value['legacy_checksum_sha256']),
)))
PY
}

write_legacy_adoption_marker_prepared() {
    local install_root="$1" legacy="$2" current_raw="$3" legacy_identity="$4" manifest_sha="$5" checksum_sha="$6"
    local marker tmp
    marker="$(legacy_adoption_marker_path "$install_root")"
    [[ ! -e "$marker" && ! -L "$marker" ]] || die "legacy adoption marker already exists"
    [[ "$legacy_identity" =~ ^[^:]+:[0-9]+$ ]] || die "legacy release identity is invalid"
    [[ "$current_raw" == "releases/${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" || "$current_raw" == "./releases/${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" || "$current_raw" == */"releases/${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" ]] || die "legacy current symlink target is invalid"
    [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ && "$checksum_sha" =~ ^[0-9a-f]{64}$ ]] || die "legacy adoption fingerprint is invalid"
    tmp="$install_root/.legacy-adoption.$$.tmp"
    [[ ! -e "$tmp" && ! -L "$tmp" ]] || die "legacy adoption marker staging path already exists"
    umask 077
    python3 - "$tmp" "$LEGACY_ADOPTION_MARKER_FORMAT" "$LEGACY_APPROVED_COMMIT" "$LEGACY_OVERLAY_SUFFIX" "$APPROVED_LEGACY_SOURCE_GIT_TREE" "$legacy_identity" "$manifest_sha" "$checksum_sha" "$current_raw" <<'PY'
import json, os, pathlib, sys

path, marker_format, commit, suffix, source_tree, identity, manifest_sha, checksum_sha, current_raw = sys.argv[1:]
value = {
    'format': marker_format,
    'version': 1,
    'phase': 'prepared',
    'source_commit': commit,
    'source_git_tree': source_tree,
    'overlay_suffix': suffix,
    'legacy_release': f'releases/{commit}-{suffix}',
    'canonical_release': f'releases/{commit}',
    'legacy_identity': identity,
    'legacy_manifest_sha256': manifest_sha,
    'legacy_checksum_sha256': checksum_sha,
    'current_raw': current_raw,
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    data = (json.dumps(value, separators=(',', ':')) + '\n').encode('utf-8')
    os.write(fd, data)
    os.fsync(fd)
finally:
    os.close(fd)
PY
    chown root:root -- "$tmp"
    chmod 0600 -- "$tmp"
    move_noreplace "$tmp" "$marker" || { rm -f -- "$tmp"; die "could not atomically create prepared legacy adoption marker"; }
    assert_legacy_adoption_marker_phase "$install_root" prepared "$manifest_sha" "$checksum_sha" || die "prepared legacy adoption marker verification failed"
}

complete_legacy_adoption_marker() {
    local install_root="$1" canonical marker tmp manifest_sha checksum_sha
    canonical="$install_root/releases/$LEGACY_APPROVED_COMMIT"
    marker="$(legacy_adoption_marker_path "$install_root")"
    assert_legacy_adoption_marker_phase "$install_root" prepared || die "legacy adoption marker is not in prepared phase"
    [[ -d "$canonical" && ! -L "$canonical" ]] || die "normalized legacy release is missing while completing adoption marker"
    manifest_sha="$(sha256sum "$canonical/bundle-manifest.json" | awk '{print $1}')"
    checksum_sha="$(sha256sum "$canonical/SHA256SUMS" | awk '{print $1}')"
    [[ "$manifest_sha" =~ ^[0-9a-f]{64}$ && "$checksum_sha" =~ ^[0-9a-f]{64}$ ]] || die "could not hash normalized legacy release metadata"
    assert_legacy_adoption_marker_phase "$install_root" prepared "$manifest_sha" "$checksum_sha" || die "prepared legacy adoption marker fingerprint changed"
    tmp="$install_root/.legacy-adoption.$$.completed.tmp"
    [[ ! -e "$tmp" && ! -L "$tmp" ]] || die "legacy completed marker staging path already exists"
    umask 077
    python3 - "$marker" "$tmp" "$manifest_sha" "$checksum_sha" <<'PY'
import json, os, pathlib, sys
marker, destination, manifest_sha, checksum_sha = sys.argv[1:]
value = json.loads(pathlib.Path(marker).read_text(encoding='utf-8'))
if value.get('phase') != 'prepared':
    raise SystemExit('legacy adoption marker is not prepared')
value['phase'] = 'completed'
value['legacy_manifest_sha256'] = manifest_sha
value['legacy_checksum_sha256'] = checksum_sha
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(fd, (json.dumps(value, separators=(',', ':')) + '\n').encode('utf-8'))
    os.fsync(fd)
finally:
    os.close(fd)
PY
    chown root:root -- "$tmp"
    chmod 0600 -- "$tmp"
    python3 - "$tmp" "$marker" <<'PY'
import os, pathlib, sys
source, destination = map(pathlib.Path, sys.argv[1:])
if destination.is_symlink() or not destination.is_file():
    raise SystemExit('legacy adoption marker changed before completion')
os.replace(source, destination)
fd = os.open(destination.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
    assert_legacy_adoption_marker "$install_root" || die "completed legacy adoption marker verification failed"
}

# Retain the historical function name for callers/tests; it now means a
# prepared marker is promoted only after current has already been switched to
# the canonical release.
write_legacy_adoption_marker() {
    complete_legacy_adoption_marker "$@"
}

assert_legacy_overlay_installation() {
    local install_root="$1" release="$2" releases="$1/releases" base commit manifest_commit resolved release_mode
    [[ -d "$release" && ! -L "$release" ]] || die "legacy Enterprise overlay is missing or symlinked"
    base="$(basename -- "$release")"
    commit="$(legacy_overlay_commit_for_name "$base" || true)"
    [[ "$commit" == "$LEGACY_APPROVED_COMMIT" ]] || die "current release is not the approved historical Revision10 overlay"
    [[ "$release" == "$releases/$base" ]] || die "legacy Enterprise overlay is not directly under releases"
    resolved="$(readlink -f -- "$release" 2>/dev/null || true)"
    [[ "$resolved" == "$release" ]] || die "legacy Enterprise overlay realpath escapes releases"
    [[ "$(stat -c '%u' -- "$release" 2>/dev/null || true)" == 0 ]] || die "legacy Enterprise overlay must be root-owned"
    release_mode=$((8#$(stat -c '%a' -- "$release" 2>/dev/null || printf 0)))
    (( (release_mode & 0022) == 0 )) || die "legacy Enterprise overlay is group/world writable"
    assert_secure_ancestors "$release"
    assert_managed_tree_ownership "$release"
    verify_handoff_tree "$release" || die "legacy Enterprise overlay failed handoff tree validation"
    manifest_commit="$(verify_approved_legacy_manifest_5a "$release")" || die "legacy Enterprise overlay failed strict historical manifest validation"
    [[ "$manifest_commit" == "$commit" ]] || die "legacy Enterprise overlay source identity does not match its release name"
    verify_checksum_file "$release" >/dev/null || die "legacy Enterprise overlay failed checksum validation"
    verify_generated_branding "$release" || die "legacy Enterprise overlay generated branding validation failed"
    LEGACY_RELEASE_PATH="$release"
    LEGACY_RELEASE_COMMIT="$commit"
}

verify_existing_release_contract() {
    local install_root="$1" release="$2" commit="${3:-$(basename -- "$release")}" manifest_commit
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "existing release identity must be a full lowercase 40-character SHA"
    verify_handoff_tree "$release" || die "existing release failed handoff tree validation"
    if [[ "$commit" == "$LEGACY_APPROVED_COMMIT" ]] && assert_legacy_adoption_marker "$install_root"; then
        manifest_commit="$(verify_approved_legacy_manifest_5a "$release")" || die "existing adopted legacy release failed manifest validation"
        verify_generated_branding "$release" || die "existing adopted legacy release branding validation failed"
    else
        manifest_commit="$(verify_manifest "$release" existing-platform-compat)" || die "existing release failed manifest validation"
    fi
    [[ "$manifest_commit" == "$commit" ]] || die "existing release manifest commit does not match release identity"
    verify_checksum_file "$release" >/dev/null || die "existing release failed checksum validation"
}

# Classify the current topology without validating a legacy payload.  An exact
# suffix is only an allowlisted candidate shape; manifest/checksum/branding
# proof is deferred until after the incoming handoff passes strict validation.
classify_existing_current_for_update() {
    local install_root="$1" current releases resolved commit release release_mode releases_mode raw raw_base canonical marker phase
    MANAGED_INSTALLATION_ERROR_HINT="initial construction requires --fresh-install"
    LEGACY_RELEASE_PATH=""
    LEGACY_RELEASE_COMMIT=""
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    [[ -d "$install_root" ]] || die "no existing AoiTalk Enterprise installation"
    current="$install_root/current"
    releases="$install_root/releases"
    marker="$(legacy_adoption_marker_path "$install_root")"
    [[ -L "$current" ]] || die "no existing AoiTalk Enterprise installation"
    [[ -d "$releases" && ! -L "$releases" ]] || die "existing Enterprise installation is missing releases directory"
    [[ "$(stat -c '%u' -- "$releases" 2>/dev/null || true)" == 0 ]] || die "existing releases directory must be root-owned"
    releases_mode=$((8#$(stat -c '%a' -- "$releases" 2>/dev/null || printf 0)))
    (( (releases_mode & 0022) == 0 )) || die "existing releases directory is group/world writable"
    raw="$(readlink -- "$current" 2>/dev/null || true)"
    resolved="$(readlink -f -- "$current" 2>/dev/null || true)"
    # A crash after rename(legacy, canonical) leaves the old suffix symlink
    # dangling.  Recognize only that exact journal-recoverable shape before the
    # generic current-link safety check (which quite correctly rejects any
    # other dangling link).
    if [[ -z "$resolved" ]]; then
        raw_base="${raw##*/}"
        canonical="$releases/$LEGACY_APPROVED_COMMIT"
        [[ "$raw_base" == "${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" && ( "$raw" == "$releases/$raw_base" || "$raw" == "$install_root/releases/$raw_base" || "$raw" == "releases/$raw_base" ) ]] || die "existing current pointer cannot be resolved"
        [[ -d "$canonical" && ! -L "$canonical" ]] || die "existing current pointer cannot be resolved"
        [[ -f "$marker" && ! -L "$marker" ]] || die "existing current pointer cannot be resolved without a prepared legacy adoption marker"
        assert_legacy_adoption_marker_phase "$install_root" prepared || die "existing current pointer has an invalid prepared legacy adoption marker"
        [[ "$(stat -c '%u' -- "$canonical" 2>/dev/null || true)" == 0 ]] || die "recovery canonical release must be root-owned"
        assert_secure_ancestors "$canonical"
        LEGACY_RELEASE_PATH="$canonical"
        LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
        printf 'approved-legacy-recovery\n'
        return 0
    fi
    # This checks link topology, ownership and permissions only.
    assert_current_link_safe "$install_root"
    [[ -n "$resolved" && "$(dirname -- "$resolved")" == "$releases" ]] || die "existing current pointer must resolve directly under releases"
    commit="$(basename -- "$resolved")"
    release="$releases/$commit"
    [[ "$resolved" == "$release" && -d "$release" && ! -L "$release" ]] || die "existing current release is not a direct managed release"
    [[ "$(stat -c '%u' -- "$release" 2>/dev/null || true)" == 0 ]] || die "existing current release must be root-owned"
    release_mode=$((8#$(stat -c '%a' -- "$release" 2>/dev/null || printf 0)))
    (( (release_mode & 0022) == 0 )) || die "existing current release is group/world writable"
    assert_secure_ancestors "$release"
    if [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
        if [[ "$commit" == "$LEGACY_APPROVED_COMMIT" && -e "$marker" ]]; then
            phase="$(python3 - "$marker" <<'PY'
import json, pathlib, sys
try:
    print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')).get('phase', ''))
except Exception:
    raise SystemExit(1)
PY
            )" || die "legacy adoption marker is invalid"
            case "$phase" in
                prepared)
                    assert_legacy_adoption_marker_phase "$install_root" prepared || die "canonical legacy recovery marker is invalid"
                    LEGACY_RELEASE_PATH="$release"
                    LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
                    printf 'approved-legacy-recovery\n'
                    return 0
                    ;;
                completed) ;;
                *) die "legacy adoption marker has an unknown phase" ;;
            esac
        fi
        printf 'canonical\n'
    elif [[ "$commit" == "${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" ]]; then
        canonical="$releases/$LEGACY_APPROVED_COMMIT"
        if [[ -e "$marker" || -L "$marker" ]]; then
            [[ ! -L "$marker" ]] || die "legacy adoption marker is symlinked"
            phase="$(python3 - "$marker" <<'PY'
import json, pathlib, sys
try:
    print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')).get('phase', ''))
except Exception:
    raise SystemExit(1)
PY
            )" || die "legacy adoption marker is invalid"
            case "$phase" in
                prepared) assert_legacy_adoption_marker_phase "$install_root" prepared || die "prepared legacy adoption marker is invalid" ;;
                completed) die "completed legacy adoption marker conflicts with a suffixed current pointer" ;;
                *) die "legacy adoption marker has an unknown phase" ;;
            esac
        fi
        if [[ ! -e "$release" && -d "$canonical" && ! -L "$canonical" ]]; then
            # The directory rename completed but current/marker commit did not;
            # strict incoming validation will be followed by recovery of the
            # already-proven canonical tree.
            [[ -f "$marker" && ! -L "$marker" ]] || die "interrupted legacy normalization is missing its prepared marker"
            assert_legacy_adoption_marker_phase "$install_root" prepared || die "interrupted legacy normalization marker is invalid"
            LEGACY_RELEASE_PATH="$canonical"
            LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
            printf 'approved-legacy-recovery\n'
            return 0
        fi
        LEGACY_RELEASE_PATH="$release"
        LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
        printf 'approved-legacy\n'
    else
        die "existing current pointer is neither canonical nor the single approved legacy release"
    fi
}

# Validate the release currently selected by ``current``.  Merely having an
# install directory (or a symlink somewhere below releases/) is not evidence
# of a managed Enterprise installation: the selected release must be the
# direct 40-character commit directory and pass the same tree/manifest/
# checksum checks used during activation and rollback.
assert_existing_managed_installation() {
    local install_root="$1" current releases resolved commit release release_mode releases_mode manifest_commit
    MANAGED_INSTALLATION_ERROR_HINT="initial construction requires --fresh-install"
    LEGACY_RELEASE_PATH=""
    LEGACY_RELEASE_COMMIT=""
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    [[ -d "$install_root" ]] || die "no existing AoiTalk Enterprise installation"
    current="$install_root/current"
    releases="$install_root/releases"
    [[ -L "$current" ]] || die "no existing AoiTalk Enterprise installation"
    assert_current_link_safe "$install_root"
    [[ -d "$releases" && ! -L "$releases" ]] || die "existing AoiTalk Enterprise installation is missing releases directory"
    releases_mode=$((8#$(stat -c '%a' -- "$releases" 2>/dev/null || printf 0)))
    [[ "$(stat -c '%u' -- "$releases" 2>/dev/null || true)" == 0 ]] || die "existing releases directory must be root-owned"
    (( (releases_mode & 0022) == 0 )) || die "existing releases directory is group/world writable"
    resolved="$(readlink -f -- "$current" 2>/dev/null || true)"
    [[ -n "$resolved" ]] || die "existing current pointer cannot be resolved"
    [[ "$(dirname -- "$resolved")" == "$releases" ]] || die "existing current pointer must resolve directly under releases/<commit>"
    commit="$(basename -- "$resolved")"
    if [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
        release="$releases/$commit"
        [[ "$resolved" == "$release" && -d "$release" && ! -L "$release" ]] || die "existing current release is not a direct managed release"
        [[ "$(stat -c '%u' -- "$release" 2>/dev/null || true)" == 0 ]] || die "existing current release must be root-owned"
        release_mode=$((8#$(stat -c '%a' -- "$release" 2>/dev/null || printf 0)))
        (( (release_mode & 0022) == 0 )) || die "existing current release is group/world writable"
        assert_secure_ancestors "$release"
        # A canonical 5a release may be the result of the one-time legacy
        # normalization.  Its completion marker is required before the
        # historical manifest validator is considered.
        if [[ "$commit" == "$LEGACY_APPROVED_COMMIT" ]] && assert_legacy_adoption_marker "$install_root"; then
            assert_managed_tree_ownership "$release"
            verify_handoff_tree "$release" || die "existing legacy release failed handoff tree validation"
            manifest_commit="$(verify_approved_legacy_manifest_5a "$release")" || die "existing legacy release failed manifest validation"
        else
            verify_handoff_tree "$release" || die "existing current release failed handoff tree validation"
            manifest_commit="$(verify_manifest "$release" existing-platform-compat)" || die "existing current release failed manifest validation"
        fi
        [[ "$manifest_commit" == "$commit" ]] || die "existing current release manifest commit does not match current pointer"
        verify_checksum_file "$release" >/dev/null || die "existing current release failed checksum validation"
        if [[ "$commit" == "$LEGACY_APPROVED_COMMIT" ]] && assert_legacy_adoption_marker "$install_root"; then
            verify_generated_branding "$release" || die "existing legacy release generated branding validation failed"
        fi
    else
        if [[ "$commit" == "${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" && -d "$releases/$LEGACY_APPROVED_COMMIT" && ! -L "$releases/$LEGACY_APPROVED_COMMIT" ]] && assert_legacy_adoption_marker "$install_root"; then
            # Recovery state after a durable marker but before current symlink
            # replacement. The next normal apply will finish the swap.
            assert_managed_tree_ownership "$releases/$LEGACY_APPROVED_COMMIT"
            verify_handoff_tree "$releases/$LEGACY_APPROVED_COMMIT" || die "interrupted legacy normalization tree is invalid"
            verify_approved_legacy_manifest_5a "$releases/$LEGACY_APPROVED_COMMIT" >/dev/null || die "interrupted legacy normalization manifest is invalid"
            verify_checksum_file "$releases/$LEGACY_APPROVED_COMMIT" >/dev/null || die "interrupted legacy normalization checksums are invalid"
            verify_generated_branding "$releases/$LEGACY_APPROVED_COMMIT" || die "interrupted legacy normalization branding is invalid"
        else
            assert_legacy_overlay_installation "$install_root" "$resolved"
        fi
    fi
    MANAGED_INSTALLATION_ERROR_HINT=""
}

validate_managed_root_for_reset() {
    local root="$1" label="$2" owner mode_num
    is_abs_safe "$root" || die "fresh cleanup $label root must be a narrow absolute path: $root"
    [[ ! -L "$root" ]] || die "fresh cleanup $label root must not be a symlink: $root"
    [[ "$root" != "/" ]] || die "fresh cleanup refuses to remove filesystem root"
    if [[ -e "$root" ]]; then
        [[ -d "$root" ]] || die "fresh cleanup $label root is not a directory: $root"
        assert_secure_ancestors "$root"
        owner="$(stat -c '%u' -- "$root" 2>/dev/null || true)"
        mode_num=$((8#$(stat -c '%a' -- "$root" 2>/dev/null || printf 0)))
        [[ "$owner" == 0 ]] || die "fresh cleanup $label root must be root-owned: $root"
        (( (mode_num & 0022) == 0 )) || die "fresh cleanup $label root is group/world writable: $root"
    fi
}

docker_resource_ids() {
    local docker_bin="$1" kind="$2" output id
    case "$kind" in
        containers) output="$("$docker_bin" ps -aq --filter "label=com.docker.compose.project=$CANONICAL_COMPOSE_PROJECT")" || die "Docker daemon query failed while listing Enterprise containers" ;;
        networks) output="$("$docker_bin" network ls -q --filter "label=com.docker.compose.project=$CANONICAL_COMPOSE_PROJECT")" || die "Docker daemon query failed while listing Enterprise networks" ;;
        volumes) output="$("$docker_bin" volume ls -q --filter "label=com.docker.compose.project=$CANONICAL_COMPOSE_PROJECT")" || die "Docker daemon query failed while listing Enterprise volumes" ;;
        *) die "unsupported Docker resource kind: $kind" ;;
    esac
    while IFS= read -r id; do
        [[ -z "$id" ]] && continue
        [[ "$id" =~ ^[[:alnum:]][[:alnum:]_.-]*$ ]] || die "Docker returned an unsafe Enterprise resource identifier"
        printf '%s\n' "$id"
    done <<< "$output"
}

cleanup_canonical_compose_resources() {
    local docker_bin="${1:-docker}" kind id remaining ids
    if [[ "$docker_bin" == */* ]]; then
        [[ -x "$docker_bin" ]] || die "Docker CLI is unavailable; refusing to delete Enterprise state"
    else
        command -v "$docker_bin" >/dev/null 2>&1 || die "Docker CLI is unavailable; refusing to delete Enterprise state"
    fi
    "$docker_bin" info >/dev/null 2>&1 || die "Docker daemon is unavailable; refusing to delete Enterprise state"
    ids="$(docker_resource_ids "$docker_bin" containers)"
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        "$docker_bin" rm -f "$id" >/dev/null || die "could not stop/remove canonical Enterprise container"
    done <<< "$ids"
    ids="$(docker_resource_ids "$docker_bin" networks)"
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        "$docker_bin" network rm "$id" >/dev/null || die "could not remove canonical Enterprise network"
    done <<< "$ids"
    ids="$(docker_resource_ids "$docker_bin" volumes)"
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        "$docker_bin" volume rm "$id" >/dev/null || die "could not remove canonical Enterprise volume"
    done <<< "$ids"
    remaining="$(docker_resource_ids "$docker_bin" containers)"
    [[ -z "$remaining" ]] || die "canonical Enterprise containers remain after cleanup"
    remaining="$(docker_resource_ids "$docker_bin" networks)"
    [[ -z "$remaining" ]] || die "canonical Enterprise networks remain after cleanup"
    remaining="$(docker_resource_ids "$docker_bin" volumes)"
    [[ -z "$remaining" ]] || die "canonical Enterprise volumes remain after cleanup"
}

clear_install_state_preserving_lock() {
    local install_root="$1" preserve_path="${2:-}" child name lock_file="$1/.operation.lock" nested nested_name
    [[ -d "$install_root" && ! -L "$install_root" ]] || die "fresh cleanup install root is missing or symlinked: $install_root"
    [[ -f "$lock_file" && ! -L "$lock_file" ]] || die "fresh cleanup requires the operation lock to remain present"
    if [[ -n "$preserve_path" ]]; then
        [[ "$preserve_path" == "$install_root/.handoff-tmp/"* && -d "$preserve_path" && ! -L "$preserve_path" ]] || die "fresh cleanup staging path is outside managed handoff scratch: $preserve_path"
    fi
    while IFS= read -r -d '' child; do
        name="$(basename -- "$child")"
        [[ "$name" == ".operation.lock" ]] && continue
        case "$name" in
            .handoff-tmp)
                if [[ -n "$preserve_path" && "$preserve_path" == "$child/"* ]]; then
                    while IFS= read -r -d '' nested; do
                        [[ "$nested" == "$preserve_path" ]] && continue
                        nested_name="$(basename -- "$nested")"
                        [[ "$nested_name" == "." || "$nested_name" == ".." ]] && continue
                        rm -rf -- "$nested" || die "could not clear stale handoff scratch: $nested"
                    done < <(find -P "$child" -mindepth 1 -maxdepth 1 -print0)
                    continue
                fi
                ;;
            current|releases|active-release|state|.corrupt-*|.collision-*|.current.*.tmp|.enterprise-legacy-adoption-v1.json|.legacy-adoption.*.tmp|.offline-inputs-required|.offline-inputs-required.*.tmp) ;;
            *) continue ;;
        esac
        # rm never follows a symlink supplied as the top-level operand; this
        # removes a managed link itself while preserving any external target.
        rm -rf -- "$child" || die "could not clear managed install state: $child"
    done < <(find -P "$install_root" -mindepth 1 -maxdepth 1 -print0)
    [[ -f "$lock_file" && ! -L "$lock_file" ]] || die "fresh cleanup removed the operation lock"
}

remove_managed_root() {
    local root="$1" label="$2"
    [[ -e "$root" ]] || return 0
    [[ -d "$root" && ! -L "$root" ]] || die "fresh cleanup $label root became unsafe: $root"
    # The root itself is canonical/explicit; rm -rf removes symlink children
    # as links and never follows them to an external target.
    rm -rf -- "$root" || die "could not remove canonical Enterprise $label state"
}

# Destructive helper kept parameterized for isolated tests.  The public CLI
# calls it only with the fixed production roots below; the optional fourth
# argument is a test-injected Docker executable, not a production path/env
# bypass.
fresh_reset_managed_state() {
    local install_root="$1" config_root="$2" data_root="$3" docker_bin="${4:-docker}" preserve_path="${5:-}"
    require_root_state
    validate_managed_root_for_reset "$install_root" "install"
    validate_managed_root_for_reset "$config_root" "config"
    validate_managed_root_for_reset "$data_root" "data"
    assert_operation_lock_held "$install_root"
    cleanup_canonical_compose_resources "$docker_bin"
    clear_install_state_preserving_lock "$install_root" "$preserve_path"
    remove_managed_root "$config_root" "config"
    remove_managed_root "$data_root" "data"
    log "Fresh Enterprise state reset complete; canonical Compose resources and managed roots were removed."
}

fresh_reset_production_state() {
    local install_root="$1" preserve_path="${2:-}"
    [[ "$install_root" == "$CANONICAL_INSTALL_ROOT" ]] || die "--fresh-install requires canonical install root $CANONICAL_INSTALL_ROOT"
    fresh_reset_managed_state "$CANONICAL_INSTALL_ROOT" "$CANONICAL_CONFIG_ROOT" "$CANONICAL_DATA_ROOT" docker "$preserve_path"
}

stage_handoff() {
    local input="$1" temp_root="$2" stage="$2/stage" identity_before identity_after digest_before digest_after
    mkdir -p "$stage"
    if [[ -f "$input" ]]; then
        # This function is called through command substitution by
        # apply_handoff.  Bash disables errexit for commands inside that
        # context, so every safety boundary must be an explicit fail-closed
        # check; never continue to extraction after central-directory
        # validation has rejected an entry.
        assert_no_symlink_ancestors "$input"
        identity_before="$(handoff_archive_identity "$input")"
        [[ -n "$identity_before" ]] || die "could not identify handoff ZIP"
        digest_before="$(handoff_archive_digest "$input")"
        [[ "$digest_before" =~ ^[0-9a-f]{64}$ ]] || die "could not hash handoff ZIP"
        verify_zip_entries "$input" || die "handoff ZIP central-directory validation failed"
        identity_after="$(handoff_archive_identity "$input")"
        [[ "$identity_after" == "$identity_before" ]] || die "handoff ZIP changed during central-directory verification"
        safe_extract_zip "$input" "$stage" || die "handoff ZIP safe extraction failed"
        identity_after="$(handoff_archive_identity "$input")"
        [[ "$identity_after" == "$identity_before" ]] || die "handoff ZIP changed during extraction"
        digest_after="$(handoff_archive_digest "$input")"
        [[ "$digest_after" == "$digest_before" ]] || die "handoff ZIP bytes changed during extraction"
    else
        die "handoff input must be the canonical handoff ZIP (directory input is disabled): $input"
    fi
    verify_handoff_tree "$stage" || die "extracted handoff tree failed reparse/secret validation"
    verify_manifest "$stage" >/dev/null || die "extracted handoff manifest failed strict contract validation"
    verify_checksum_file "$stage" || die "extracted handoff checksum verification failed"
    printf '%s\n' "$stage"
}

normalize_release_modes() {
    local release="$1" p rel
    [[ -d "$release" && ! -L "$release" ]] || die "release mode normalization root is missing or symlinked: $release"
    # ZIP mode bits are deliberately ignored.  Handoff source contains no
    # secrets, so canonical release directories/files are traversable/readable
    # by the non-root operator after activation while remaining root-owned.
    while IFS= read -r p; do
        [[ ! -L "$p" ]] || die "release mode normalization found a symlink: $p"
        chmod 0755 -- "$p" || die "could not set release directory mode: $p"
    done < <(find -P "$release" -type d -print)
    while IFS= read -r p; do
        [[ ! -L "$p" ]] || die "release mode normalization found a symlink: $p"
        chmod 0644 -- "$p" || die "could not set release file mode: $p"
    done < <(find -P "$release" -type f -print)
    # Only these fixed, source-controlled runtime shells may be executable;
    # never derive chmod targets from ZIP entries or manifest input.
    for rel in \
        deploy/enterprise/deploy-compose.sh \
        deploy/enterprise/update-on-server.sh \
        deploy/enterprise/verify-gemma-vllm-gfx1151.sh \
        docker/entrypoint.enterprise.sh \
        setup.sh \
        run.sh; do
        p="$release/source/$rel"
        [[ -f "$p" && ! -L "$p" ]] || die "canonical runtime shell is missing/symlinked: $rel"
        chmod 0755 -- "$p" || die "could not set canonical runtime shell mode: $rel"
    done
    chmod 0755 -- "$release" || die "could not set release root mode"
}

atomic_activate() {
    local stage="$1" install_root="$2" commit="$3" release_dir current_link tmp_link corrupt_release
    local previous_current_target="" previous_current_exists=0 moved_new=0 moved_old=0 stage_identity release_identity
    release_dir="$install_root/releases/$commit"
    current_link="$install_root/current"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    ensure_secure_directory "$install_root/releases" 0755
    assert_current_link_safe "$install_root"
    if [[ -L "$current_link" ]]; then
        previous_current_target="$(readlink -- "$current_link")"
        previous_current_exists=1
    fi
    # Normalize the fully verified stage before touching the active release.
    # This makes mode failures harmless: the previous release/current pointer
    # remain in place and no partially activated tree is exposed.
    normalize_release_modes "$stage"
    if [[ -L "$release_dir" ]]; then die "existing release must not be a symlink: $release_dir"; fi
    if [[ -e "$release_dir" ]]; then
        [[ -d "$release_dir" && ! -L "$release_dir" ]] || die "existing release is not a directory: $release_dir"
        [[ "$(stat -c '%u' -- "$release_dir" 2>/dev/null || true)" == 0 ]] || die "existing release must be root-owned: $release_dir"
        local release_mode=$((8#$(stat -c '%a' -- "$release_dir" 2>/dev/null || printf 0)))
        (( (release_mode & 0022) == 0 )) || die "existing release is group/world writable: $release_dir"
        # Same-commit reuse is not trusted merely because the manifest names
        # the same commit. Re-run every reparse/tree/manifest/checksum check;
        # a corrupt release is quarantined only after the new stage is proven.
        if ! (verify_handoff_tree "$release_dir" && verify_manifest "$release_dir" existing-platform-compat >/dev/null && verify_checksum_file "$release_dir" >/dev/null); then
            assert_no_reparse_tree "$release_dir"
            corrupt_release="$install_root/.corrupt-${commit}-$$-${RANDOM}"
            [[ ! -e "$corrupt_release" && ! -L "$corrupt_release" ]] || die "release quarantine path already exists"
            mv -T -- "$release_dir" "$corrupt_release"
            if ! move_noreplace "$stage" "$release_dir"; then
                # A destination that appeared after quarantine must not make
                # the previous active release disappear.  Move the racing
                # path aside (without following symlinks), restore the old
                # release to its original identity, then reject the update.
                local collision_release="$install_root/.collision-${commit}-$$-${RANDOM}"
                if [[ -e "$release_dir" || -L "$release_dir" ]]; then
                    [[ ! -e "$collision_release" && ! -L "$collision_release" ]] || die "release collision quarantine path already exists"
                    mv -T -- "$release_dir" "$collision_release" || die "release destination race and previous release could not be restored"
                fi
                mv -T -- "$corrupt_release" "$release_dir" || die "release replacement failed and previous release could not be restored"
                if [[ -e "$collision_release" || -L "$collision_release" ]]; then
                    if [[ -L "$collision_release" ]]; then
                        rm -f -- "$collision_release"
                    else
                        assert_no_reparse_tree "$collision_release"
                        chmod -R u+w -- "$collision_release"
                        rm -rf -- "$collision_release"
                    fi
                fi
                die "release replacement failed; previous release restored"
            fi
            moved_old=1
            moved_new=1
            # Previous activated trees are intentionally read-only.  The
            # quarantine is already reparse-free and has passed the failed
            # validation branch, so restore owner write permission only while
            # removing that private quarantine; the new release remains a-w.
            # Do not remove the quarantine until the new current pointer has
            # been installed; rollback below can then restore both trees.
        else
            rm -rf -- "$stage"
        fi
    else
        # The destination was absent during the initial check.  Use `mv -T`
        # (never the default nested-directory behavior), then compare the
        # renamed directory's device/inode with the staged identity.  A
        # concurrent destination creation/replacement therefore fails closed
        # instead of placing the handoff under release_dir/stage.
        stage_identity="$(stat -c '%d:%i' -- "$stage" 2>/dev/null || true)"
        [[ -n "$stage_identity" ]] || die "could not identify handoff staging directory"
        [[ ! -e "$release_dir" && ! -L "$release_dir" ]] || die "release destination appeared during activation: $release_dir"
        if ! move_noreplace "$stage" "$release_dir"; then
            die "release activation destination changed; staged handoff was not activated"
        fi
        [[ -d "$release_dir" && ! -L "$release_dir" ]] || die "activated release is missing or symlinked: $release_dir"
        release_identity="$(stat -c '%d:%i' -- "$release_dir" 2>/dev/null || true)"
        [[ "$release_identity" == "$stage_identity" ]] || die "release identity changed during activation: $release_dir"
        moved_new=1
    fi
    restore_activation() {
        local restore_tmp="$install_root/.current.restore.$$.tmp"
        rm -f -- "$restore_tmp" "$current_link" || true
        if (( moved_new )); then
            if [[ -d "$release_dir" && ! -L "$release_dir" ]]; then
                chmod -R u+w -- "$release_dir" || true
                rm -rf -- "$release_dir" || true
            fi
        fi
        if (( moved_old )) && [[ -d "$corrupt_release" && ! -L "$corrupt_release" ]]; then
            mv -T -- "$corrupt_release" "$release_dir" || true
        fi
        if (( previous_current_exists )); then
            ln -s -- "$previous_current_target" "$restore_tmp" 2>/dev/null || true
            if [[ -L "$restore_tmp" ]]; then
                mv -Tf -- "$restore_tmp" "$current_link" || true
            fi
        fi
        rm -f -- "$restore_tmp" || true
    }
    tmp_link="$install_root/.current.$$.tmp"
    rm -f -- "$tmp_link"
    if ! ln -s -- "$release_dir" "$tmp_link"; then
        restore_activation
        die "could not stage current pointer; previous release restored"
    fi
    # Re-check the destination immediately before replacement.  Only an
    # absent pointer or an existing symlink is replaceable; a regular file
    # appearing after the initial ancestor check is a fail-closed race.
    [[ ! -e "$current_link" || -L "$current_link" ]] || {
        rm -f -- "$tmp_link"
        restore_activation
        die "current pointer changed to a non-symlink; previous release restored"
    }
    if ! mv -Tf -- "$tmp_link" "$current_link"; then
        rm -f -- "$tmp_link"
        restore_activation
        die "could not activate current pointer; previous release restored"
    fi
    if (( moved_old )); then
        chmod -R u+w -- "$corrupt_release" || {
            # Activation is already complete; retain the quarantine rather
            # than risking deletion of a previous release on cleanup failure.
            die "new release activated but old quarantine cleanup could not be prepared: $corrupt_release"
        }
        rm -rf -- "$corrupt_release" || die "new release activated but old quarantine cleanup failed: $corrupt_release"
    fi
    log "Activated Enterprise handoff source_commit=$commit at $current_link"
}

normalize_legacy_overlay_release() {
    local install_root="$1" releases legacy canonical current marker current_raw current_target marker_phase marker_fields
    local legacy_identity_before legacy_identity_after canonical_identity marker_identity marker_raw marker_manifest marker_checksum
    local manifest_sha checksum_sha tmp_link was_current_legacy=0 was_current_canonical=0
    releases="$install_root/releases"
    legacy="$releases/${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}"
    canonical="$releases/$LEGACY_APPROVED_COMMIT"
    current="$install_root/current"
    marker="$(legacy_adoption_marker_path "$install_root")"
    [[ -L "$current" ]] || die "legacy normalization requires a current symlink"
    current_raw="$(readlink -- "$current" 2>/dev/null || true)"
    current_target="$(readlink -f -- "$current" 2>/dev/null || true)"
    if [[ -z "$current_target" && "${current_raw##*/}" == "${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}" ]]; then
        current_target="$legacy"
    fi
    [[ "$current_target" == "$legacy" || "$current_target" == "$canonical" ]] || die "current pointer changed before legacy normalization"
    [[ "$current_target" == "$legacy" ]] && was_current_legacy=1
    [[ "$current_target" == "$canonical" ]] && was_current_canonical=1

    # A completed marker is terminal.  It is valid only with a canonical
    # current pointer; a suffixed current after completion is never a recovery
    # state and must not be silently re-adopted.
    if [[ -e "$marker" || -L "$marker" ]]; then
        [[ ! -L "$marker" ]] || die "legacy adoption marker is symlinked"
        marker_fields="$(legacy_adoption_marker_fields "$install_root" 2>/dev/null || true)"
        [[ -n "$marker_fields" ]] || die "legacy adoption marker is invalid"
        marker_phase="$(python3 - "$marker" <<'PY'
import json, pathlib, sys
try:
    print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')).get('phase', ''))
except Exception:
    raise SystemExit(1)
PY
        )" || die "legacy adoption marker is invalid"
        case "$marker_phase" in
            completed)
                assert_legacy_adoption_marker "$install_root" || die "completed legacy adoption marker is invalid"
                (( was_current_canonical )) || die "completed legacy adoption marker conflicts with a non-canonical current pointer"
                LEGACY_RELEASE_PATH=""
                LEGACY_RELEASE_COMMIT=""
                return 0
                ;;
            prepared)
                assert_legacy_adoption_marker_phase "$install_root" prepared || die "prepared legacy adoption marker is invalid"
                IFS=$'\t' read -r marker_identity marker_raw marker_manifest marker_checksum <<< "$marker_fields"
                [[ "$current_raw" == "$marker_raw" || ( "$was_current_canonical" == 1 && "$current_raw" == "$canonical" ) ]] || die "current pointer does not match the prepared legacy adoption marker"
                if [[ -e "$legacy" || -L "$legacy" ]]; then
                    [[ "$was_current_legacy" == 1 ]] || die "prepared legacy adoption marker conflicts with current topology"
                    [[ ! -e "$canonical" && ! -L "$canonical" ]] || die "legacy and canonical releases both exist during adoption recovery"
                    legacy_identity_before="$(stat -c '%d:%i' -- "$legacy" 2>/dev/null || true)"
                    [[ "$legacy_identity_before" == "$marker_identity" ]] || die "legacy release inode changed before adoption recovery"
                    assert_legacy_overlay_installation "$install_root" "$legacy"
                    legacy_identity_after="$(stat -c '%d:%i' -- "$legacy" 2>/dev/null || true)"
                    [[ "$legacy_identity_after" == "$marker_identity" ]] || die "legacy release inode changed during adoption recovery"
                    [[ "$(readlink -- "$current" 2>/dev/null || true)" == "$marker_raw" ]] || die "current pointer changed during adoption recovery"
                    move_noreplace "$legacy" "$canonical" || die "could not atomically resume historical overlay normalization"
                    canonical_identity="$(stat -c '%d:%i' -- "$canonical" 2>/dev/null || true)"
                    [[ "$canonical_identity" == "$marker_identity" ]] || die "canonical release inode does not match the prepared legacy identity"
                else
                    [[ -d "$canonical" && ! -L "$canonical" ]] || die "prepared legacy adoption marker has no recoverable release tree"
                    canonical_identity="$(stat -c '%d:%i' -- "$canonical" 2>/dev/null || true)"
                    [[ "$canonical_identity" == "$marker_identity" ]] || die "canonical release inode does not match the prepared legacy identity"
                    [[ "$was_current_legacy" == 1 || "$was_current_canonical" == 1 ]] || die "prepared legacy adoption marker has an invalid current topology"
                fi
                ;;
            *)
                die "legacy adoption marker has an unknown phase"
                ;;
        esac
    else
        # First attempt: only the exact suffix-selected release may enter the
        # proof and identity transition, and the canonical destination must be
        # absent.  No merge/copy/overwrite is permitted.
        (( was_current_legacy )) || die "legacy normalization requires the approved suffix release to be current"
        [[ -d "$legacy" && ! -L "$legacy" ]] || die "approved legacy overlay is missing or symlinked"
        [[ ! -e "$canonical" && ! -L "$canonical" ]] || die "canonical legacy release destination already exists; refusing collision"
        legacy_identity_before="$(stat -c '%d:%i' -- "$legacy" 2>/dev/null || true)"
        [[ -n "$legacy_identity_before" ]] || die "could not identify the approved legacy release"
        assert_legacy_overlay_installation "$install_root" "$legacy"
        legacy_identity_after="$(stat -c '%d:%i' -- "$legacy" 2>/dev/null || true)"
        [[ "$legacy_identity_after" == "$legacy_identity_before" ]] || die "approved legacy release changed during validation"
        [[ "$(readlink -- "$current" 2>/dev/null || true)" == "$current_raw" ]] || die "current pointer changed during validation"
        manifest_sha="$(sha256sum "$legacy/bundle-manifest.json" | awk '{print $1}')"
        checksum_sha="$(sha256sum "$legacy/SHA256SUMS" | awk '{print $1}')"
        write_legacy_adoption_marker_prepared "$install_root" "$legacy" "$current_raw" "$legacy_identity_before" "$manifest_sha" "$checksum_sha"
        marker_fields="$(legacy_adoption_marker_fields "$install_root")"
        IFS=$'\t' read -r marker_identity marker_raw marker_manifest marker_checksum <<< "$marker_fields"
        legacy_identity_after="$(stat -c '%d:%i' -- "$legacy" 2>/dev/null || true)"
        [[ "$legacy_identity_after" == "$marker_identity" ]] || die "legacy release inode changed before normalization"
        [[ "$(readlink -- "$current" 2>/dev/null || true)" == "$marker_raw" ]] || die "current pointer changed before normalization"
        move_noreplace "$legacy" "$canonical" || die "could not atomically normalize historical overlay identity"
        canonical_identity="$(stat -c '%d:%i' -- "$canonical" 2>/dev/null || true)"
        [[ "$canonical_identity" == "$marker_identity" ]] || die "canonical release inode does not match prepared legacy identity"
    fi

    # Re-validate the canonical tree and its content-addressed fingerprint
    # before exposing it through current.  This is deliberately repeated after
    # rename so recovery cannot turn a marker into a trust bypass.
    assert_managed_tree_ownership "$canonical"
    verify_handoff_tree "$canonical" || die "normalized legacy release failed tree validation"
    verify_approved_legacy_manifest_5a "$canonical" >/dev/null || die "normalized legacy release failed manifest validation"
    verify_checksum_file "$canonical" >/dev/null || die "normalized legacy release failed checksum validation"
    verify_generated_branding "$canonical" || die "normalized legacy release branding validation failed"
    manifest_sha="$(sha256sum "$canonical/bundle-manifest.json" | awk '{print $1}')"
    checksum_sha="$(sha256sum "$canonical/SHA256SUMS" | awk '{print $1}')"
    [[ "$manifest_sha" == "$marker_manifest" && "$checksum_sha" == "$marker_checksum" ]] || die "normalized legacy release fingerprint does not match the prepared marker"
    canonical_identity="$(stat -c '%d:%i' -- "$canonical" 2>/dev/null || true)"
    [[ "$canonical_identity" == "$marker_identity" ]] || die "normalized legacy release inode does not match the prepared marker"

    if (( was_current_legacy )); then
        [[ "$(readlink -- "$current" 2>/dev/null || true)" == "$marker_raw" ]] || die "current pointer changed before canonical activation"
        tmp_link="$install_root/.current.$$-legacy.tmp"
        [[ ! -e "$tmp_link" && ! -L "$tmp_link" ]] || die "legacy current pointer staging path already exists"
        ln -s -- "$canonical" "$tmp_link" || die "could not stage canonical legacy current pointer"
        [[ "$(readlink -- "$current" 2>/dev/null || true)" == "$marker_raw" ]] || { rm -f -- "$tmp_link"; die "current pointer changed during legacy normalization"; }
        mv -Tf -- "$tmp_link" "$current" || { rm -f -- "$tmp_link"; die "could not activate canonical legacy current pointer"; }
        [[ "$(readlink -f -- "$current" 2>/dev/null || true)" == "$canonical" ]] || die "canonical legacy current pointer verification failed"
    elif (( ! was_current_canonical )); then
        die "legacy normalization reached an unknown current state"
    fi

    # The completed marker is written last.  A crash before this point leaves a
    # prepared marker and a precisely recoverable topology; a completed marker
    # can never coexist with a suffix current.
    complete_legacy_adoption_marker "$install_root"
    LEGACY_RELEASE_PATH=""
    LEGACY_RELEASE_COMMIT=""
    log "Normalized historical overlay identity to releases/$LEGACY_APPROVED_COMMIT"
}

# Stable semantic name used by the update transaction and contract tests.  The
# implementation remains the single normalization routine above so there is no
# second, weaker adoption path.
adopt_approved_legacy_release() {
    normalize_legacy_overlay_release "$@"
}

verify_legacy_authorization() {
    local stage="$1" commit="$2"
    [[ -f "$stage/bundle-manifest.json" && ! -L "$stage/bundle-manifest.json" ]] || die "incoming handoff manifest is missing"
    python3 - "$stage/bundle-manifest.json" "$commit" "$LEGACY_APPROVED_COMMIT" "$LEGACY_OVERLAY_SUFFIX" <<'PY'
import json, pathlib, re, sys
path, commit, approved, suffix = sys.argv[1:]
manifest = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
compat = manifest.get('compatibility')
legacy = compat.get('legacy_adoption') if isinstance(compat, dict) else None
if not isinstance(legacy, dict):
    raise SystemExit('incoming handoff does not authorize legacy adoption')
if legacy.get('policy_id') != 'aoitalk-managed-adoption-v1':
    raise SystemExit('incoming handoff legacy adoption policy is not canonical')
if legacy.get('allowed_source_commit') != approved or legacy.get('allowed_source_git_tree') != '85f30781f3ca603c883c1a3562b35c2adb8c0027' or legacy.get('allowed_suffix') != suffix:
    raise SystemExit('incoming handoff legacy adoption candidate is not approved')
if legacy.get('require_exact_manifest') is not True or legacy.get('require_exact_checksum_coverage') is not True or legacy.get('require_generated_enterprise_brand') is not True:
    raise SystemExit('incoming handoff legacy adoption proof requirements are incomplete')
if legacy.get('foreign_policy') != 'reject':
    raise SystemExit('incoming handoff legacy foreign policy is not reject')
if not re.fullmatch(r'[0-9a-f]{40}', commit):
    raise SystemExit('incoming handoff source commit is invalid')
PY
}

handoff_offline_enabled() {
    local manifest="$1"
    python3 - "$manifest" <<'PY'
import json, pathlib, sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
offline = value.get("offline_build")
raise SystemExit(0 if isinstance(offline, dict) and offline.get("enabled") is True else 1)
PY
}

write_offline_requirement_marker() {
    local install_root="$1" commit="$2" marker marker_tmp
    marker="$install_root/.offline-inputs-required"
    marker_tmp="$install_root/.offline-inputs-required.$$.tmp"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "offline input requirement source commit is invalid"
    [[ ! -e "$marker_tmp" && ! -L "$marker_tmp" ]] || die "offline input requirement marker staging path already exists"
    printf '%s\n' "$commit" > "$marker_tmp"
    chown root:root -- "$marker_tmp"
    chmod 0600 -- "$marker_tmp"
    mv -Tf -- "$marker_tmp" "$marker" || { rm -f -- "$marker_tmp"; die "could not persist offline input requirement marker"; }
}

clear_offline_requirement_marker() {
    local install_root="$1" marker="$1/.offline-inputs-required"
    [[ ! -L "$marker" ]] || die "offline input requirement marker is unsafe"
    [[ ! -e "$marker" ]] && return 0
    [[ -f "$marker" ]] || die "offline input requirement marker is not a regular file"
    [[ "$(stat -c '%u' -- "$marker" 2>/dev/null || true)" == 0 ]] || die "offline input requirement marker must be root-owned"
    rm -f -- "$marker" || die "could not clear offline input requirement marker"
}

install_verified_offline_inputs() {
    local archive="$1" stage="$2" install_root="$3" commit="$4" validator destination existing
    [[ -n "$archive" ]] || return 0
    is_abs_safe "$archive" || die "offline companion archive must be an absolute path"
    [[ -f "$archive" && ! -L "$archive" ]] || die "offline companion archive is missing or symlinked: $archive"
    assert_no_symlink_ancestors "$archive"
    validator="$stage/source/deploy/enterprise/offline_inputs.py"
    [[ -f "$validator" && ! -L "$validator" ]] || die "offline input validator is missing from the strict handoff"
    destination="$install_root/.handoff-tmp/offline-inputs-$commit"
    [[ ! -e "$destination" && ! -L "$destination" ]] || die "offline input staging destination already exists"
    python3 "$validator" hydrate --archive "$archive" --destination "$destination" || die "offline companion hydration failed"
    python3 "$validator" verify --root "$destination" --handoff "$stage/bundle-manifest.json" --archive "$archive" || die "offline companion binding verification failed"
    ensure_secure_directory "$install_root/offline-inputs" 0700
    existing="$install_root/offline-inputs/$commit"
    if [[ -e "$existing" || -L "$existing" ]]; then
        [[ -d "$existing" && ! -L "$existing" ]] || die "existing offline input destination is unsafe: $existing"
        # Never replace a previously verified companion for the same source
        # commit.  A caller may reuse it only when the archive and manifest
        # prove identical contents.
        cmp -s -- "$destination/offline-build-manifest.json" "$existing/offline-build-manifest.json" || die "offline input destination already contains a different source-bound companion"
        chmod -R u+w -- "$destination" || true
        rm -rf -- "$destination"
    else
        move_noreplace "$destination" "$existing" || die "offline input activation raced or failed"
    fi
    chmod 0700 -- "$existing"
    log "Verified offline Enterprise inputs activated for source_commit=$commit"
}

apply_handoff() {
    local input="$1" install_root="$2" backend="${3:-external}" fresh_install="${4:-0}" temp_root stage commit existing_kind input_identity_before input_identity_after input_digest_before input_digest_after
    is_abs_safe "$input" || die "handoff input must be an absolute path"
    [[ "${input,,}" == *.zip && -f "$input" && ! -L "$input" ]] || die "handoff input must be the canonical ZIP (directory input is disabled)"
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    [[ "$fresh_install" == 0 || "$fresh_install" == 1 ]] || die "internal fresh-install mode is invalid"
    assert_no_symlink_ancestors "$input"
    input_identity_before="$(stat -c '%d:%i:%s:%Y' -- "$input" 2>/dev/null || true)"
    [[ -n "$input_identity_before" ]] || die "could not identify handoff ZIP"
    input_digest_before="$(handoff_archive_digest "$input")"
    [[ "$input_digest_before" =~ ^[0-9a-f]{64}$ ]] || die "could not hash handoff ZIP"
    # Central-directory checks deliberately happen before the managed-current
    # check.  A hostile ZIP must never be hidden behind a missing-current
    # error, while no-current default apply still avoids extraction.
    verify_zip_entries "$input" || die "handoff ZIP central-directory validation failed"
    existing_kind="fresh"
    if (( fresh_install )); then
        [[ "$install_root" == "$CANONICAL_INSTALL_ROOT" ]] || die "--fresh-install requires canonical install root $CANONICAL_INSTALL_ROOT"
    else
        existing_kind="$(classify_existing_current_for_update "$install_root")"
        if [[ "$existing_kind" == "approved-legacy" ]]; then
            LEGACY_RELEASE_PATH="$install_root/releases/${LEGACY_APPROVED_COMMIT}-${LEGACY_OVERLAY_SUFFIX}"
            LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
        elif [[ "$existing_kind" == "approved-legacy-recovery" ]]; then
            LEGACY_RELEASE_PATH="$install_root/releases/$LEGACY_APPROVED_COMMIT"
            LEGACY_RELEASE_COMMIT="$LEGACY_APPROVED_COMMIT"
        fi
        if [[ "$existing_kind" == "canonical" ]]; then
            assert_existing_managed_installation "$install_root"
        fi
    fi
    # Keep staging on the install-root filesystem so renameat2 can activate
    # it atomically.  Fresh cleanup receives this exact path and removes all
    # other managed install contents without deleting the verified handoff.
    ensure_secure_directory "$install_root/.handoff-tmp" 0700
    temp_root="$(mktemp -d "$install_root/.handoff-tmp/apply.XXXXXX")"
    chown root:root -- "$temp_root"
    chmod 0700 -- "$temp_root"
    trap 'rm -rf -- "$temp_root"' RETURN
    stage="$(stage_handoff "$input" "$temp_root")"
    input_identity_after="$(stat -c '%d:%i:%s:%Y' -- "$input" 2>/dev/null || true)"
    [[ "$input_identity_after" == "$input_identity_before" ]] || die "handoff ZIP changed during verification/extraction"
    input_digest_after="$(handoff_archive_digest "$input")"
    [[ "$input_digest_after" == "$input_digest_before" ]] || die "handoff ZIP bytes changed during verification/extraction"
    commit="$(verify_manifest "$stage")"
    [[ "$backend" =~ ^(external|gemma-vllm|deepseek-llamacpp|sglang-cuda)$ ]] || die "unsupported backend: $backend"
    if handoff_offline_enabled "$stage/bundle-manifest.json"; then
        [[ -n "$APPLY_OFFLINE_INPUTS" ]] || die "incoming handoff requires its source-bound offline companion ZIP"
    elif [[ -n "$APPLY_OFFLINE_INPUTS" ]]; then
        die "offline companion ZIP cannot be applied to a handoff whose offline contract is disabled"
    fi
    # Reject direct activation before even legacy normalization or companion
    # installation can mutate managed state without the durable checkpoint.
    if (( ! fresh_install )); then
        local durable_lock_fd="${OPERATION_LOCK_FD:-${AOIT_INTERNAL_OPERATION_LOCK_FD:-}}"
        [[ "$durable_lock_fd" =~ ^[0-9]+$ ]] || die "durable activation requires the inherited operation lock descriptor"
        python3 -B "$stage/source/deploy/enterprise/durable_state.py" assert-ready \
            --candidate "$commit" --install-root "$install_root" --lock-fd "$durable_lock_fd" || \
            die "DURABLE_CHECKPOINT_REQUIRED: use the transactional handoff bootstrap/update interface"
    fi
    if (( ! fresh_install )) && [[ "$existing_kind" == "approved-legacy" || "$existing_kind" == "approved-legacy-recovery" ]]; then
        # The candidate path was classified before extraction, but the legacy
        # payload is not trusted until the incoming handoff is strict-valid.
        if [[ "$existing_kind" == "approved-legacy" ]]; then
            assert_legacy_overlay_installation "$install_root" "$LEGACY_RELEASE_PATH"
        else
            assert_managed_tree_ownership "$LEGACY_RELEASE_PATH"
            verify_handoff_tree "$LEGACY_RELEASE_PATH" || die "interrupted legacy normalization tree is invalid"
            verify_approved_legacy_manifest_5a "$LEGACY_RELEASE_PATH" >/dev/null || die "interrupted legacy normalization manifest is invalid"
            verify_checksum_file "$LEGACY_RELEASE_PATH" >/dev/null || die "interrupted legacy normalization checksums are invalid"
            verify_generated_branding "$LEGACY_RELEASE_PATH" || die "interrupted legacy normalization branding is invalid"
        fi
        verify_legacy_authorization "$stage" "$commit"
        # Normalize the legacy identity only after the incoming handoff is
        # fully strict-validated, but before activation can expose a new
        # current pointer.  A collision therefore leaves the old state intact.
        adopt_approved_legacy_release "$install_root"
    fi
    if [[ -n "$APPLY_OFFLINE_INPUTS" ]]; then
        install_verified_offline_inputs "$APPLY_OFFLINE_INPUTS" "$stage" "$install_root" "$commit"
    fi
    if (( fresh_install )); then
        fresh_reset_production_state "$install_root" "$temp_root"
    fi
    atomic_activate "$stage" "$install_root" "$commit"
    if [[ -n "$APPLY_OFFLINE_INPUTS" ]]; then
        write_offline_requirement_marker "$install_root" "$commit"
    else
        clear_offline_requirement_marker "$install_root"
    fi
    trap - RETURN
    rm -rf -- "$temp_root"
    log "Handoff apply complete. Run current/source/deploy/enterprise/deploy-compose.sh up $backend after target model/digest checks."
}

rollback_handoff() {
    die "source-pointer-only rollback is disabled; use durable_state.py restore --transaction <id> or recover"
}

status_handoff() {
    local install_root="$1" current="${1:-}/current"
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    if [[ -L "$install_root/current" ]]; then
        local resolved manifest_commit commit
        resolved="$(readlink -f "$install_root/current" 2>/dev/null || true)"
        [[ -n "$resolved" && -d "$resolved" && ! -L "$resolved" ]] || die "current pointer cannot be resolved to a release directory"
        commit="$(basename -- "$resolved")"
        [[ "$(dirname -- "$resolved")" == "$install_root/releases" ]] || die "current pointer must resolve directly under releases/<commit>"
        if [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
            assert_secure_ancestors "$resolved"
            assert_managed_tree_ownership "$resolved"
            verify_handoff_tree "$resolved" || die "current release failed handoff tree validation"
            if [[ "$commit" == "$LEGACY_APPROVED_COMMIT" ]] && assert_legacy_adoption_marker "$install_root"; then
                manifest_commit="$(verify_approved_legacy_manifest_5a "$resolved")" || die "current legacy release failed manifest validation"
                verify_generated_branding "$resolved" || die "current legacy release generated branding validation failed"
            else
                manifest_commit="$(verify_manifest "$resolved" existing-platform-compat)" || die "current release failed manifest validation"
            fi
            [[ "$manifest_commit" == "$commit" ]] || die "current release manifest commit does not match current pointer"
            verify_checksum_file "$resolved" >/dev/null || die "current release failed checksum validation"
            printf 'current=%s\n' "$resolved"
        else
            assert_legacy_overlay_installation "$install_root" "$resolved"
            printf 'current=%s\nlegacy_overlay=valid source_commit=%s suffix=%s\n' "$resolved" "$LEGACY_RELEASE_COMMIT" "$LEGACY_OVERLAY_SUFFIX"
        fi
    else
        printf 'current=none\n'
    fi
}

usage() {
    cat <<'EOF'
Usage: update-on-server.sh <command> ...

  apply [--fresh-install] <handoff.zip> <install-root> [backend]
  apply [--fresh-install] [--offline-inputs <companion.zip>] <handoff.zip> <install-root> [backend]
  rollback <install-root> <source-commit>
  status <install-root>

default apply = update an existing managed Enterprise installation
--fresh-install = destructive initial construction (only immediately after apply)
EOF
}

parse_apply_args() {
    local -a args=("$@")
    APPLY_FRESH_INSTALL=0
    APPLY_OFFLINE_INPUTS=""
    if (( ${#args[@]} > 0 )) && [[ "${args[0]}" == "--fresh-install" ]]; then
        APPLY_FRESH_INSTALL=1
        args=("${args[@]:1}")
    elif (( ${#args[@]} > 0 )) && [[ "${args[0]}" == --* && "${args[0]}" != --offline-inputs ]]; then
        die "unknown apply option: ${args[0]}"
    fi
    if (( ${#args[@]} > 1 )) && [[ "${args[0]}" == "--offline-inputs" ]]; then
        [[ ${#args[@]} -ge 2 ]] || die "--offline-inputs requires a companion ZIP path"
        APPLY_OFFLINE_INPUTS="${args[1]}"
        args=("${args[@]:2}")
    elif (( ${#args[@]} > 0 )) && [[ "${args[0]}" == --* ]]; then
        die "unknown apply option: ${args[0]}"
    fi
    (( ${#args[@]} >= 2 && ${#args[@]} <= 3 )) || die "apply requires [--fresh-install] <handoff.zip> <install-root> [backend]"
    [[ "${args[0]}" != -* && "${args[1]}" != -* ]] || die "apply options are allowed only immediately after apply"
    if (( ${#args[@]} == 3 )); then
        [[ "${args[2]}" != -* ]] || die "apply options are allowed only immediately after apply"
        APPLY_BACKEND="${args[2]}"
    else
        APPLY_BACKEND="external"
    fi
    APPLY_INPUT="${args[0]}"
    APPLY_INSTALL_ROOT="${args[1]}"
}

main() {
    local command="${1:-}"
    case "$command" in
        apply)
            shift
            parse_apply_args "$@"
            acquire_operation_lock "$APPLY_INSTALL_ROOT"
            trap 'release_operation_lock' EXIT
            apply_handoff "$APPLY_INPUT" "$APPLY_INSTALL_ROOT" "$APPLY_BACKEND" "$APPLY_FRESH_INSTALL"
            ;;
        rollback)
            [[ $# == 3 ]] || die "rollback requires <install-root> <source-commit>"
            [[ "$2" != -* && "$3" != -* ]] || die "rollback does not accept options"
            acquire_operation_lock "$2"
            trap 'release_operation_lock' EXIT
            rollback_handoff "$2" "$3"
            ;;
        status)
            [[ $# == 2 ]] || die "status requires <install-root>"
            [[ "$2" != -* ]] || die "status does not accept options"
            status_handoff "$2"
            ;;
        -h|--help|help) usage ;;
        *) usage >&2; exit 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
