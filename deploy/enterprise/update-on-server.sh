#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical Enterprise handoff updater.  It accepts only the checksum-covered
# aoitalk-enterprise-handoff ZIP emitted by
# scripts/build_enterprise_handoff.ps1.  model weights and secrets never travel
# through this path; model download is a separate HF_TOKEN operation described
# by README.enterprise.md.

log() { printf '[AoiTalk Enterprise] %s\n' "$*"; }
die() { printf '[AoiTalk Enterprise] ERROR: %s\n' "$*" >&2; exit 1; }

OPERATION_LOCK_FD=""

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
    assert_current_link_safe "$install_root"
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

verify_manifest() {
    local root="$1"
    [[ -f "$root/bundle-manifest.json" && ! -L "$root/bundle-manifest.json" ]] || die "bundle-manifest.json is missing or symlinked"
    python3 - "$root/bundle-manifest.json" "$root" <<'PY'
import ipaddress, json, pathlib, re, sys, urllib.parse
manifest_path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); m=json.loads(manifest_path.read_text(encoding='utf-8'))
def fail(msg): raise SystemExit(msg)
if m.get('format') != 'aoitalk-enterprise-handoff' or int(m.get('version',0)) != 1: fail('unsupported Enterprise handoff format/version')
commit=m.get('source_commit','')
if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit): fail('manifest source_commit is invalid')
if m.get('source_dirty') is not False: fail('handoff source_dirty must be false')
target=m.get('target_build') or {}
if target.get('os') != 'linux' or target.get('architecture') != 'linux/amd64' or target.get('source_directory') != 'source' or target.get('build_required_on_target') is not True: fail('target_build contract is not linux/amd64 sanitized-source')
source_tree=m.get('source_tree') or {}
if source_tree.get('kind')!='git-archive' or source_tree.get('path')!='source' or source_tree.get('commit')!=commit or source_tree.get('dirty') is not False or source_tree.get('sanitized') is not True or source_tree.get('import_closure_verified') is not True or source_tree.get('build_context_safe') is not True: fail('source tree HEAD/sanitization evidence is missing')
backend=m.get('backend') or {}
if backend.get('default') not in {'external','gemma-vllm','deepseek-llamacpp','sglang-cuda'}: fail('backend default is invalid')
if not set(backend.get('supported') or {}) >= {'external','gemma-vllm','deepseek-llamacpp','sglang-cuda'}: fail('backend supported set is incomplete')
transport=m.get('transport') or {}
if transport.get('default') not in {'https','http-redirect'} or not set(transport.get('supported') or {}) >= {'https','http-redirect'}: fail('transport contract is invalid')
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
expected_refs={'postgres':'pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b','qdrant':'qdrant/qdrant@sha256:94728574965d17c6485dd361aa3c0818b325b9016dac5ea6afec7b4b2700865f','caddy':'caddy@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d','busybox':'busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662','curl':'curlimages/curl@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b','gemma-vllm':'rocm/vllm@sha256:394194d36edcf9b36bcb563e143b21b80e64e7d04f33a447b448c0c0c00c04a8','sglang':'lmsysorg/sglang@sha256:16aba8925507e631e1dc1e23d95d026533602591775f6a8db68b74ee99746155','deepseek-llamacpp':'ghcr.io/ggml-org/llama.cpp@sha256:5a7d34c5a378b6f3b542e71690bd82db7b5bf31fd77d9d1582cc7f2c9043ad8c'}
expected_bases={'dockerfile-node':'node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436','dockerfile-python':'python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2'}
pins=m.get('image_pins'); seen=set()
if not isinstance(pins,list): fail('handoff image_pins is missing')
for pin in pins:
    if pin.get('name') in seen: fail('duplicate image pin name')
    seen.add(pin.get('name')); name=pin.get('name'); ref=pin.get('ref',''); kind=pin.get('kind','dependency')
    if kind=='local-build':
        if name!='aoitalk' or pin.get('immutable_digest_required') is not False or pin.get('build_from_source') is not True or pin.get('local_image_id_required') is not True or pin.get('image_id_env') != 'AOITALK_IMAGE_ID' or not re.fullmatch(r'aoitalk/enterprise:handoff-'+commit[:12],ref): fail('application local image contract is invalid')
    elif kind=='build-base':
        if name not in expected_bases or pin.get('immutable_digest_required') is not True or ref != expected_bases[name]: fail(f'dockerfile build base pin mismatch: {name}')
    else:
        if name not in expected_refs or kind!='dependency' or pin.get('immutable_digest_required') is not True or ref != expected_refs[name]: fail(f'dependency image pin mismatch: {name}')
if seen != {'aoitalk',*expected_refs,*expected_bases}: fail('image pin set is incomplete')
dockerfile=root/'source'/'Dockerfile'
if not dockerfile.is_file() or dockerfile.is_symlink(): fail('sanitized Dockerfile is missing/symlinked')
docker_text=dockerfile.read_text(encoding='utf-8')
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
if (m.get('no_secrets') or {}).get('enforced') is not True: fail('no_secrets boundary is not enforced')
print(commit)
PY
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
    done < <(find "$root/source" -type f -print)
}

