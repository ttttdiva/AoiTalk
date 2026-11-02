"""Fail-closed source/runtime boundary for an explicitly selected release.

This module has a standard-library-only import surface so ``main.py`` can
validate a release before loading dotenv, application modules, or database
code.  Normal development starts remain opt-out and keep their historical
environment behavior.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit, unquote


MANIFEST_NAME = "runtime-release.json"
DEPENDENCY_INVENTORY_NAME = "runtime-dependencies.json"
ACCEPTANCE_SUFFIX = ".accepted.json"
SCHEMA_VERSION = 1
RELEASE_ROOT_ENV = "AOITALK_RUNTIME_RELEASE_ROOT"
CONFIG_ROOT_ENV = "AOITALK_RUNTIME_CONFIG_ROOT"
CACHE_ROOT_ENV = "AOITALK_RUNTIME_CACHE_ROOT"
TRANSFER_LOCK_ROOT_ENV = "AOITALK_RUNTIME_TRANSFER_LOCK_ROOT"
ACTIVE_ENV = "AOITALK_RUNTIME_RELEASE_ACTIVE"
RESTART_HOOK_TOKEN = "aoitalk-pinned-release-supervisor-exit-42-v1"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_PATH_ENV = {
    "AOITALK_DATA_DIR": "data_root",
    "AOITALK_WORKSPACES_DIR": "workspace_root",
    "AOITALK_STORAGE_ROOTS_FILE": "storage_registry_file",
    "AOITALK_GENERATED_MEDIA_DIR": "generated_media_root",
    "AOITALK_OPERATIONS_ARTIFACT_ROOT": "operations_artifacts_root",
    "AOITALK_PROJECT_ROOT": "project_root",
    "AOITALK_MIOTTS_REFS_DIR": "miotts_refs_dir",
    "AOITALK_MIOTTS_PRESETS_DIR": "miotts_presets_dir",
    "AOITALK_IRODORI_REFS_DIR": "irodori_refs_dir",
    "AOITALK_GPU_TRIAL_STATE_PATH": "gpu_trial_state_file",
    "AOITALK_PET_DATA_DIR": "pet_data_root",
    "AOITALK_LOCAL_LLM_RUNTIME_ROOT": "local_llm_runtime_root",
}
_REQUIRED_RUNTIME_PATHS = {
    "canonical_root",
    "config_root",
    "gateway_root",
    "dotenv_file",
    "data_root",
    "workspace_root",
    "storage_registry_file",
    "generated_media_root",
    "operations_artifacts_root",
    "mobile_identity_root",
    "voice_capture_root",
    "miotts_refs_dir",
    "miotts_presets_dir",
    "irodori_refs_dir",
    "transfer_lock_root",
    "cache_root",
    "heartbeat_config_dir",
    "skills_config_dir",
    "workflow_config_dir",
    "log_root",
    "shared_local_runtime_cache",
    "app_source_update_cache",
    "project_root",
    "gpu_trial_state_file",
    "pet_data_root",
    "local_llm_runtime_root",
    "legacy_config_file",
}
_SOURCE_CODE_ROOTS = (
    "src",
    "scripts",
    "alembic",
    "alembic_sync_v3",
    "plugins/AoiTalk_Plugin",
    "mobile/src",
)
_MUTABLE_TREE_NAMES = {
    ".local",
    ".runtime",
    "artifacts",
    "cache",
    "certs",
    "data",
    "logs",
    "node_modules",
    "venv",
    "workspaces",
}


class RuntimeReleaseError(RuntimeError):
    """A pinned release is incomplete, changed, or incompatible with runtime."""


@dataclass(frozen=True)
class RuntimeReleaseContext:
    source_root: Path
    canonical_root: Path
    manifest: Mapping[str, object]
    manifest_path: Path
    acceptance_path: Path
    dotenv_file: Path

    @property
    def source_commit(self) -> str:
        return str(self.manifest["source_commit"])

    @property
    def runtime_paths(self) -> Mapping[str, str | None]:
        return self.manifest["runtime_contract"]["paths"]  # type: ignore[index,return-value]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise RuntimeReleaseError("release file cannot be verified") from None
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_commit(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_RE.fullmatch(value) is not None


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _assert_no_reparse(path: Path, *, required: bool = True) -> Path:
    """Check existing components lexically; never resolve through a junction."""
    candidate = Path(os.path.abspath(os.fspath(path)))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if required:
                raise RuntimeReleaseError("runtime path is missing") from None
            continue
        except OSError:
            raise RuntimeReleaseError("runtime path cannot be inspected") from None
        if _is_reparse(info):
            raise RuntimeReleaseError("runtime path contains a reparse point")
    if required and not candidate.exists():
        raise RuntimeReleaseError("runtime path is missing")
    return candidate


def _under(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(path), os.path.normcase(root))
        ) == os.path.normcase(root)
    except ValueError:
        return False


def _absolute_path(value: object, *, base: Path | None = None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeReleaseError("runtime path contract is incomplete")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if base is None:
            raise RuntimeReleaseError("runtime path must be absolute")
        path = base / path
    return Path(os.path.abspath(os.fspath(path)))


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeReleaseError("release file inventory is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeReleaseError("release file inventory is invalid")
    if path.as_posix() != value:
        raise RuntimeReleaseError("release file inventory is invalid")
    if value == MANIFEST_NAME or value == DEPENDENCY_INVENTORY_NAME:
        return value
    return value


def _forbidden_release_path(value: str) -> bool:
    path = PurePosixPath(value)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if parts.intersection({"credentials", "secrets", ".ssh", ".aws"}):
        return True
    if "private" in parts and "config" in parts:
        return True
    if any(part.lower().startswith(".env") for part in path.parts):
        return True
    if name in {"id_rsa", "id_ed25519", "private_key"}:
        return True
    return name.endswith(
        (".pem", ".key", ".p12", ".pfx", ".kdbx", ".der", ".p8", ".mobileprovision")
    )


def file_inventory_digest(files: Mapping[str, str]) -> str:
    normalized = [{"path": path, "sha256": files[path]} for path in sorted(files)]
    return sha256_bytes(_canonical_json(normalized))


def backend_inventory_digest(files: Mapping[str, str]) -> str:
    selected = {
        path: digest
        for path, digest in files.items()
        if path == "main.py"
        or path.startswith(("src/", "alembic/", "alembic_sync_v3/"))
    }
    if "main.py" not in selected or "src/runtime_release_contract.py" not in selected:
        raise RuntimeReleaseError("release backend inventory is incomplete")
    return file_inventory_digest(selected)


def seal_candidate_manifest(
    source_root: Path,
    build_receipt: Mapping[str, object],
    runtime_contract: Mapping[str, object],
    previous_accepted: Mapping[str, object],
) -> dict[str, object]:
    """Assemble a sealed candidate receipt; acceptance remains a separate act."""
    export_path = source_root / "runtime-source-export.json"
    try:
        exported = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeReleaseError(
            "clean source export receipt is unavailable"
        ) from None
    if (
        not isinstance(exported, dict)
        or exported.get("schema_version") != SCHEMA_VERSION
    ):
        raise RuntimeReleaseError("clean source export receipt is invalid")
    files = exported.get("files")
    if not isinstance(files, list):
        raise RuntimeReleaseError("clean source export inventory is invalid")
    file_hashes: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeReleaseError("clean source export inventory is invalid")
        name = _safe_relative(item.get("path"))
        digest = item.get("sha256")
        if name in file_hashes or not _valid_hash(digest):
            raise RuntimeReleaseError("clean source export inventory is invalid")
        file_hashes[name] = str(digest)
    if file_inventory_digest(file_hashes) != exported.get("source_tree_sha256"):
        raise RuntimeReleaseError("clean source export tree receipt mismatch")
    if backend_inventory_digest(file_hashes) != exported.get("backend_tree_sha256"):
        raise RuntimeReleaseError("clean source export backend receipt mismatch")
    _verify_restart_integration(source_root, file_hashes)

    commit = exported.get("source_commit")
    artifact_id = exported.get("artifact_id")
    if not _valid_commit(commit) or artifact_id != f"portal-runtime-{commit}":
        raise RuntimeReleaseError("clean source export identity is invalid")
    archive_hash = exported.get("source_archive_sha256")
    if not _valid_hash(archive_hash):
        raise RuntimeReleaseError("clean source export archive hash is invalid")
    if build_receipt.get("restart_contract") != RESTART_HOOK_TOKEN:
        raise RuntimeReleaseError(
            "85 and admin restart integration is not yet recorded"
        )

    frontend = {
        "build_id": build_receipt.get("frontend_build_id"),
        "fingerprint_sha256": build_receipt.get("frontend_fingerprint_sha256"),
        "fingerprint_digest": build_receipt.get("frontend_fingerprint_digest"),
    }
    if (
        not isinstance(frontend["build_id"], str)
        or not frontend["build_id"]
        or not _valid_hash(frontend["fingerprint_sha256"])
        or not _valid_hash(frontend["fingerprint_digest"])
    ):
        raise RuntimeReleaseError("candidate frontend build receipt is incomplete")

    lockfiles = exported.get("lockfiles")
    if not isinstance(lockfiles, dict) or not lockfiles:
        raise RuntimeReleaseError("candidate lockfile receipt is missing")
    python_version = build_receipt.get("python_version")
    node_version = build_receipt.get("node_version")
    if not isinstance(python_version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+", python_version
    ):
        raise RuntimeReleaseError("candidate Python version receipt is invalid")
    if not isinstance(node_version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+", node_version.removeprefix("v")
    ):
        raise RuntimeReleaseError("candidate Node version receipt is invalid")
    if not isinstance(runtime_contract, dict):
        raise RuntimeReleaseError("candidate runtime contract is missing")
    runtime_paths = runtime_contract.get("paths")
    database_identity = runtime_contract.get("database_identity_sha256")
    if (
        not isinstance(runtime_paths, dict)
        or set(runtime_paths) != _REQUIRED_RUNTIME_PATHS
        or not _valid_hash(database_identity)
    ):
        raise RuntimeReleaseError(
            "candidate runtime path and database contract is incomplete"
        )
    canonical_root = runtime_contract.get("canonical_root")
    if not isinstance(canonical_root, str) or not Path(canonical_root).is_absolute():
        raise RuntimeReleaseError("candidate canonical runtime root must be absolute")
    if runtime_paths.get("canonical_root") != canonical_root:
        raise RuntimeReleaseError(
            "candidate canonical runtime root does not match its path contract"
        )

    previous_commit = previous_accepted.get("source_commit")
    previous_id = previous_accepted.get("artifact_id")
    previous_manifest = previous_accepted.get("manifest_sha256")
    previous_paths = previous_accepted.get("runtime_paths")
    previous_database = previous_accepted.get("database_identity_sha256")
    if (
        not _valid_commit(previous_commit)
        or previous_commit == commit
        or previous_id != f"portal-runtime-{previous_commit}"
        or not _valid_hash(previous_manifest)
        or previous_paths != runtime_paths
        or previous_database != database_identity
    ):
        raise RuntimeReleaseError(
            "candidate previous accepted reference is missing or incompatible"
        )

    dependency_inventory = {
        "schema_version": SCHEMA_VERSION,
        "python_version": python_version,
        "node_version": node_version.removeprefix("v"),
        "python_packages": build_receipt.get("python_packages"),
        "node_packages": build_receipt.get("node_packages"),
        "lockfiles": lockfiles,
    }
    for package_field in ("python_packages", "node_packages"):
        package_list = dependency_inventory[package_field]
        if not isinstance(package_list, list):
            raise RuntimeReleaseError("candidate dependency inventory is incomplete")
        for package in package_list:
            if (
                not isinstance(package, dict)
                or not isinstance(package.get("name"), str)
                or not isinstance(package.get("version"), str)
                or not package["name"]
                or not package["version"]
            ):
                raise RuntimeReleaseError("candidate dependency inventory is invalid")
    dependency_bytes = _canonical_json(dependency_inventory) + b"\n"
    dependency_path = source_root / DEPENDENCY_INVENTORY_NAME
    _assert_no_reparse(dependency_path, required=False)
    if dependency_path.exists():
        raise RuntimeReleaseError("candidate dependency inventory already exists")
    dependency_path.write_bytes(dependency_bytes)

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "sealed",
        "artifact_id": artifact_id,
        "source_commit": commit,
        "source_archive_sha256": archive_hash,
        "source_tree_sha256": exported["source_tree_sha256"],
        "backend_tree_sha256": exported["backend_tree_sha256"],
        "files": files,
        "frontend": frontend,
        "lockfiles": lockfiles,
        "dependencies": {
            "inventory_sha256": sha256_bytes(dependency_bytes),
            "python_version": python_version,
            "node_version": node_version.removeprefix("v"),
            "runtime_versions_sha256": sha256_bytes(
                _canonical_json(
                    {
                        "python": python_version,
                        "node": node_version.removeprefix("v"),
                    }
                )
            ),
        },
        "runtime_contract": {
            "canonical_root": canonical_root,
            "paths": runtime_paths,
            "database_identity_sha256": database_identity,
        },
        "previous_accepted": {
            "source_commit": previous_commit,
            "artifact_id": previous_id,
            "manifest_sha256": previous_manifest,
            "runtime_paths": previous_paths,
            "database_identity_sha256": previous_database,
        },
        "restart_contract": RESTART_HOOK_TOKEN,
    }
    return manifest


def write_sealed_manifest(source_root: Path, manifest: Mapping[str, object]) -> Path:
    """Write only a new candidate manifest; never marks it accepted."""
    if manifest.get("status") != "sealed":
        raise RuntimeReleaseError("only a sealed candidate manifest can be written")
    destination = source_root / MANIFEST_NAME
    _assert_no_reparse(destination, required=False)
    if destination.exists():
        raise RuntimeReleaseError("candidate manifest already exists")
    payload = _canonical_json(manifest) + b"\n"
    temporary = source_root / (MANIFEST_NAME + ".tmp")
    _assert_no_reparse(temporary, required=False)
    if temporary.exists():
        raise RuntimeReleaseError("candidate manifest temporary already exists")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return destination


def _verify_restart_integration(source_root: Path, files: Mapping[str, object]) -> None:
    route_relative = "src/api/routes/system_routes.py"
    expected_hash = files.get(route_relative)
    if not _valid_hash(expected_hash):
        raise RuntimeReleaseError(
            "admin restart integration is missing from the release source"
        )
    route_path = source_root / route_relative
    _assert_no_reparse(route_path)
    if sha256_file(route_path) != expected_hash:
        raise RuntimeReleaseError("admin restart source hash mismatch")
    try:
        source = route_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RuntimeReleaseError("admin restart source cannot be verified") from None
    if RESTART_HOOK_TOKEN not in source or "restart_launch(" not in source:
        raise RuntimeReleaseError("admin restart has no fixed-release supervisor hook")


def _parse_manifest(path: Path) -> dict[str, object]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise RuntimeReleaseError("release manifest exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RuntimeReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeReleaseError(
            "release manifest is unavailable or invalid"
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeReleaseError("release manifest is invalid")
    return payload


def _verify_file_inventory(
    source_root: Path, manifest: Mapping[str, object]
) -> dict[str, str]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeReleaseError("release file inventory is missing")
    actual: dict[str, str] = {}
    for item in raw_files:
        if not isinstance(item, dict):
            raise RuntimeReleaseError("release file inventory is invalid")
        relative = _safe_relative(item.get("path"))
        digest = item.get("sha256")
        if _forbidden_release_path(relative):
            raise RuntimeReleaseError(
                "release source inventory contains a private file path"
            )
        if not _valid_hash(digest) or relative in actual:
            raise RuntimeReleaseError("release file inventory is invalid")
        path = source_root.joinpath(*PurePosixPath(relative).parts)
        _assert_no_reparse(path)
        if not path.is_file():
            raise RuntimeReleaseError("release source file is missing")
        actual_digest = sha256_file(path)
        if actual_digest != digest:
            raise RuntimeReleaseError("release source hash mismatch")
        actual[relative] = str(digest)

    if (
        actual.get("main.py") is None
        or actual.get("src/runtime_release_contract.py") is None
    ):
        raise RuntimeReleaseError("release entrypoint inventory is incomplete")
    if file_inventory_digest(actual) != manifest.get("source_tree_sha256"):
        raise RuntimeReleaseError("release source tree fingerprint mismatch")
    if backend_inventory_digest(actual) != manifest.get("backend_tree_sha256"):
        raise RuntimeReleaseError("release backend fingerprint mismatch")
    return actual


def _verify_no_unlisted_code(source_root: Path, known_files: Mapping[str, str]) -> None:
    ignored = {
        ".git",
        ".local",
        ".runtime",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "cache",
        "certs",
        "data",
        "dist",
        "logs",
        "node_modules",
        "venv",
        "workspaces",
    }
    code_suffixes = {
        ".py",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
    }
    for root_name in (*_SOURCE_CODE_ROOTS, "frontend/src", "frontend/scripts"):
        root = source_root / Path(root_name)
        if not root.exists():
            continue
        _assert_no_reparse(root)
        for current, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            kept: list[str] = []
            for name in dirnames:
                if name in ignored:
                    continue
                child = current_path / name
                info = child.lstat()
                if _is_reparse(info):
                    raise RuntimeReleaseError("release source contains a reparse point")
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                if Path(name).suffix.lower() not in code_suffixes:
                    continue
                path = current_path / name
                relative = path.relative_to(source_root).as_posix()
                if relative not in known_files:
                    raise RuntimeReleaseError("release contains unlisted Python source")


def _verify_runtime_files(source_root: Path, manifest: Mapping[str, object]) -> None:
    frontend = manifest.get("frontend")
    dependencies = manifest.get("dependencies")
    if not isinstance(frontend, dict) or not isinstance(dependencies, dict):
        raise RuntimeReleaseError("release build and dependency provenance is missing")
    raw_inventory = manifest.get("files")
    if not isinstance(raw_inventory, list):
        raise RuntimeReleaseError("release source inventory is missing")
    file_hashes = {
        item.get("path"): item.get("sha256")
        for item in raw_inventory
        if isinstance(item, dict)
    }
    _verify_restart_integration(source_root, file_hashes)
    build_id_path = source_root / "frontend" / ".next" / "BUILD_ID"
    fingerprint_path = (
        source_root / "frontend" / ".next" / "aoitalk-build-fingerprint.json"
    )
    next_dir = _assert_no_reparse(build_id_path.parent)
    _assert_no_reparse(build_id_path)
    _assert_no_reparse(fingerprint_path)
    if not build_id_path.is_file() or not fingerprint_path.is_file():
        raise RuntimeReleaseError("release frontend build is missing")
    try:
        build_id = build_id_path.read_text(encoding="utf-8").strip()
        fingerprint_bytes = fingerprint_path.read_bytes()
        fingerprint = json.loads(fingerprint_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeReleaseError(
            "release frontend build metadata is invalid"
        ) from None
    if not build_id or build_id != frontend.get("build_id"):
        raise RuntimeReleaseError("release frontend build ID mismatch")
    if sha256_bytes(fingerprint_bytes) != frontend.get("fingerprint_sha256"):
        raise RuntimeReleaseError("release frontend fingerprint mismatch")
    if not isinstance(fingerprint, dict) or not _valid_hash(fingerprint.get("digest")):
        raise RuntimeReleaseError("release frontend fingerprint is invalid")
    if fingerprint.get("digest") != frontend.get("fingerprint_digest"):
        raise RuntimeReleaseError("release frontend source fingerprint mismatch")

    inventory_path = source_root / DEPENDENCY_INVENTORY_NAME
    _assert_no_reparse(inventory_path)
    expected_inventory_hash = dependencies.get("inventory_sha256")
    if not _valid_hash(expected_inventory_hash):
        raise RuntimeReleaseError("release dependency fingerprint is invalid")
    try:
        inventory_bytes = inventory_path.read_bytes()
        inventory = json.loads(inventory_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeReleaseError(
            "release dependency inventory is unavailable"
        ) from None
    if sha256_bytes(inventory_bytes) != expected_inventory_hash:
        raise RuntimeReleaseError("release dependency fingerprint mismatch")
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != SCHEMA_VERSION
    ):
        raise RuntimeReleaseError("release dependency inventory is invalid")
    if inventory.get("python_version") != dependencies.get("python_version"):
        raise RuntimeReleaseError("release Python dependency provenance mismatch")
    if inventory.get("node_version") != dependencies.get("node_version"):
        raise RuntimeReleaseError("release Node dependency provenance mismatch")
    runtime_versions = {
        "python": dependencies.get("python_version"),
        "node": dependencies.get("node_version"),
    }
    if sha256_bytes(_canonical_json(runtime_versions)) != dependencies.get(
        "runtime_versions_sha256"
    ):
        raise RuntimeReleaseError("release runtime version fingerprint mismatch")
    if inventory.get("lockfiles") != manifest.get("lockfiles"):
        raise RuntimeReleaseError("release dependency lockfile provenance mismatch")
    python_packages = inventory.get("python_packages")
    node_packages = inventory.get("node_packages")
    if not isinstance(python_packages, list) or not isinstance(node_packages, list):
        raise RuntimeReleaseError("release dependency inventory is invalid")
    for package in python_packages:
        if not isinstance(package, dict):
            raise RuntimeReleaseError("release Python dependency inventory is invalid")
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise RuntimeReleaseError("release Python dependency inventory is invalid")
        try:
            installed_version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeReleaseError("a pinned Python dependency is missing") from None
        if installed_version != version:
            raise RuntimeReleaseError("a pinned Python dependency version mismatch")
    for package in node_packages:
        if not isinstance(package, dict):
            raise RuntimeReleaseError("release Node dependency inventory is invalid")
        name, version = package.get("name"), package.get("version")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name, re.IGNORECASE)
        ):
            raise RuntimeReleaseError("release Node dependency inventory is invalid")
        package_json = (
            source_root
            / "frontend"
            / "node_modules"
            / Path(*name.split("/"))
            / "package.json"
        )
        _assert_no_reparse(package_json)
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RuntimeReleaseError(
                "a pinned Node dependency is unavailable"
            ) from None
        if package_data.get("name") != name or package_data.get("version") != version:
            raise RuntimeReleaseError("a pinned Node dependency version mismatch")
    lockfiles = manifest.get("lockfiles")
    if not isinstance(lockfiles, dict) or not lockfiles:
        raise RuntimeReleaseError("release lockfile provenance is missing")
    for relative, expected in lockfiles.items():
        if not isinstance(relative, str) or not _valid_hash(expected):
            raise RuntimeReleaseError("release lockfile provenance is invalid")
        path = source_root.joinpath(*PurePosixPath(_safe_relative(relative)).parts)
        _assert_no_reparse(path)
        if sha256_file(path) != expected:
            raise RuntimeReleaseError("release lockfile hash mismatch")
    node_modules = source_root / "frontend" / "node_modules"
    _assert_no_reparse(node_modules)
    if not node_modules.is_dir():
        raise RuntimeReleaseError("release frontend dependencies are missing")
    _assert_dependency_tree_has_no_reparse(node_modules)


def _assert_dependency_tree_has_no_reparse(root: Path) -> None:
    """Reject junctions in installed dependencies without reading package data."""
    visited = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in (*dirnames, *filenames):
            visited += 1
            if visited > 250_000:
                raise RuntimeReleaseError(
                    "release dependency tree exceeds its entry limit"
                )
            try:
                info = (current_path / name).lstat()
            except OSError:
                raise RuntimeReleaseError(
                    "release dependency tree cannot be inspected"
                ) from None
            if _is_reparse(info):
                raise RuntimeReleaseError(
                    "release dependency tree contains a mutable link"
                )


def _validate_acceptance(
    source_root: Path,
    canonical_root: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    check_previous: bool = True,
) -> Path:
    source_commit = manifest.get("source_commit")
    artifact_id = manifest.get("artifact_id")
    if (
        not _valid_commit(source_commit)
        or artifact_id != f"portal-runtime-{source_commit}"
    ):
        raise RuntimeReleaseError("release source identity is invalid")
    if source_root.name != artifact_id:
        raise RuntimeReleaseError("release folder and source identity disagree")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeReleaseError("release manifest schema is unsupported")
    if manifest.get("status") != "sealed":
        raise RuntimeReleaseError("release candidate has not been accepted")
    if not _valid_hash(manifest.get("source_archive_sha256")):
        raise RuntimeReleaseError("release archive provenance is missing")
    if manifest.get("restart_contract") != RESTART_HOOK_TOKEN:
        raise RuntimeReleaseError("release restart contract is not integrated")

    acceptance_path = (
        canonical_root / "artifacts" / "releases" / f"{artifact_id}{ACCEPTANCE_SUFFIX}"
    )
    _assert_no_reparse(acceptance_path)
    acceptance = _parse_manifest(acceptance_path)
    manifest_digest = sha256_file(manifest_path)
    if (
        acceptance.get("schema_version") != SCHEMA_VERSION
        or acceptance.get("status") != "accepted"
        or acceptance.get("source_commit") != source_commit
        or acceptance.get("artifact_id") != artifact_id
        or acceptance.get("manifest_sha256") != manifest_digest
    ):
        raise RuntimeReleaseError("release acceptance receipt is missing or mismatched")

    previous = manifest.get("previous_accepted")
    if not isinstance(previous, dict):
        raise RuntimeReleaseError("release rollback reference is missing")
    previous_commit = previous.get("source_commit")
    previous_artifact = previous.get("artifact_id")
    if (
        not _valid_commit(previous_commit)
        or previous_artifact != f"portal-runtime-{previous_commit}"
        or previous_commit == source_commit
    ):
        raise RuntimeReleaseError("release rollback reference is invalid")
    if not _valid_hash(previous.get("manifest_sha256")):
        raise RuntimeReleaseError("release rollback provenance is missing")
    if check_previous:
        previous_root = (
            canonical_root / "artifacts" / "releases" / str(previous_artifact)
        )
        if not previous_root.is_dir():
            raise RuntimeReleaseError("previous accepted release is unavailable")
        _assert_no_reparse(previous_root)
        previous_manifest_path = previous_root / MANIFEST_NAME
        _assert_no_reparse(previous_manifest_path)
        if sha256_file(previous_manifest_path) != previous.get("manifest_sha256"):
            raise RuntimeReleaseError("previous accepted release manifest changed")
        previous_manifest = _parse_manifest(previous_manifest_path)
        _verify_unbundled_paths(previous_root)
        previous_acceptance_path = (
            canonical_root
            / "artifacts"
            / "releases"
            / f"{previous_artifact}{ACCEPTANCE_SUFFIX}"
        )
        _assert_no_reparse(previous_acceptance_path)
        previous_acceptance = _parse_manifest(previous_acceptance_path)
        if (
            previous_manifest.get("status") != "sealed"
            or previous_manifest.get("source_commit") != previous_commit
            or previous_acceptance.get("status") != "accepted"
            or previous_acceptance.get("source_commit") != previous_commit
            or previous_acceptance.get("artifact_id") != previous_artifact
            or previous_acceptance.get("manifest_sha256")
            != previous.get("manifest_sha256")
        ):
            raise RuntimeReleaseError("previous accepted release is unavailable")
    return acceptance_path


def _database_identity(environ: Mapping[str, str]) -> str:
    url = str(environ.get("DATABASE_URL", "")).strip()
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {
                "postgres",
                "postgresql",
                "postgresql+psycopg",
                "postgresql+asyncpg",
            }:
                raise ValueError
            host = (parsed.hostname or "").lower()
            database = unquote(parsed.path.lstrip("/"))
            port = parsed.port or 5432
        except (ValueError, UnicodeError):
            raise RuntimeReleaseError(
                "canonical database configuration is invalid"
            ) from None
        schema = str(environ.get("POSTGRES_SCHEMA", "public")).strip() or "public"
    else:
        host = str(environ.get("POSTGRES_HOST", "")).strip().lower()
        database = str(environ.get("POSTGRES_DB", "")).strip()
        raw_port = str(environ.get("POSTGRES_PORT", "")).strip()
        schema = str(environ.get("POSTGRES_SCHEMA", "public")).strip() or "public"
        if not host or not database or not raw_port:
            raise RuntimeReleaseError("canonical database configuration is missing")
        try:
            port = int(raw_port)
        except ValueError:
            raise RuntimeReleaseError(
                "canonical database configuration is invalid"
            ) from None
    if not host or not database or not 1 <= int(port) <= 65535:
        raise RuntimeReleaseError("canonical database configuration is invalid")
    identity = {
        "driver": "postgresql",
        "host": host,
        "port": int(port),
        "database": database,
        "schema": schema,
    }
    return sha256_bytes(_canonical_json(identity))


def _resolve_configured_path(
    environ: Mapping[str, str], name: str, default: Path, canonical_root: Path
) -> Path:
    raw = str(environ.get(name, "")).strip()
    path = _absolute_path(raw, base=canonical_root) if raw else default
    return Path(os.path.abspath(os.fspath(path)))


def resolve_runtime_paths(
    environ: Mapping[str, str], canonical_root: Path
) -> dict[str, str | None]:
    """Resolve durable runtime paths without creating directories or files."""
    root = Path(os.path.abspath(os.fspath(canonical_root)))
    config_root = Path(os.path.abspath(str(environ.get(CONFIG_ROOT_ENV, root))))
    gateway_root = _resolve_configured_path(
        environ, "GATEWAY_ROOT", root.parent / "85_CaddyGateway", root.parent
    )
    data_root = _resolve_configured_path(
        environ, "AOITALK_DATA_DIR", root / "data", root
    )
    workspace_root = _resolve_configured_path(
        environ, "AOITALK_WORKSPACES_DIR", root / "workspaces", root
    )
    storage = _resolve_configured_path(
        environ,
        "AOITALK_STORAGE_ROOTS_FILE",
        root / "data" / "storage_roots.json",
        root,
    )
    generated_media = _resolve_configured_path(
        environ, "AOITALK_GENERATED_MEDIA_DIR", root / "data" / "generated_media", root
    )
    operations = _resolve_configured_path(
        environ,
        "AOITALK_OPERATIONS_ARTIFACT_ROOT",
        root / "data" / "operations_artifacts",
        root,
    )
    project = _resolve_configured_path(environ, "AOITALK_PROJECT_ROOT", root, root)
    pet_data = _resolve_configured_path(
        environ, "AOITALK_PET_DATA_DIR", root / "data" / "pets", root
    )
    local_llm_runtime = _resolve_configured_path(
        environ,
        "AOITALK_LOCAL_LLM_RUNTIME_ROOT",
        root / ".aoitalk-local-llm" / "runtime",
        root,
    )
    miotts_refs = _resolve_configured_path(
        environ, "AOITALK_MIOTTS_REFS_DIR", root / "config" / "miotts_refs", root
    )
    miotts_presets = _resolve_configured_path(
        environ, "AOITALK_MIOTTS_PRESETS_DIR", root / "config" / "miotts_presets", root
    )
    irodori_refs = _resolve_configured_path(
        environ, "AOITALK_IRODORI_REFS_DIR", root / "config" / "irodori_refs", root
    )
    docker = str(environ.get("AOITALK_DOCKER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    legacy_config = (
        root / "config" / ("config.docker.yaml" if docker else "config.yaml")
    )
    legacy_config_value = str(legacy_config) if legacy_config.is_file() else None
    return {
        "canonical_root": str(root),
        "config_root": str(config_root),
        "gateway_root": str(gateway_root),
        "dotenv_file": str(root / ".env"),
        "legacy_config_file": legacy_config_value,
        "data_root": str(data_root),
        "workspace_root": str(workspace_root),
        "storage_registry_file": str(storage),
        "generated_media_root": str(generated_media),
        "operations_artifacts_root": str(operations),
        "mobile_identity_root": str(data_root),
        "voice_capture_root": str(data_root / "character_voice_captures"),
        "miotts_refs_dir": str(miotts_refs),
        "miotts_presets_dir": str(miotts_presets),
        "irodori_refs_dir": str(irodori_refs),
        "transfer_lock_root": str(data_root / "search" / "transfer-locks"),
        "cache_root": str(root / "cache"),
        "heartbeat_config_dir": str(config_root / "config" / "heartbeats"),
        "skills_config_dir": str(config_root / "config" / "skills"),
        "workflow_config_dir": str(config_root / "config" / "workflows"),
        "log_root": str(root / "logs"),
        "shared_local_runtime_cache": str(root / "cache" / "shared_local_runtime"),
        "app_source_update_cache": str(root / "cache" / "app_source_updates"),
        "project_root": str(project),
        "gpu_trial_state_file": str(
            _resolve_configured_path(
                environ,
                "AOITALK_GPU_TRIAL_STATE_PATH",
                data_root / "gpu_admission" / "trial-state.json",
                root,
            )
        ),
        "pet_data_root": str(pet_data),
        "local_llm_runtime_root": str(local_llm_runtime),
    }


def build_runtime_contract(
    environ: Mapping[str, str], canonical_root: Path
) -> dict[str, object]:
    """Build a secret-free path and database identity receipt in memory."""
    root = Path(os.path.abspath(os.fspath(canonical_root)))
    return {
        "canonical_root": str(root),
        "paths": resolve_runtime_paths(environ, root),
        "database_identity_sha256": _database_identity(environ),
    }


def _verify_path_contract(
    manifest: Mapping[str, object],
    canonical_root: Path,
    environ: Mapping[str, str],
    *,
    after_dotenv: bool,
) -> dict[str, str | None]:
    contract = manifest.get("runtime_contract")
    if not isinstance(contract, dict):
        raise RuntimeReleaseError("canonical runtime contract is missing")
    expected_paths = contract.get("paths")
    if (
        not isinstance(expected_paths, dict)
        or set(expected_paths) != _REQUIRED_RUNTIME_PATHS
    ):
        raise RuntimeReleaseError("canonical runtime path contract is incomplete")
    paths = (
        resolve_runtime_paths(environ, canonical_root)
        if after_dotenv
        else expected_paths
    )
    if dict(expected_paths) != paths:
        raise RuntimeReleaseError(
            "effective canonical runtime paths differ from the release contract"
        )
    if contract.get("database_identity_sha256") is None:
        if after_dotenv:
            raise RuntimeReleaseError("canonical database identity is missing")
    elif after_dotenv and _database_identity(environ) != contract.get(
        "database_identity_sha256"
    ):
        raise RuntimeReleaseError(
            "canonical database identity differs from the release contract"
        )

    required_dirs = (
        canonical_root,
        canonical_root / "config",
        canonical_root / "data",
        canonical_root / "workspaces",
    )
    for path in required_dirs:
        checked = _assert_no_reparse(path)
        if not checked.is_dir():
            raise RuntimeReleaseError("canonical runtime directory is invalid")
    dotenv = _assert_no_reparse(canonical_root / ".env")
    if not dotenv.is_file():
        raise RuntimeReleaseError("canonical runtime configuration is missing")
    legacy_config = paths.get("legacy_config_file")
    if legacy_config is not None:
        config_path = _assert_no_reparse(Path(legacy_config))
        if not config_path.is_file():
            raise RuntimeReleaseError("canonical runtime configuration is missing")
    for role in ("data_root", "workspace_root"):
        path = _assert_no_reparse(Path(str(paths[role])))
        if not path.is_dir():
            raise RuntimeReleaseError("canonical persistent root is invalid")
    for path_value in paths.values():
        if path_value is not None:
            _assert_no_reparse(Path(path_value), required=False)
    for role, env_name in (
        ("storage_registry_file", "AOITALK_STORAGE_ROOTS_FILE"),
        ("generated_media_root", "AOITALK_GENERATED_MEDIA_DIR"),
        ("operations_artifacts_root", "AOITALK_OPERATIONS_ARTIFACT_ROOT"),
    ):
        raw = str(environ.get(env_name, "")).strip()
        value = Path(str(paths[role]))
        if raw and not _assert_no_reparse(value, required=True).exists():
            raise RuntimeReleaseError("configured canonical storage path is missing")
    return paths


def _verify_previous_path_contract(
    manifest: Mapping[str, object], current_paths: Mapping[str, str | None]
) -> None:
    previous = manifest.get("previous_accepted")
    if not isinstance(previous, dict):
        raise RuntimeReleaseError("release rollback reference is missing")
    previous_paths = previous.get("runtime_paths")
    if not isinstance(previous_paths, dict) or dict(previous_paths) != dict(
        current_paths
    ):
        raise RuntimeReleaseError(
            "previous release does not preserve canonical runtime paths"
        )
    current_db = manifest.get("runtime_contract", {}).get("database_identity_sha256")  # type: ignore[union-attr]
    if previous.get("database_identity_sha256") != current_db:
        raise RuntimeReleaseError(
            "previous release targets a different canonical database"
        )


def _verify_import_roots(
    source_root: Path,
    canonical_root: Path,
    module_paths: Sequence[str],
    environ: Mapping[str, str],
    python_executable: str,
    python_prefix: str,
    *,
    loaded_modules: Mapping[str, object] | None = None,
) -> None:
    if str(environ.get("PYTHONPATH", "")).strip():
        raise RuntimeReleaseError("PYTHONPATH is not allowed for a pinned release")
    prefix = _assert_no_reparse(Path(python_prefix))
    expected_venv = source_root / "venv"
    if os.path.normcase(os.path.abspath(prefix)) != os.path.normcase(
        os.path.abspath(expected_venv)
    ):
        raise RuntimeReleaseError(
            "release Python environment does not match the source artifact"
        )
    executable = _assert_no_reparse(Path(python_executable))
    if not _under(executable, expected_venv):
        raise RuntimeReleaseError(
            "release Python executable is outside the source artifact"
        )

    base_prefix = Path(sys.base_prefix)
    allowed_roots = (source_root, prefix, base_prefix)
    base_site_roots = [base_prefix / "Lib" / "site-packages"]
    base_site_roots.extend(base_prefix.glob("lib/python*/site-packages"))
    for value in module_paths:
        entry = Path(value or os.getcwd())
        if not entry.is_absolute():
            entry = Path(os.path.abspath(os.fspath(entry)))
        if _under(entry, canonical_root) and not _under(entry, source_root):
            raise RuntimeReleaseError(
                "Python import path points at the mutable development tree"
            )
        if any(_under(entry, site_root) for site_root in base_site_roots):
            raise RuntimeReleaseError(
                "system Python packages are not pinned to this release"
            )
        if not any(_under(entry, allowed) for allowed in allowed_roots):
            raise RuntimeReleaseError(
                "Python import path is outside the pinned runtime"
            )
        if entry.exists():
            _assert_no_reparse(entry)

    if loaded_modules:
        for name, module in loaded_modules.items():
            if name == "src" or name.startswith("src."):
                raise RuntimeReleaseError(
                    "an application module was loaded before release preflight"
                )

    site_roots = [prefix / "Lib" / "site-packages"]
    site_roots.extend(prefix.glob("lib/python*/site-packages"))
    for site_root in site_roots:
        if not site_root.is_dir():
            continue
        _assert_no_reparse(site_root)
        _assert_dependency_tree_has_no_reparse(site_root)
        for pth in site_root.glob("*.pth"):
            _assert_no_reparse(pth)
            try:
                if pth.stat().st_size > 64 * 1024:
                    raise RuntimeReleaseError(
                        "release dependency path file exceeds its size limit"
                    )
                text = pth.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeError):
                raise RuntimeReleaseError(
                    "release dependency path file cannot be verified"
                ) from None
            for line in text.splitlines():
                stripped = line.strip()
                if (
                    not stripped
                    or stripped.startswith("#")
                    or stripped.startswith("import ")
                ):
                    continue
                candidate = Path(stripped)
                if not candidate.is_absolute():
                    candidate = site_root / candidate
                if not _under(candidate, source_root) and not _under(candidate, prefix):
                    raise RuntimeReleaseError(
                        "editable dependency points at the mutable development tree"
                    )
            if "__editable__" in text:
                raise RuntimeReleaseError(
                    "editable dependency points at the mutable development tree"
                )
        if any(site_root.glob("*.egg-link")):
            raise RuntimeReleaseError(
                "editable dependency points at the mutable development tree"
            )

    if (
        str(environ.get("NODE_PATH", "")).strip()
        or str(environ.get("NODE_OPTIONS", "")).strip()
    ):
        raise RuntimeReleaseError(
            "Node preload/import path overrides are not allowed for a pinned release"
        )


def _node_version() -> str:
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise RuntimeReleaseError("the release Node runtime is unavailable")
    system_root = os.environ.get("SystemRoot", "")
    child_env = {"PATH": os.path.dirname(node)}
    if system_root:
        child_env["SystemRoot"] = system_root
    try:
        result = subprocess.run(
            [node, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeReleaseError(
            "the release Node runtime could not be verified"
        ) from None
    value = result.stdout.strip().removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise RuntimeReleaseError("the release Node runtime version is invalid")
    return value


def _verify_unbundled_paths(source_root: Path) -> None:
    forbidden = (
        "data",
        "workspaces",
        "certs",
        ".env",
        ".env.local",
        "frontend/.env",
        "frontend/.env.local",
        ".git",
        "node_modules",
        ".local",
        ".runtime",
        "artifacts",
        "cache",
        "logs",
        "repo.local",
    )
    for relative in forbidden:
        if os.path.lexists(source_root / Path(relative)):
            raise RuntimeReleaseError(
                "release source contains a canonical persistent path"
            )
    for root in (source_root, source_root / "frontend"):
        if root.is_dir() and any(root.glob(".env*")):
            raise RuntimeReleaseError("release source contains a dotenv file")
    uploads = source_root / "frontend" / "public" / "uploads"
    if os.path.lexists(uploads):
        raise RuntimeReleaseError("release source contains a user upload root")


def prepare_runtime_release(
    entry_file: str | os.PathLike[str],
    environ: MutableMapping[str, str],
    module_paths: Sequence[str] | None = None,
    python_executable: str | None = None,
    python_prefix: str | None = None,
    python_version: str | None = None,
    node_version: str | None = None,
    loaded_modules: Mapping[str, object] | None = None,
) -> RuntimeReleaseContext | None:
    """Preflight an explicitly bound release; return ``None`` when OFF.

    The OFF branch does not read dotenv, change cwd/sys.path, connect services,
    create paths, or mutate the process environment.
    """
    configured_source = str(environ.get("GATEWAY_AOITALK_ROOT", "")).strip()
    source_root = Path(os.path.abspath(os.fspath(Path(entry_file).absolute().parent)))
    release_layout = (
        source_root.parent.name.lower() == "releases"
        and source_root.parent.parent.name.lower() == "artifacts"
    )
    if not configured_source:
        if release_layout:
            raise RuntimeReleaseError(
                "release source requires an explicit 85 root binding"
            )
        return None
    requested = _absolute_path(configured_source)
    actual_cwd = Path(os.path.abspath(os.getcwd()))
    if (
        os.path.normcase(os.path.abspath(requested))
        == os.path.normcase(os.path.abspath(source_root))
        and not release_layout
    ):
        return None

    source_root = _assert_no_reparse(source_root)
    if not source_root.is_dir():
        raise RuntimeReleaseError("pinned source root is unavailable")
    if (
        len(source_root.parents) < 3
        or source_root.parent.name != "releases"
        or source_root.parent.parent.name != "artifacts"
    ):
        raise RuntimeReleaseError(
            "pinned source must be inside the existing release directory"
        )
    canonical_root = source_root.parents[2]
    canonical_value = str(environ.get("GATEWAY_AOITALK_WORKING_DIRECTORY", "")).strip()
    if not canonical_value:
        raise RuntimeReleaseError(
            "canonical working directory is required for a pinned release"
        )
    configured_canonical = _absolute_path(canonical_value)
    if os.path.normcase(os.path.abspath(configured_canonical)) != os.path.normcase(
        os.path.abspath(canonical_root)
    ) or os.path.normcase(os.path.abspath(actual_cwd)) != os.path.normcase(
        os.path.abspath(canonical_root)
    ):
        raise RuntimeReleaseError(
            "pinned source and canonical working directory do not match"
        )
    _assert_no_reparse(canonical_root)

    manifest_path = _assert_no_reparse(source_root / MANIFEST_NAME)
    manifest = _parse_manifest(manifest_path)
    acceptance_path = _validate_acceptance(
        source_root, canonical_root, manifest_path, manifest
    )
    _verify_unbundled_paths(source_root)

    entry_path = _assert_no_reparse(Path(os.path.abspath(os.fspath(entry_file))))
    if entry_path != source_root / "main.py":
        raise RuntimeReleaseError("release entrypoint does not match the pinned source")
    files = _verify_file_inventory(source_root, manifest)
    _verify_no_unlisted_code(source_root, files)
    module_paths = tuple(sys.path if module_paths is None else module_paths)
    python_executable = (
        sys.executable if python_executable is None else python_executable
    )
    python_prefix = sys.prefix if python_prefix is None else python_prefix
    _verify_import_roots(
        source_root,
        canonical_root,
        module_paths,
        environ,
        python_executable,
        python_prefix,
        loaded_modules=loaded_modules,
    )
    _verify_runtime_files(source_root, manifest)

    expected_versions = manifest["dependencies"]
    python_version = (
        platform.python_version() if python_version is None else python_version
    )
    node_version = (
        _node_version() if node_version is None else node_version.removeprefix("v")
    )
    if expected_versions.get("python_version") != python_version:
        raise RuntimeReleaseError("release Python runtime version mismatch")
    if expected_versions.get("node_version") != node_version:
        raise RuntimeReleaseError("release Node runtime version mismatch")

    static_paths = _verify_path_contract(
        manifest, canonical_root, environ, after_dotenv=False
    )
    if not Path(str(static_paths["gateway_root"])).is_dir():
        raise RuntimeReleaseError("existing Gateway root is unavailable")
    _verify_previous_path_contract(manifest, static_paths)
    # This is the first mutation.  All source, import, build, dependency,
    # acceptance, and canonical-root checks above are read-only.
    environ[RELEASE_ROOT_ENV] = str(source_root)
    environ[CONFIG_ROOT_ENV] = str(canonical_root)
    environ[ACTIVE_ENV] = "1"
    return RuntimeReleaseContext(
        source_root=source_root,
        canonical_root=canonical_root,
        manifest=manifest,
        manifest_path=manifest_path,
        acceptance_path=acceptance_path,
        dotenv_file=canonical_root / ".env",
    )


def activate_runtime_paths(
    context: RuntimeReleaseContext, environ: MutableMapping[str, str]
) -> Mapping[str, str | None]:
    """Validate dotenv-derived DB/path identity and bind existing roots only."""
    paths = _verify_path_contract(
        context.manifest, context.canonical_root, environ, after_dotenv=True
    )
    _verify_previous_path_contract(context.manifest, paths)
    for env_name, role in _RUNTIME_PATH_ENV.items():
        value = paths[role]
        if value is not None:
            environ[env_name] = value
    environ["AOITALK_PROJECT_ROOT"] = str(paths["project_root"])
    environ["AOITALK_RUNTIME_LOG_ROOT"] = str(paths["log_root"])
    environ["AOITALK_PET_DATA_DIR"] = str(paths["pet_data_root"])
    environ["AOITALK_LOCAL_LLM_RUNTIME_ROOT"] = str(paths["local_llm_runtime_root"])
    environ["AOITALK_RUNTIME_CACHE_ROOT"] = str(paths["cache_root"])
    environ[TRANSFER_LOCK_ROOT_ENV] = str(paths["transfer_lock_root"])
    # The existing Gateway activation blob stays in 85; never copy its key.
    environ["GATEWAY_ROOT"] = str(paths["gateway_root"])
    return paths


def config_path(relative: str, source_root: Path) -> Path:
    """Resolve mutable configuration from the canonical root only when active."""
    root = os.environ.get(CONFIG_ROOT_ENV)
    if not is_runtime_release_active(source_root) or not root:
        return source_root / relative
    canonical = _absolute_path(root)
    return canonical / relative


def persistent_path(relative: str, source_root: Path) -> Path:
    """Resolve repository-owned durable state while preserving OFF behavior."""
    root = (
        os.environ.get(CONFIG_ROOT_ENV)
        if is_runtime_release_active(source_root)
        else None
    )
    if not root:
        return source_root / relative
    return _absolute_path(root) / relative


def is_runtime_release_active(source_root: Path | None = None) -> bool:
    """Ignore release-looking dotenv values unless they bind this export."""
    if os.environ.get(ACTIVE_ENV) != "1":
        return False
    configured_root = str(os.environ.get(RELEASE_ROOT_ENV, "")).strip()
    canonical_root = str(os.environ.get(CONFIG_ROOT_ENV, "")).strip()
    if not configured_root or not canonical_root:
        return False
    if source_root is None:
        source_root = Path(__file__).resolve().parents[1]
    source = Path(os.path.abspath(os.fspath(source_root)))
    configured = Path(os.path.abspath(configured_root))
    canonical = Path(os.path.abspath(canonical_root))
    if os.path.normcase(source) != os.path.normcase(configured):
        return False
    return (
        source.parent.name.lower() == "releases"
        and source.parent.parent.name.lower() == "artifacts"
        and len(source.parents) >= 3
        and os.path.normcase(source.parents[2]) == os.path.normcase(canonical)
        and re.fullmatch(r"portal-runtime-[0-9a-f]{40}", source.name) is not None
    )


def restart_launch(
    context: RuntimeReleaseContext, exit_code: int
) -> dict[str, str] | None:
    """Describe the only valid restart target for the future supervisor hook."""
    if exit_code != 42:
        return None
    python = (
        context.source_root
        / "venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    return {
        "entry": str(context.source_root / "main.py"),
        "executable": str(python),
        "working_directory": str(context.canonical_root),
        "source_root": str(context.source_root),
    }


def previous_accepted_root(context: RuntimeReleaseContext) -> Path:
    """Resolve the previous accepted artifact without touching durable data."""
    previous = context.manifest.get("previous_accepted")
    if not isinstance(previous, dict):
        raise RuntimeReleaseError("release rollback reference is missing")
    root = (
        context.canonical_root / "artifacts" / "releases" / str(previous["artifact_id"])
    )
    _assert_no_reparse(root)
    manifest_path = _assert_no_reparse(root / MANIFEST_NAME)
    if sha256_file(manifest_path) != previous.get("manifest_sha256"):
        raise RuntimeReleaseError("previous accepted release manifest changed")
    manifest = _parse_manifest(manifest_path)
    _validate_acceptance(
        root, context.canonical_root, manifest_path, manifest, check_previous=False
    )
    _verify_unbundled_paths(root)
    files = _verify_file_inventory(root, manifest)
    _verify_no_unlisted_code(root, files)
    _verify_runtime_files(root, manifest)
    _verify_previous_path_contract(manifest, context.runtime_paths)
    if manifest.get("runtime_contract", {}).get(
        "database_identity_sha256"
    ) != context.manifest.get("runtime_contract", {}).get("database_identity_sha256"):  # type: ignore[union-attr]
        raise RuntimeReleaseError(
            "previous release targets a different canonical database"
        )
    return root


__all__ = [
    "ACTIVE_ENV",
    "ACCEPTANCE_SUFFIX",
    "CONFIG_ROOT_ENV",
    "DEPENDENCY_INVENTORY_NAME",
    "MANIFEST_NAME",
    "RELEASE_ROOT_ENV",
    "RESTART_HOOK_TOKEN",
    "RuntimeReleaseContext",
    "RuntimeReleaseError",
    "activate_runtime_paths",
    "backend_inventory_digest",
    "build_runtime_contract",
    "config_path",
    "file_inventory_digest",
    "is_runtime_release_active",
    "persistent_path",
    "prepare_runtime_release",
    "previous_accepted_root",
    "restart_launch",
    "resolve_runtime_paths",
    "seal_candidate_manifest",
    "sha256_bytes",
    "sha256_file",
    "write_sealed_manifest",
]
