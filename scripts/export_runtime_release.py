#!/usr/bin/env python3
"""Create a deterministic, data-free source export from one explicit Git SHA."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
from typing import Any

SCHEMA_VERSION = 1
EXCLUDED_PATHS = (
    "data",
    "workspaces",
    "certs",
    "artifacts",
    "cache",
    "logs",
    ".local",
    ".runtime",
    "repo.local",
    "venv",
    ".venv",
    "node_modules",
    "frontend/node_modules",
    "frontend/.next",
    "frontend/.next-dev",
    "frontend/.next-verify",
    "frontend/public/uploads",
    "config/private",
    "config/secrets",
    "credentials",
    ".ssh",
    ".aws",
    "secrets",
    ".codex-remote-attachments",
)
SOURCE_ROOTS = (
    "alembic",
    "alembic_sync_v3",
    "contracts",
    "deploy",
    "docker",
    "mobile",
    "pc_bridge",
    "resources",
    "scripts",
    "src",
    "tests",
)
FRONTEND_RECURSIVE_ROOTS = {"src", "scripts", "e2e", "e2e-live", "tests"}
SOURCE_FILES = (
    ".gitattributes",
    ".gitignore",
    "Dockerfile",
    "alembic.ini",
    "alembic_sync_v3.ini",
    "main.py",
    "pyproject.toml",
    "run.bat",
    "run.sh",
    "setup.bat",
    "setup.sh",
)
SENSITIVE_GLOBS = (
    ":(exclude,glob).env*",
    ":(exclude,glob)**/.env*",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude,glob)**/*.pem",
    ":(exclude,glob)**/*.key",
    ":(exclude,glob)**/*.p12",
    ":(exclude,glob)**/*.pfx",
    ":(exclude,glob)**/*.kdbx",
    ":(exclude,glob)**/*.der",
    ":(exclude,glob)**/*.p8",
    ":(exclude,glob)**/*.mobileprovision",
)
LOCKFILE_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
}
LOCKFILE_PREFIXES = ("requirements-",)
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROOT = REPO_ROOT / ".local"
CANDIDATE_ROOT = LOCAL_ROOT / "runtime-release-candidates"

_contract_spec = importlib.util.spec_from_file_location(
    "_aoitalk_runtime_release_contract_export",
    REPO_ROOT / "src" / "runtime_release_contract.py",
)
if _contract_spec is None or _contract_spec.loader is None:
    raise RuntimeError("runtime release contract module is unavailable")
_contract_module = importlib.util.module_from_spec(_contract_spec)
sys.modules[_contract_spec.name] = _contract_module
_contract_spec.loader.exec_module(_contract_module)
RuntimeReleaseError = _contract_module.RuntimeReleaseError
seal_candidate_manifest = _contract_module.seal_candidate_manifest
write_sealed_manifest = _contract_module.write_sealed_manifest


class ExportError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _assert_local_destination(path: Path) -> Path:
    local_root = Path(os.path.abspath(LOCAL_ROOT))
    candidate = Path(os.path.abspath(path))
    if os.path.commonpath(
        (os.path.normcase(candidate), os.path.normcase(local_root))
    ) != os.path.normcase(local_root):
        raise ExportError("export output must remain under repository .local")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if os.path.lexists(current) and _is_reparse(current):
            raise ExportError("export output contains a reparse point")
    return candidate


def _git_tree_entries(
    repository_root: Path, commit: str, tree_path: str | None = None
) -> list[tuple[str, str, str]]:
    treeish = f"{commit}:{tree_path}" if tree_path else commit
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "ls-tree",
                "-z",
                "--full-tree",
                treeish,
            ],
            check=False,
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError("source tree metadata could not be read") from None
    if result.returncode:
        raise ExportError("source tree metadata is unavailable")
    entries: list[tuple[str, str, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_type, _object_id = metadata.decode("ascii").split(" ", 2)
            name = raw_name.decode("utf-8", "strict")
        except (ValueError, UnicodeError):
            raise ExportError("source tree metadata contains an invalid path") from None
        full_name = f"{tree_path}/{name}" if tree_path else name
        entries.append((mode, object_type, full_name))
    if len(entries) > 250_000:
        raise ExportError("source tree exceeds its metadata entry limit")
    return entries


def _git_file_names(
    repository_root: Path, commit: str, prefixes: tuple[str, ...]
) -> list[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                "--full-tree",
                commit,
                "--",
                *prefixes,
            ],
            check=False,
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError("source file metadata could not be read") from None
    if result.returncode:
        raise ExportError("source file metadata is unavailable")
    try:
        names = [
            name.decode("utf-8", "strict")
            for name in result.stdout.split(b"\0")
            if name
        ]
    except UnicodeError:
        raise ExportError("source file metadata contains an invalid path") from None
    if len(names) > 250_000:
        raise ExportError("source tree exceeds its metadata entry limit")
    return names


def _selected_source_paths(repository_root: Path, commit: str) -> list[str]:
    selected: set[str] = set()
    root_entries = _git_tree_entries(repository_root, commit)
    root_files = {
        name
        for mode, object_type, name in root_entries
        if object_type == "blob" and mode in {"100644", "100755"}
    }
    selected.update(
        name
        for name in SOURCE_FILES
        if name in root_files and not _forbidden_member(name)
    )
    selected.update(
        name
        for name in _git_file_names(repository_root, commit, SOURCE_ROOTS)
        if not _forbidden_member(name)
    )

    selected.update(
        name
        for name in _git_file_names(repository_root, commit, ("config",))
        if not _forbidden_member(name)
    )

    frontend_entries = [
        entry for entry in root_entries if entry[2] == "frontend" and entry[1] == "tree"
    ]
    frontend_prefixes: list[str] = []
    if frontend_entries:
        for mode, object_type, name in _git_tree_entries(
            repository_root, commit, "frontend"
        ):
            if _forbidden_member(name):
                continue
            if object_type == "blob" and mode in {"100644", "100755"}:
                selected.add(name)
            elif object_type == "tree" and mode == "040000":
                if name == "frontend/public":
                    for public_mode, public_type, public_name in _git_tree_entries(
                        repository_root, commit, name
                    ):
                        if _forbidden_member(public_name):
                            continue
                        if public_type == "blob" and public_mode in {
                            "100644",
                            "100755",
                        }:
                            selected.add(public_name)
                        elif public_type == "tree" and public_mode == "040000":
                            frontend_prefixes.append(public_name)
                        else:
                            raise ExportError(
                                "frontend public tree contains a link or submodule"
                            )
                else:
                    frontend_prefixes.append(name)
            else:
                raise ExportError("frontend source contains a link or submodule")
    if frontend_prefixes:
        selected.update(
            name
            for name in _git_file_names(
                repository_root, commit, tuple(frontend_prefixes)
            )
            if not _forbidden_member(name)
        )

    required = {
        "main.py",
        "pyproject.toml",
        "src/runtime_release_contract.py",
        "src/api/routes/system_routes.py",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/next.config.ts",
        "frontend/scripts/next-build.mjs",
        "tests/test_runtime_release_contract.py",
        "tests/test_runtime_release_export.py",
    }
    if not required.issubset(selected):
        raise ExportError("source commit is missing a required runtime/export file")
    return sorted(selected)


def _archive_chunks(paths: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_size = 5_000
    for path in paths:
        spec = f":(literal){path}"
        size = len(spec.encode("utf-16-le")) // 2 + 8
        if current and current_size + size > 20_000:
            chunks.append(current)
            current = []
            current_size = 5_000
        if current_size + size > 24_000:
            raise ExportError("source file path exceeds the Git command-line limit")
        current.append(spec)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _run_archive(
    repository_root: Path, commit: str, archive_path: Path, source_paths: list[str]
) -> None:
    try:
        timestamp_result = subprocess.run(
            ["git", "-C", str(repository_root), "show", "-s", "--format=%ct", commit],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        timestamp = int(timestamp_result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ExportError("source commit timestamp could not be verified") from None

    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="w", format=tarfile.PAX_FORMAT) as output:
            for chunk_number, chunk in enumerate(
                _archive_chunks(source_paths), start=1
            ):
                command = [
                    "git",
                    "-C",
                    str(repository_root),
                    "archive",
                    "--format=tar",
                    commit,
                    "--",
                    *chunk,
                ]
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError:
                    raise ExportError("git archive could not be started") from None
                if process.stdout is None:
                    process.kill()
                    process.wait()
                    raise ExportError("git archive output is unavailable")
                with process.stdout:
                    try:
                        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                            for member in archive:
                                if member.isdir():
                                    continue
                                name = _safe_member_name(member.name)
                                if (
                                    name not in source_paths
                                    or _forbidden_member(name)
                                    or not member.isfile()
                                    or name in seen
                                ):
                                    raise ExportError(
                                        "Git archive returned an unselected or unsafe source path"
                                    )
                                source = archive.extractfile(member)
                                if source is None:
                                    raise ExportError(
                                        "git archive member is incomplete"
                                    )
                                normalized = tarfile.TarInfo(name)
                                normalized.size = member.size
                                normalized.mode = member.mode & 0o777
                                normalized.mtime = timestamp
                                normalized.uid = 0
                                normalized.gid = 0
                                normalized.uname = ""
                                normalized.gname = ""
                                with source:
                                    output.addfile(normalized, source)
                                seen.add(name)
                        # Git may append archive trailer blocks beyond tarfile's
                        # logical EOF. Drain the selected-source-only pipe so a
                        # writer cannot receive SIGPIPE on a valid archive.
                        while process.stdout.read(1024 * 1024):
                            pass
                    except (OSError, tarfile.TarError):
                        process.kill()
                        process.wait()
                        raise ExportError("Git archive output is invalid") from None
                if process.wait() != 0:
                    roots = sorted({path.split("/", 1)[0] for path in chunk})
                    raise ExportError(
                        f"Git archive failed for safe source chunk {chunk_number} "
                        f"({len(chunk)} paths across {','.join(roots)})"
                    )
    except Exception:
        if archive_path.exists() and not _is_reparse(archive_path):
            archive_path.unlink(missing_ok=True)
        raise


def archive_commit(repository_root: Path, commit: str, archive_path: Path) -> str:
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ExportError("source commit must be an explicit full Git SHA")
    repository_root = repository_root.resolve(strict=True)
    archive_path = _assert_local_destination(archive_path)
    if archive_path.exists():
        raise ExportError("release archive output already exists")
    try:
        checked = subprocess.run(
            ["git", "-C", str(repository_root), "cat-file", "-t", commit],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError("explicit source commit could not be inspected") from None
    if checked.returncode or checked.stdout.strip() != "commit":
        raise ExportError("explicit source commit is unavailable")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        selected = _selected_source_paths(repository_root, commit)
        _run_archive(repository_root, commit, archive_path, selected)
        _validate_archive(archive_path)
        return _sha256_file(archive_path)
    except Exception:
        if archive_path.exists() and not _is_reparse(archive_path):
            archive_path.unlink(missing_ok=True)
        raise


def _safe_member_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ExportError("archive contains an invalid path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ExportError("archive contains an invalid path")
    return relative.as_posix()


def _forbidden_member(value: str) -> bool:
    path = PurePosixPath(value)
    parts = path.parts
    if not parts:
        return True
    excluded_roots = {
        "data",
        "workspaces",
        "certs",
        "artifacts",
        "cache",
        "logs",
        "venv",
        ".venv",
        "node_modules",
        ".local",
        ".runtime",
        ".codex-remote-attachments",
        "repo.local",
    }
    if parts[0] in excluded_roots:
        return True
    if any(part in {"data", "workspaces", "certs"} for part in parts[1:]):
        return True
    if "node_modules" in parts:
        return True
    if (
        len(parts) >= 2
        and parts[0] == "frontend"
        and parts[1]
        in {
            "node_modules",
            ".next",
            ".next-dev",
            ".next-verify",
        }
    ):
        return True
    if parts[:2] in {("config", "private"), ("config", "secrets")}:
        return True
    if len(parts) >= 3 and parts[:3] == ("frontend", "public", "uploads"):
        return True
    if any(
        part.lower() in {"credentials", "secrets", ".ssh", ".aws"} for part in parts
    ):
        return True
    if "uploads" in parts:
        return True
    if any(part.lower().startswith(".env") for part in parts):
        return True
    name = path.name.lower()
    if name in {"id_rsa", "id_ed25519", "private_key"}:
        return True
    if name.endswith(
        (".pem", ".key", ".p12", ".pfx", ".kdbx", ".der", ".p8", ".mobileprovision")
    ):
        return True
    return False


def _validate_archive(archive_path: Path) -> None:
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError):
        raise ExportError("source archive is invalid") from None
    if len(members) > 200_000:
        raise ExportError("source export exceeds its file-count limit")
    for member in members:
        name = _safe_member_name(member.name)
        if _forbidden_member(name) or not (member.isdir() or member.isfile()):
            raise ExportError(
                "source archive contains a forbidden path or special file"
            )


def _extract_clean_archive(archive_path: Path, destination: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    total_bytes = 0
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > 200_000:
                raise ExportError("source export exceeds its file-count limit")
            for member in members:
                name = _safe_member_name(member.name)
                if _forbidden_member(name):
                    raise ExportError(
                        "git archive included a forbidden runtime or private path"
                    )
                target = destination.joinpath(*PurePosixPath(name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ExportError("source archive contains a link or special file")
                total_bytes += member.size
                if total_bytes > 4 * 1024 * 1024 * 1024:
                    raise ExportError("source export exceeds its size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ExportError("source archive is incomplete")
                digest = hashlib.sha256()
                with source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                files[name] = digest.hexdigest()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return files


def _source_tree_digest(files: dict[str, str]) -> str:
    entries = [{"path": name, "sha256": files[name]} for name in sorted(files)]
    return _sha256_bytes(_canonical_json(entries))


def _backend_tree_digest(files: dict[str, str]) -> str:
    selected = {
        name: digest
        for name, digest in files.items()
        if name == "main.py"
        or name.startswith(("src/", "alembic/", "alembic_sync_v3/"))
    }
    if "main.py" not in selected or "src/runtime_release_contract.py" not in selected:
        raise ExportError("source commit does not contain the pinned runtime bootstrap")
    return _source_tree_digest(selected)


def _lockfiles(files: dict[str, str]) -> dict[str, str]:
    selected = {
        path: digest
        for path, digest in files.items()
        if PurePosixPath(path).name in LOCKFILE_NAMES
        or any(
            PurePosixPath(path).name.startswith(prefix) for prefix in LOCKFILE_PREFIXES
        )
    }
    if "frontend/package-lock.json" not in selected:
        raise ExportError("source commit has no locked frontend dependency tree")
    return dict(sorted(selected.items()))


def export_commit(
    repository_root: Path,
    commit: str,
    destination: Path,
    archive_path: Path,
) -> dict[str, Any]:
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ExportError("source commit must be an explicit full Git SHA")
    repository_root = repository_root.resolve(strict=True)
    destination = _assert_local_destination(destination)
    archive_path = _assert_local_destination(archive_path)
    if destination.name != f"portal-runtime-{commit}":
        raise ExportError("candidate directory must contain the full source SHA")
    if destination.exists() or archive_path.exists():
        raise ExportError("release candidate output already exists")
    if (
        destination == archive_path
        or destination in archive_path.parents
        or archive_path in destination.parents
    ):
        raise ExportError("archive and source export paths must be separate")
    try:
        checked = subprocess.run(
            ["git", "-C", str(repository_root), "cat-file", "-t", commit],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError("explicit source commit could not be inspected") from None
    if checked.returncode or checked.stdout.strip() != "commit":
        raise ExportError("explicit source commit is unavailable")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        selected = _selected_source_paths(repository_root, commit)
        _run_archive(repository_root, commit, archive_path, selected)
        _validate_archive(archive_path)
        archive_hash = _sha256_file(archive_path)
        file_hashes = _extract_clean_archive(archive_path, destination)
        lockfiles = _lockfiles(file_hashes)
        export_receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "candidate",
            "artifact_id": f"portal-runtime-{commit}",
            "source_commit": commit,
            "source_archive_sha256": archive_hash,
            "source_tree_sha256": _source_tree_digest(file_hashes),
            "backend_tree_sha256": _backend_tree_digest(file_hashes),
            "files": [
                {"path": name, "sha256": file_hashes[name]}
                for name in sorted(file_hashes)
            ],
            "lockfiles": lockfiles,
            "excluded_roots": list(EXCLUDED_PATHS),
        }
        receipt_path = destination / "runtime-source-export.json"
        receipt_path.write_bytes(_canonical_json(export_receipt) + b"\n")
        return export_receipt
    except Exception:
        if destination.exists() and not _is_reparse(destination):
            shutil.rmtree(destination, ignore_errors=True)
        if archive_path.exists() and not _is_reparse(archive_path):
            archive_path.unlink(missing_ok=True)
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export one explicit Git commit")
    export.add_argument(
        "--commit", required=True, help="explicit full 40-character Git commit"
    )
    export.add_argument(
        "--destination",
        required=True,
        help="new source-export directory under repository .local",
    )
    export.add_argument(
        "--archive", required=True, help="new source tar under repository .local"
    )
    export.add_argument("--repository-root", default=str(REPO_ROOT))
    archive = commands.add_parser(
        "archive", help="write one reproducibility archive without extracting it"
    )
    archive.add_argument(
        "--commit", required=True, help="explicit full 40-character Git commit"
    )
    archive.add_argument(
        "--archive", required=True, help="new source tar under repository .local"
    )
    archive.add_argument("--repository-root", default=str(REPO_ROOT))
    seal = commands.add_parser(
        "seal", help="seal build and runtime receipts on a candidate export"
    )
    seal.add_argument("--candidate-root", required=True)
    seal.add_argument("--build-receipt", required=True)
    seal.add_argument("--runtime-contract", required=True)
    seal.add_argument("--previous-accepted", required=True)
    return parser.parse_args(argv)


def _read_json_object(path: Path, *, allowed_keys: set[str]) -> dict[str, object]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ExportError("receipt exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ExportError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ExportError("release receipt is unavailable or invalid") from None
    if not isinstance(payload, dict) or set(payload) != allowed_keys:
        raise ExportError("release receipt fields are incomplete or unexpected")
    return payload


def seal_candidate(
    candidate_root: Path,
    build_receipt_path: Path,
    runtime_contract_path: Path,
    previous_accepted_path: Path,
) -> Path:
    candidate_root = _assert_local_destination(candidate_root)
    if (
        candidate_root.name
        != f"portal-runtime-{candidate_root.name.removeprefix('portal-runtime-')}"
    ):
        raise ExportError("candidate directory does not include its full source SHA")
    for path in (build_receipt_path, runtime_contract_path, previous_accepted_path):
        _assert_local_destination(path)
    build = _read_json_object(
        build_receipt_path,
        allowed_keys={
            "frontend_build_id",
            "frontend_fingerprint_sha256",
            "frontend_fingerprint_digest",
            "python_version",
            "node_version",
            "python_packages",
            "node_packages",
            "restart_contract",
        },
    )
    runtime = _read_json_object(
        runtime_contract_path,
        allowed_keys={"canonical_root", "paths", "database_identity_sha256"},
    )
    previous = _read_json_object(
        previous_accepted_path,
        allowed_keys={
            "source_commit",
            "artifact_id",
            "manifest_sha256",
            "runtime_paths",
            "database_identity_sha256",
        },
    )
    try:
        manifest = seal_candidate_manifest(candidate_root, build, runtime, previous)
        if manifest.get("artifact_id") != candidate_root.name:
            raise ExportError(
                "candidate directory does not match the sealed source SHA"
            )
        return write_sealed_manifest(candidate_root, manifest)
    except RuntimeReleaseError as exc:
        raise ExportError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "archive":
        try:
            digest = archive_commit(
                Path(args.repository_root), args.commit, Path(args.archive)
            )
        except ExportError as exc:
            print(f"runtime release archive failed: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {"source_commit": args.commit, "archive_sha256": digest}, sort_keys=True
            )
        )
        return 0
    if args.command == "seal":
        try:
            manifest_path = seal_candidate(
                Path(args.candidate_root),
                Path(args.build_receipt),
                Path(args.runtime_contract),
                Path(args.previous_accepted),
            )
        except ExportError as exc:
            print(f"runtime release seal failed: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "status": "sealed; acceptance still required",
                    "manifest": manifest_path.name,
                }
            )
        )
        return 0
    try:
        receipt = export_commit(
            Path(args.repository_root),
            args.commit,
            Path(args.destination),
            Path(args.archive),
        )
    except ExportError as exc:
        print(f"runtime release export failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "source_commit": receipt["source_commit"],
                "archive_sha256": receipt["source_archive_sha256"],
                "source_tree_sha256": receipt["source_tree_sha256"],
                "backend_tree_sha256": receipt["backend_tree_sha256"],
                "file_count": len(receipt["files"]),
                "lockfile_count": len(receipt["lockfiles"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
