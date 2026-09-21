#!/usr/bin/env bash
set -Eeuo pipefail

# Install the exact APT closure declared by an offline Enterprise companion.
# This helper intentionally has no online branch. It materializes a private
# flat file: repository from the hash-verified .deb/index closure, disables all
# host/public source lists, and asks apt to install every row selected for the
# current Dockerfile phase at its exact architecture/version.

die() {
    printf 'enterprise offline APT: %s\n' "$*" >&2
    exit 1
}

root="${1:-}"
phase="${2:-all}"
[[ -n "$root" && "$root" == /* ]] || die "offline input root must be an absolute path"
case "$phase" in
  builder|runtime|nodejs|all) ;;
  *) die "offline APT phase must be builder, runtime, nodejs, or all" ;;
esac
[[ -d "$root" && ! -L "$root" ]] || die "offline input root is missing or symlinked: $root"
manifest="$root/offline-build-manifest.json"
if [[ ! -f "$manifest" || -L "$manifest" ]]; then
    manifest="$root/offline-input-manifest.json"
fi
[[ -f "$manifest" && ! -L "$manifest" ]] || die "offline input manifest is missing or symlinked"
[[ -d "$root/apt/archives" && -d "$root/apt/lists" ]] || die "offline APT archives/indexes are missing"

command -v python3 >/dev/null 2>&1 || die "python3 is required to read the offline APT lock"
repo="$(mktemp -d /tmp/enterprise-offline-apt.XXXXXX)"
selection_file="$(mktemp /tmp/enterprise-offline-apt-selection.XXXXXX)"
cleanup() {
    rm -rf -- "$repo" "$selection_file"
}
trap cleanup EXIT

# Build the local file: repository and emit the exact phase closure. The
# source Packages indexes are parsed rather than trusted as opaque bytes: each
# row must have an index record matching its name/version/arch, while every
# archive is copied into the repository with its verified SHA/size.
python3 - "$manifest" "$root" "$repo" "$phase" >"$selection_file" <<'PY'
import bz2
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import re
import shutil
import sys

manifest_path, root_text, repo_text, phase = sys.argv[1:]
root = Path(root_text)
repo = Path(repo_text)
document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
apt = document.get("apt")
if not isinstance(apt, dict):
    raise SystemExit("offline APT declaration is missing")
rows = apt.get("packages")
if not isinstance(rows, list) or not rows:
    raise SystemExit("offline APT package lock is missing")
allowed_phases = {"builder", "runtime", "nodejs"}

def parse_control(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    current = None
    for raw in payload.decode("utf-8", "strict").splitlines():
        if raw.startswith((" ", "\t")) and current:
            result[current] = result[current] + "\n" + raw.strip()
            continue
        if not raw.strip() or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        current = key
        result[key] = value.strip()
    return result

def read_index(path: Path) -> bytes:
    raw = path.read_bytes()
    lower = path.name.lower()
    if lower.endswith(".gz"):
        return gzip.decompress(raw)
    if lower.endswith(".bz2"):
        return bz2.decompress(raw)
    if lower.endswith(".xz"):
        return lzma.decompress(raw)
    return raw

def records_for(index_path: str) -> list[dict[str, str]]:
    payload = read_index(root / index_path)
    records = []
    for block in re.split(r"\n\s*\n", payload.decode("utf-8", "strict").replace("\r\n", "\n")):
        if block.strip():
            records.append(parse_control(block.encode("utf-8")))
    return records

index_records: dict[tuple[str, str, str], dict[str, str]] = {}
for index_row in apt.get("package_indexes", []):
    if not isinstance(index_row, dict):
        raise SystemExit("offline APT Packages index row is invalid")
    index_path = str(index_row.get("path", ""))
    if not index_path.startswith("apt/lists/"):
        raise SystemExit(f"offline APT Packages index path is invalid: {index_path}")
    for record in records_for(index_path):
        key = (record.get("Package", ""), record.get("Version", ""), record.get("Architecture", ""))
        if all(key) and key in index_records and index_records[key] != record:
            raise SystemExit(f"offline APT Packages index has conflicting records: {key[0]}")
        if all(key):
            index_records[key] = record

seen_names: set[str] = set()
archive_names: set[str] = set()
selected: list[tuple[str, str, str, str]] = []
for row in rows:
    if not isinstance(row, dict):
        raise SystemExit("offline APT package row is invalid")
    name, version = row.get("name"), row.get("version")
    arch = row.get("architecture") or "amd64"
    phases = row.get("phases")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
        raise SystemExit("offline APT package name is invalid")
    if name in seen_names:
        raise SystemExit(f"offline APT package is duplicated: {name}")
    seen_names.add(name)
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*", version):
        raise SystemExit(f"offline APT package version is invalid: {name}")
    if arch not in {"all", "amd64"}:
        raise SystemExit(f"offline APT architecture is invalid: {name}")
    if not isinstance(phases, list) or not phases or any(item not in allowed_phases for item in phases) or len(phases) != len(set(phases)):
        raise SystemExit(f"offline APT package phase binding is invalid: {name}")
    archive = str(row.get("archive", ""))
    if not archive.startswith("apt/archives/") or "/" in archive[len("apt/archives/"):]:
        raise SystemExit(f"offline APT archive path is invalid: {name}")
    archive_path = root / archive
    if not archive_path.is_file() or archive_path.is_symlink():
        raise SystemExit(f"offline APT archive is missing: {archive}")
    archive_name = archive_path.name
    if archive_name in archive_names:
        raise SystemExit(f"offline APT archive filename is duplicated: {archive_name}")
    archive_names.add(archive_name)
    key = (name, version, arch)
    record = index_records.get(key)
    if record is None:
        raise SystemExit(f"offline APT Packages index has no exact record: {name}")
    expected_size = int(row.get("size_bytes", -1))
    expected_sha = str(row.get("sha256", "")).lower()
    actual_size = archive_path.stat().st_size
    actual_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if expected_size != actual_size or expected_sha != actual_sha:
        raise SystemExit(f"offline APT archive hash/size mismatch: {name}")
    shutil.copyfile(archive_path, repo / archive_name)
    if phase == "all" or phase in phases:
        selected.append((name, arch, version, f"{name}:{arch}={version}"))

if not selected:
    raise SystemExit(f"offline APT package lock has no packages for phase={phase}")

(repo / "archives").mkdir(parents=True, exist_ok=True)
for archive_name in archive_names:
    shutil.copyfile(repo / archive_name, repo / "archives" / archive_name)

# Include every validated package row in the local repository so apt's resolver
# can only satisfy dependencies from the same immutable closure. The Filename
# field is rewritten to the copied flat-repository basename.
blocks: list[str] = []
for row in rows:
    name, version = str(row["name"]), str(row["version"])
    arch = str(row.get("architecture") or "amd64")
    archive_name = Path(str(row["archive"])).name
    record = dict(index_records[(name, version, arch)])
    record["Filename"] = f"./{archive_name}"
    record["Size"] = str((repo / archive_name).stat().st_size)
    record["SHA256"] = str(row["sha256"]).lower()
    fields = ["Package", "Version", "Architecture", "Multi-Arch", "Essential", "Protected",
              "Depends", "Pre-Depends", "Provides", "Conflicts", "Breaks", "Replaces",
              "Filename", "Size", "SHA256"]
    blocks.append("\n".join(f"{field}: " + str(record[field]).replace("\n", "\n ") for field in fields if record.get(field)))
(repo / "Packages").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
(repo / "lists" / "partial").mkdir(parents=True, exist_ok=True)
(repo / "archives" / "partial").mkdir(parents=True, exist_ok=True)
(repo / "sources.list").write_text(f"deb [trusted=yes] file:{repo} ./\n", encoding="utf-8")

for name, arch, version, spec in selected:
    print(f"{name}\t{arch}\t{version}\t{spec}")
PY

[[ -s "$selection_file" ]] || die "offline APT phase selection is empty"
packages=()
selected_rows=()
while IFS=$'\t' read -r name arch version spec; do
    [[ -n "$name" && -n "$arch" && -n "$version" && -n "$spec" ]] || die "offline APT phase selection is malformed"
    packages+=("$spec")
    selected_rows+=("$name"$'\t'"$arch"$'\t'"$version")
done < "$selection_file"

apt_opts=(
    -o "Dir::Etc::sourcelist=$repo/sources.list"
    -o "Dir::Etc::sourceparts=-"
    -o "Dir::State::lists=$repo/lists/"
    -o "Dir::Cache::archives=$repo/archives/"
    -o Acquire::Retries=0
    -o Acquire::AllowInsecureRepositories=true
    -o APT::Get::AllowUnauthenticated=true
)
# Only the generated file: source is visible to apt. --no-download remains a
# second fail-closed guard against any accidental resolver/network fallback.
DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" update
DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" install -y --no-download --no-install-recommends "${packages[@]}"

for selected in "${selected_rows[@]}"; do
    IFS=$'\t' read -r name arch version <<< "$selected"
    status="$(dpkg-query -W -f='${db:Status-Status}\t${Version}\t${Architecture}' "$name:$arch" 2>/dev/null || true)"
    expected=$'installed\t'"$version"$'\t'"$arch"
    [[ "$status" == "$expected" ]] || die "installed APT package does not match the locked closure: $name ($status != $expected)"
done