stage_handoff() {
    local input="$1" temp_root="$2" stage="$2/stage"
    mkdir -p "$stage"
    if [[ -f "$input" ]]; then
        # This function is called through command substitution by
        # apply_handoff.  Bash disables errexit for commands inside that
        # context, so every safety boundary must be an explicit fail-closed
        # check; never continue to extraction after central-directory
        # validation has rejected an entry.
        verify_zip_entries "$input" || die "handoff ZIP central-directory validation failed"
        safe_extract_zip "$input" "$stage" || die "handoff ZIP safe extraction failed"
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
        if ! (verify_handoff_tree "$release_dir" && verify_manifest "$release_dir" >/dev/null && verify_checksum_file "$release_dir" >/dev/null); then
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

apply_handoff() {
    local input="$1" install_root="$2" backend="${3:-external}" temp_root stage commit
    is_abs_safe "$input" || die "handoff input must be an absolute path"
    [[ "${input,,}" == *.zip && -f "$input" && ! -L "$input" ]] || die "handoff input must be the canonical ZIP (directory input is disabled)"
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    ensure_secure_directory "$install_root/.handoff-tmp" 0700
    temp_root="$(mktemp -d "$install_root/.handoff-tmp/apply.XXXXXX")"
    trap 'rm -rf -- "$temp_root"' RETURN
    stage="$(stage_handoff "$input" "$temp_root")"
    commit="$(verify_manifest "$stage")"
    [[ "$backend" =~ ^(external|gemma-vllm|deepseek-llamacpp|sglang-cuda)$ ]] || die "unsupported backend: $backend"
    atomic_activate "$stage" "$install_root" "$commit"
    trap - RETURN
    rm -rf -- "$temp_root"
    log "Handoff apply complete. Run current/source/deploy/enterprise/deploy-compose.sh up $backend after target model/digest checks."
}

rollback_handoff() {
    local install_root="$1" commit="$2" release tmp resolved release_mode
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "rollback source-commit must be a full lowercase 40-character SHA"
    release="$install_root/releases/$commit"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    ensure_secure_directory "$install_root" 0755
    ensure_secure_directory "$install_root/releases" 0755
    assert_current_link_safe "$install_root"
    [[ -d "$release" && ! -L "$release" ]] || die "requested handoff release is missing: $release"
    resolved="$(readlink -f -- "$release" 2>/dev/null || true)"
    [[ "$resolved" == "$install_root/releases/$commit" ]] || die "rollback release realpath escapes releases/<commit>: $release"
    [[ "$(basename -- "$resolved")" == "$commit" && "$(dirname -- "$resolved")" == "$install_root/releases" ]] || die "rollback release is not a direct commit directory"
    [[ "$(stat -c '%u' -- "$release" 2>/dev/null || true)" == 0 ]] || die "rollback release must be root-owned"
    release_mode=$((8#$(stat -c '%a' -- "$release" 2>/dev/null || printf 0)))
    (( (release_mode & 0022) == 0 )) || die "rollback release must not be group/world writable"
    verify_handoff_tree "$release"
    verify_manifest "$release" >/dev/null
    verify_checksum_file "$release"
    tmp="$install_root/.current.$$.tmp"; rm -f -- "$tmp"; ln -s -- "$release" "$tmp"
    [[ ! -e "$install_root/current" || -L "$install_root/current" ]] || { rm -f -- "$tmp"; die "current pointer changed to a non-symlink during rollback"; }
    mv -Tf -- "$tmp" "$install_root/current"
    log "Rolled back source-only handoff to commit=$commit; model/data state was not modified."
}

status_handoff() {
    local install_root="$1" current="${1:-}/current"
    is_abs_safe "$install_root" || die "install root must be a narrow absolute path"
    [[ ! -L "$install_root" ]] || die "install root must not be a symlink"
    if [[ -L "$install_root/current" ]]; then
        printf 'current=%s\n' "$(readlink -f "$install_root/current")"
        verify_manifest "$install_root/current" >/dev/null
    else
        printf 'current=none\n'
    fi
}

usage() {
    cat <<'EOF'
Usage: update-on-server.sh <command> ...
  apply <handoff.zip> <install-root> [backend]
  rollback <install-root> <source-commit>
  status <install-root>
EOF
}

main() {
    local command="${1:-}"
    case "$command" in
        apply) acquire_operation_lock "${3:-}" ;;
        rollback) acquire_operation_lock "${2:-}" ;;
    esac
    trap 'release_operation_lock' EXIT
    case "$command" in
        apply) [[ $# -ge 3 ]] || die "apply requires <handoff.zip> <install-root> [backend]"; apply_handoff "$2" "$3" "${4:-external}" ;;
        rollback) [[ $# -ge 3 ]] || die "rollback requires <install-root> <source-commit>"; rollback_handoff "$2" "$3" ;;
        status) [[ $# -ge 2 ]] || die "status requires <install-root>"; status_handoff "$2" ;;
        -h|--help|help) usage ;;
        *) usage >&2; exit 2 ;;
    esac
}
main "$@"
