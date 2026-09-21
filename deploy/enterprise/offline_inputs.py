#!/usr/bin/env python3
"""Validation and hydration for restricted-network Enterprise build inputs.

The Enterprise application handoff contains source only.  A separately
transported, source-bound companion contains the immutable build inputs that a
target without Internet egress needs (OCI archives, APT metadata/packages,
NodeSource metadata, Python wheels, and the npm cache).  This module is kept
dependency-free so it can run on the target before Docker is invoked.
"""

from __future__ import annotations

import argparse
import base64
import bz2
import hashlib
import io
import json
import gzip
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile
import lzma


FORMAT_SUFFIX = "-enterprise-offline-inputs"
VERSION = 1
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024
REQUIRED_CATEGORIES = {
    "oci-images": "images/",
    "apt-debs": "apt/archives/",
    "apt-indexes": "apt/lists/",
    "nodesource": "nodesource/",
    "python-wheels": "python-wheels/",
    "npm-cache": "npm-cache/",
}
# These are the explicitly requested packages in the canonical Dockerfile.
# The companion may contain additional dependency closure rows, but it may not
# omit any of these immutable top-level inputs.
REQUIRED_APT_NAMES = frozenset(
    {
        "build-essential",
        "cmake",
        "libssl-dev",
        "libffi-dev",
        "libpq-dev",
        "portaudio19-dev",
        "libsndfile1-dev",
        "git",
        "libportaudio2",
        "libportaudiocpp0",
        "ffmpeg",
        "sox",
        "libsox-fmt-all",
        "libsndfile1",
        "libpq5",
        "postgresql-client",
        "fonts-noto-cjk",
        "fonts-noto-cjk-extra",
        "locales",
        "curl",
        "ca-certificates",
        "gosu",
        "nodejs",
    }
)
# New handoffs explicitly bind the runtime timezone contract.  Keep this
# separate from the historical base set so an already-issued companion can be
# validated for existing-platform compatibility, while every new handoff must
# carry the OS tzdata database used by Python and Node.js.
TIMEZONE_REQUIRED_APT_NAMES = frozenset({"tzdata"})
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMMUTABLE_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
FORBIDDEN_COMPONENTS = {
    ".git", "node_modules", "venv", "mobile", "keys", "certs", "tokens",
    "data", "logs", "private", "model", "models", "secret", "secrets",
    "weights", "weight", "checkpoint", "checkpoints", "huggingface",
}
FORBIDDEN_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".crt", ".cer", ".token", ".secret")
FORBIDDEN_BASENAMES = {".npmrc", ".pypirc", ".netrc", "auth.json", "credentials.json", "config.json"}


class OfflineInputError(ValueError):
    """Raised when a companion is malformed or not bound to its handoff."""


def _fail(message: str) -> None:
    raise OfflineInputError(message)


def _load_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        _fail(f"manifest is missing or symlinked: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - error text is diagnostic
        _fail(f"invalid JSON ({path}): {exc}")
    if not isinstance(value, dict):
        _fail(f"JSON document must be an object: {path}")
    return value


def safe_relative(value: object, *, field: str = "path") -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        _fail(f"unsafe offline input {field}: {value!r}")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        _fail(f"unsafe offline input {field}: {value!r}")
    # PurePosixPath collapses repeated separators; reject those before this
    # conversion so manifest keys cannot alias each other.
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != value:
        _fail(f"offline input {field} is not canonical: {value!r}")
    folded_parts = [part.casefold() for part in parts]
    if any(part in FORBIDDEN_COMPONENTS or part == ".env" or part.startswith(".env.") for part in folded_parts):
        _fail(f"offline input {field} crosses the secret/data boundary: {value!r}")
    if any(part.endswith(FORBIDDEN_SUFFIXES) for part in folded_parts):
        _fail(f"offline input {field} has a forbidden credential suffix: {value!r}")
    if folded_parts[-1] in FORBIDDEN_BASENAMES:
        _fail(f"offline input {field} has a forbidden credential filename: {value!r}")
    return normalized


def _assert_safe_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        _fail(f"offline input root is missing or symlinked: {root}")
    for entry in root.rglob("*"):
        if entry.is_symlink():
            _fail(f"offline input contains a symlink: {entry.relative_to(root)}")
        try:
            entry_stat = entry.stat()
            mode = entry_stat.st_mode
        except OSError as exc:
            _fail(f"cannot inspect offline input: {entry}: {exc}")
        if not (entry.is_dir() or stat.S_ISREG(mode)):
            _fail(f"offline input contains a non-regular entry: {entry.relative_to(root)}")
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            if entry_stat.st_uid != 0 or stat.S_IMODE(mode) & 0o022:
                _fail(f"offline input entry is not root-owned and private: {entry.relative_to(root)}")


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        _fail(f"cannot read offline input {path}: {exc}")
    return size, digest.hexdigest()


def _file_records(root: Path) -> dict[str, tuple[int, str]]:
    actual: dict[str, tuple[int, str]] = {}
    folded: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in {"offline-build-manifest.json", "offline-input-manifest.json"}:
            continue
        safe_relative(rel)
        key = rel.casefold()
        if key in folded:
            _fail(f"offline input contains a case-colliding path: {rel}")
        folded.add(key)
        actual[rel] = _hash_file(path)
    return actual


def source_tree_digest(source_root: Path) -> str:
    """Return the deterministic digest used to bind a companion to source/."""
    if source_root.is_symlink() or not source_root.is_dir():
        _fail("handoff source tree is missing or symlinked")
    records: list[str] = []
    paths = list(source_root.rglob("*"))
    paths.sort(key=lambda path: (path.relative_to(source_root).as_posix().casefold(), path.relative_to(source_root).as_posix()))
    for path in paths:
        if path.is_symlink():
            _fail(f"handoff source tree contains an unsafe entry: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            _fail(f"handoff source tree contains an unsafe entry: {path}")
        rel = path.relative_to(source_root).as_posix()
        if "\\" in rel or not rel or any(part in {"", ".", ".."} for part in rel.split("/")):
            _fail(f"handoff source tree path is not canonical: {rel!r}")
        size, digest = _hash_file(path)
        records.append(f"{digest}  {rel}\n")
    if not records:
        _fail("handoff source tree is empty")
    return hashlib.sha256("".join(records).encode("utf-8")).hexdigest()


def handoff_binding_digest(handoff: dict) -> str:
    """Digest the immutable handoff contract without circular offline fields."""
    canonical = dict(handoff)
    canonical.pop("offline_build", None)
    canonical.pop("generated_at_utc", None)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_records(document: dict) -> dict[str, tuple[int, str]]:
    rows = document.get("files")
    if not isinstance(rows, list) or not rows:
        _fail("offline input manifest files are missing")
    expected: dict[str, tuple[int, str]] = {}
    folded: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("offline input manifest contains a non-object file row")
        rel = safe_relative(row.get("path"))
        if rel in {"offline-build-manifest.json", "offline-input-manifest.json"} or rel in expected:
            _fail(f"duplicate/manifest offline input file: {rel}")
        key = rel.casefold()
        if key in folded:
            _fail(f"case-colliding offline input file: {rel}")
        folded.add(key)
        digest = str(row.get("sha256", "")).lower()
        size = row.get("size_bytes")
        if not HEX64.fullmatch(digest):
            _fail(f"offline input file hash is invalid: {rel}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(f"offline input file size is invalid: {rel}")
        expected[rel] = (size, digest)
    return expected


def _verify_file_coverage(root: Path, document: dict) -> dict[str, tuple[int, str]]:
    expected = _required_records(document)
    actual = _file_records(root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        _fail(f"offline input file coverage mismatch: missing={missing} extra={extra}")
    for rel, value in expected.items():
        if actual[rel] != value:
            _fail(f"offline input hash/size mismatch: {rel}")
    for category, prefix in REQUIRED_CATEGORIES.items():
        if not any(path.startswith(prefix) for path in expected):
            _fail(f"offline input category is missing: {category}")
    return expected


def _verify_image_pins(document: dict, handoff: dict, files: dict[str, tuple[int, str]], root: Path) -> None:
    rows = document.get("image_pins")
    if not isinstance(rows, list) or not rows:
        _fail("offline input image_pins are missing")
    image_by_name: dict[str, tuple[str, str, str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("offline input image_pins contains a non-object row")
        name = row.get("name")
        ref = row.get("ref")
        archive_value = row.get("archive")
        layout_value = row.get("oci_layout")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            _fail("offline input image pin name is invalid")
        if name in image_by_name:
            _fail(f"duplicate offline input image pin: {name}")
        if not isinstance(ref, str) or not IMMUTABLE_IMAGE.fullmatch(ref):
            _fail(f"offline input image pin is not immutable: {name}")
        expected_digest = ref.split("@", 1)[1]
        if row.get("archive_manifest_digest") != expected_digest:
            _fail(f"offline input image archive is not bound to the canonical manifest digest: {name}")
        config_digest = row.get("config_digest")
        if not isinstance(config_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest):
            _fail(f"offline input image config digest is missing: {name}")
        if row.get("platform") != "linux/amd64":
            _fail(f"offline input image platform is not linux/amd64: {name}")
        if name.startswith("dockerfile-") or layout_value is not None:
            layout = safe_relative(layout_value, field="image OCI layout")
            if not layout.startswith("images/") or not any(path.startswith(layout.rstrip("/") + "/") for path in files):
                _fail(f"offline input base OCI layout is not checksum-covered: {name}")
            platform_manifest_digest = str(row.get("platform_manifest_digest", ""))
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", platform_manifest_digest):
                _fail(f"offline input base OCI platform manifest digest is missing: {name}")
            image_by_name[name] = (ref, layout, platform_manifest_digest, config_digest)
        else:
            _fail(f"offline input image must use a verified OCI layout: {name}")

    handoff_pins = {
        row.get("name"): row.get("ref")
        for row in handoff.get("image_pins", [])
        if isinstance(row, dict) and row.get("kind") in {"dependency", "build-base"}
    }
    if set(image_by_name) != set(handoff_pins):
        _fail("offline input image pin set does not match handoff dependency/base pins")
    for name, (ref, _archive, _platform_manifest, _config_digest) in image_by_name.items():
        if handoff_pins.get(name) != ref:
            _fail(f"offline input image pin does not match handoff: {name}")
        if _archive.startswith("images/"):
            _verify_oci_layout(root, _archive, ref, _platform_manifest, _config_digest, files)


def _verify_oci_layout(root: Path, layout: str, ref: str, platform_manifest_digest: str, config_digest: str, files: dict[str, tuple[int, str]]) -> None:
    layout_root = root / layout
    if layout_root.is_symlink() or not layout_root.is_dir():
        _fail(f"offline OCI layout is missing or symlinked: {layout}")
    layout_marker = layout_root / "oci-layout"
    index_path = layout_root / "index.json"
    if layout_marker.is_symlink() or not layout_marker.is_file() or index_path.is_symlink() or not index_path.is_file():
        _fail(f"offline OCI layout metadata is incomplete: {layout}")
    index = _load_json(index_path)
    try:
        if index.get("schemaVersion") != 2:
            _fail(f"offline OCI index schema is unsupported: {layout}")
        layout_document = _load_json(layout_marker)
        if layout_document.get("imageLayoutVersion") != "1.0.0":
            _fail(f"offline OCI layout version is unsupported: {layout}")
    except OfflineInputError:
        raise
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list):
        _fail(f"offline OCI layout index is invalid: {layout}")
    expected_index_digest = ref.split("@", 1)[1]
    matching = [row for row in descriptors if isinstance(row, dict) and row.get("digest") == expected_index_digest]
    # OCI layout index.json is a local ref map.  It must expose exactly one
    # descriptor for the handoff's canonical registry digest; platform
    # selection happens by parsing that content-addressed blob below.
    if len(matching) != 1 or len(descriptors) != 1:
        _fail(f"offline OCI layout index has undeclared/duplicate canonical descriptors: {layout}")
    if matching[0].get("mediaType") not in {"application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"}:
        _fail(f"offline OCI canonical descriptor media type is invalid: {layout}")
    index_rel = f"{layout}/blobs/sha256/{expected_index_digest.split(':', 1)[1]}"
    if index_rel not in files or _hash_file(root / index_rel)[1] != expected_index_digest.split(":", 1)[1]:
        _fail(f"offline OCI canonical descriptor blob is missing or mismatched: {index_rel}")
    canonical_size, _ = _hash_file(root / index_rel)
    if matching[0].get("size") != canonical_size:
        _fail(f"offline OCI canonical descriptor size mismatch: {index_rel}")
    canonical = _load_json(root / index_rel)
    canonical_media = str(matching[0].get("mediaType") or "")
    canonical_manifests = canonical.get("manifests") if isinstance(canonical, dict) else None
    platform_rows: list[dict] = []
    if canonical_media in {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"} or canonical_manifests is not None:
        if not isinstance(canonical_manifests, list):
            _fail(f"offline OCI canonical index/list is invalid: {layout}")
        platform_rows = [
            row for row in canonical_manifests
            if isinstance(row, dict)
            and isinstance(row.get("platform"), dict)
            and row["platform"].get("os") == "linux"
            and row["platform"].get("architecture") == "amd64"
        ]
        if len(platform_rows) != 1 or platform_rows[0].get("digest") != platform_manifest_digest:
            _fail(f"offline OCI canonical index does not select exactly one linux/amd64 manifest: {layout}")
    elif canonical_media in {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}:
        if expected_index_digest != platform_manifest_digest:
            _fail(f"offline OCI canonical manifest/platform digest mismatch: {layout}")
        platform_rows = [matching[0]]
    else:
        _fail(f"offline OCI canonical descriptor media type is not an image index/manifest: {layout}")
    platform_row = platform_rows[0]
    if platform_row.get("mediaType") not in {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}:
        _fail(f"offline OCI linux/amd64 descriptor media type is invalid: {layout}")
    manifest_rel = f"{layout}/blobs/sha256/{platform_manifest_digest.split(':', 1)[1]}"
    if manifest_rel not in files:
        _fail(f"offline OCI platform manifest blob is missing: {manifest_rel}")
    manifest = _load_json(root / manifest_rel)
    manifest_size, manifest_hash = _hash_file(root / manifest_rel)
    if manifest_hash != platform_manifest_digest.split(":", 1)[1]:
        _fail(f"offline OCI platform manifest blob hash mismatch: {manifest_rel}")
    if platform_row.get("size") != manifest_size:
        _fail(f"offline OCI platform manifest descriptor size mismatch: {manifest_rel}")
    config = manifest.get("config")
    if not isinstance(config, dict) or config.get("mediaType") not in {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"} or config.get("digest") != config_digest:
        _fail(f"offline OCI config digest does not match the verified pin: {layout}")
    config_rel = f"{layout}/blobs/sha256/{config_digest.split(':', 1)[1]}"
    if config_rel not in files:
        _fail(f"offline OCI config blob is missing: {config_rel}")
    if _hash_file(root / config_rel)[1] != config_digest.split(":", 1)[1]:
        _fail(f"offline OCI config blob hash mismatch: {config_rel}")
    if config.get("size") != _hash_file(root / config_rel)[0]:
        _fail(f"offline OCI config descriptor size mismatch: {config_rel}")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        _fail(f"offline OCI platform manifest has no layers: {layout}")
    closure = {index_rel, manifest_rel, config_rel}
    for layer in layers:
        digest = layer.get("digest") if isinstance(layer, dict) else None
        if not isinstance(layer, dict) or layer.get("mediaType") not in {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar", "application/vnd.docker.image.rootfs.diff.tar.gzip"} or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            _fail(f"offline OCI layer digest is invalid: {layout}")
        layer_rel = f"{layout}/blobs/sha256/{digest.split(':', 1)[1]}"
        if layer_rel not in files:
            _fail(f"offline OCI layer blob is missing: {layer_rel}")
        if _hash_file(root / layer_rel)[1] != digest.split(":", 1)[1]:
            _fail(f"offline OCI layer blob hash mismatch: {layer_rel}")
        if layer.get("size") != _hash_file(root / layer_rel)[0]:
            _fail(f"offline OCI layer descriptor size mismatch: {layer_rel}")
        closure.add(layer_rel)
    actual_blobs = {
        path.relative_to(root).as_posix()
        for path in (layout_root / "blobs" / "sha256").glob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_blobs != closure:
        _fail(f"offline OCI blob closure has undeclared/missing blobs: {layout}")


def _oci_allowed_digests(root: Path, row: dict) -> set[str]:
    """Return only the content-addressed closure reachable from one pin."""
    layout = root / str(row["oci_layout"])
    ref_digest = str(row["ref"]).rsplit("@", 1)[1]
    platform_digest = str(row["platform_manifest_digest"])
    allowed = {ref_digest, platform_digest, str(row["config_digest"])}
    platform_blob = layout / "blobs" / "sha256" / platform_digest.split(":", 1)[1]
    manifest = _load_json(platform_blob)
    for layer in manifest.get("layers", []):
        if isinstance(layer, dict) and isinstance(layer.get("digest"), str):
            allowed.add(layer["digest"])
    return allowed


def _registry_blob(root: Path, document: dict, repository: str, digest: str, *, as_path: bool = False) -> tuple[bytes | Path, str] | None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    for row in document.get("image_pins", []):
        if not isinstance(row, dict) or not row.get("oci_layout"):
            continue
        ref = str(row.get("ref", ""))
        if "@" not in ref or ref.rsplit("@", 1)[0] != repository:
            continue
        try:
            if digest not in _oci_allowed_digests(root, row):
                return None
        except (KeyError, OfflineInputError, OSError):
            return None
        layout = root / str(row["oci_layout"])
        blob = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
        if blob.is_file() and not blob.is_symlink() and _hash_file(blob)[1] == digest.split(":", 1)[1]:
            media_type = "application/vnd.oci.image.manifest.v1+json"
            if digest == ref.rsplit("@", 1)[1]:
                try:
                    for descriptor in _load_json(layout / "index.json").get("manifests", []):
                        if isinstance(descriptor, dict) and descriptor.get("digest") == digest:
                            media_type = str(descriptor.get("mediaType") or media_type)
                            break
                except OfflineInputError:
                    return None
            return (blob if as_path else blob.read_bytes()), media_type
    return None


def serve_registry(root: Path, port: int = 51181, host: str = "127.0.0.1", token: str = "") -> None:
    """Serve verified OCI blobs as a read-only local Distribution endpoint."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        _fail("offline registry host must be loopback-only")
    root = root.absolute()
    manifest_candidates = [root / "offline-build-manifest.json", root / "offline-input-manifest.json"]
    manifest_paths = [path for path in manifest_candidates if path.is_file() and not path.is_symlink()]
    if len(manifest_paths) != 1:
        _fail("offline registry requires exactly one offline manifest")
    document = _load_json(manifest_paths[0])
    _assert_safe_tree(root)
    # Registry serving only needs the OCI closure; APT/Python/npm categories
    # are validated by verify_companion before the launcher starts the server.
    files = _file_records(root)
    synthetic_handoff = {
        "image_pins": [
            {"name": row.get("name"), "kind": "dependency", "ref": row.get("ref")}
            for row in document.get("image_pins", [])
            if isinstance(row, dict)
        ]
    }
    _verify_image_pins(document, synthetic_handoff, files, root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EnterpriseOfflineRegistry/1"

        def _respond(self, code: int, payload: bytes | Path = b"", content_type: str = "application/octet-stream", digest: str = "") -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(payload.stat().st_size if isinstance(payload, Path) else len(payload)))
            self.send_header("Content-Type", content_type)
            self.send_header("Docker-Distribution-API-Version", "registry/2.0")
            if digest:
                self.send_header("Docker-Content-Digest", digest)
            if token:
                self.send_header("X-Offline-Registry-Nonce", token)
            self.end_headers()
            if self.command == "GET":
                if isinstance(payload, Path):
                    with payload.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
                else:
                    self.wfile.write(payload)

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle()

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._respond(405)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def _handle(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/v2/" or path == "/v2":
                self._respond(200, b"{}", "application/json")
                return
            match = re.fullmatch(r"/v2/(.+)/(manifests|blobs)/(sha256:[0-9a-f]{64})", path)
            if not match:
                self._respond(404)
                return
            repository, kind, digest = match.groups()
            value = _registry_blob(root, document, repository, digest, as_path=True)
            if value is None:
                self._respond(404)
                return
            payload, content_type = value
            self._respond(200, payload, content_type if kind == "manifests" else "application/octet-stream", digest)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _verify_nodesource(document: dict, handoff: dict, files: dict[str, tuple[int, str]], root: Path) -> None:
    metadata = document.get("nodesource")
    expected = (handoff.get("build_reproducibility") or {}).get("nodesource_setup") or {}
    if not isinstance(metadata, dict):
        _fail("offline NodeSource metadata is missing")
    if (
        metadata.get("url") != expected.get("url")
        or str(metadata.get("sha256", "")).lower() != str(expected.get("sha256", "")).lower()
        or metadata.get("https_required") is not True
    ):
        _fail("offline NodeSource metadata does not match the handoff pin")
    setup_path = safe_relative(metadata.get("path"), field="NodeSource setup path")
    sources_path = safe_relative(metadata.get("sources_list"), field="NodeSource sources list")
    keyring_path = safe_relative(metadata.get("keyring"), field="NodeSource keyring")
    if not setup_path.startswith("nodesource/") or setup_path not in files:
        _fail("offline NodeSource setup input is missing from verified files")
    if files[setup_path][1] != str(metadata.get("sha256", "")).lower():
        _fail("offline NodeSource setup input hash mismatch")
    if sources_path != "nodesource/sources.list" or sources_path not in files:
        _fail("offline NodeSource HTTPS source list is missing")
    if keyring_path != "nodesource/nodesource.gpg" or keyring_path not in files:
        _fail("offline NodeSource keyring is missing")
    keyring_hash = str(metadata.get("keyring_sha256", "")).lower()
    if not HEX64.fullmatch(keyring_hash) or files[keyring_path][1] != keyring_hash:
        _fail("offline NodeSource keyring hash binding is missing or invalid")
    source_text = (root / sources_path).read_text(encoding="utf-8", errors="strict")
    if re.search(r"(?im)^\s*[^#].*http://", source_text):
        _fail("offline NodeSource source list contains an HTTP repository")
    source_lines = [line.strip() for line in source_text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    suite = "nodistro" if (handoff.get("offline_producer") or {}).get("version") == 1 else "bookworm"
    if source_lines != [f"deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x {suite} main"]:
        _fail("offline NodeSource source list does not select node_22.x over HTTPS")
    source_hash = str(metadata.get("sources_list_sha256", "")).lower()
    if not HEX64.fullmatch(source_hash) or files[sources_path][1] != source_hash:
        _fail("offline NodeSource source list hash binding is missing or invalid")


def _parse_debian_control(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for raw in payload.decode("utf-8", "strict").splitlines():
        if not raw.strip():
            continue
        if raw[:1].isspace() and current:
            fields[current] += "\n" + raw.strip()
            continue
        if ":" not in raw:
            _fail("offline APT control metadata contains a malformed field")
        key, value = raw.split(":", 1)
        current = key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", current):
            _fail(f"offline APT control metadata field is invalid: {current}")
        fields[current] = value.strip()
    return fields


def _read_deb_control(path: Path) -> dict[str, str]:
    """Read control metadata from a Debian ar/deb without third-party libs."""
    payload = path.read_bytes()
    if not payload.startswith(b"!<arch>\n"):
        _fail(f"offline APT archive is not a Debian ar file: {path.name}")
    offset = 8
    control_payload: bytes | None = None
    while offset < len(payload):
        if offset + 60 > len(payload):
            _fail(f"offline APT archive has a truncated ar header: {path.name}")
        header = payload[offset : offset + 60]
        if header[58:60] != b"`\n":
            _fail(f"offline APT archive has an invalid ar header: {path.name}")
        name = header[:16].decode("ascii", "strict").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii", "strict").strip())
        except ValueError:
            _fail(f"offline APT archive has an invalid member size: {path.name}")
        start, end = offset + 60, offset + 60 + size
        if end > len(payload):
            _fail(f"offline APT archive member exceeds file: {path.name}")
        member = payload[start:end]
        if name.startswith("control.tar"):
            control_payload = member
            break
        offset = end + (size % 2)
    if control_payload is None:
        _fail(f"offline APT archive has no control.tar member: {path.name}")
    if name.endswith(".gz"):
        control_payload = gzip.decompress(control_payload)
    elif name.endswith(".bz2"):
        control_payload = bz2.decompress(control_payload)
    elif name.endswith(".xz"):
        control_payload = lzma.decompress(control_payload)
    try:
        with tarfile.open(fileobj=io.BytesIO(control_payload), mode="r:") as archive:
            members = [member for member in archive.getmembers() if member.isfile() and PurePosixPath(member.name).name == "control"]
            if len(members) != 1:
                _fail(f"offline APT archive control.tar must contain exactly one control file: {path.name}")
            control = archive.extractfile(members[0])
            if control is None:
                _fail(f"offline APT archive control file cannot be read: {path.name}")
            return _parse_debian_control(control.read())
    except tarfile.TarError as exc:
        _fail(f"offline APT control.tar is invalid: {path.name}: {exc}")


def _parse_packages_index(payload: bytes, path: str) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail(f"offline APT Packages index is not UTF-8: {path}")
    records: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        if not block.strip():
            continue
        records.append(_parse_debian_control(block.encode("utf-8")))
    return records


def _verify_apt(
    document: dict,
    files: dict[str, tuple[int, str]],
    root: Path,
    handoff: dict | None = None,
) -> None:
    apt = document.get("apt")
    if not isinstance(apt, dict) or not isinstance(apt.get("packages"), list) or not apt.get("packages"):
        _fail("offline APT package/version metadata is missing")
    if apt.get("network_required") is not False or apt.get("install_mode") != "no-download":
        _fail("offline APT contract permits a network fallback")
    seen: set[str] = set()
    deb_files = {path for path in files if path.startswith("apt/archives/") and path.lower().endswith(".deb")}
    declared_archives: set[str] = set()
    package_rows: dict[tuple[str, str, str], dict] = {}
    allowed_phases = {"builder", "runtime", "nodejs"}
    for row in apt["packages"]:
        if not isinstance(row, dict):
            _fail("offline APT package row is not an object")
        name, version = row.get("name"), row.get("version")
        archive = safe_relative(row.get("archive"), field="APT archive")
        digest = str(row.get("sha256", "")).lower()
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) or name in seen:
            _fail("offline APT package name is invalid or duplicated")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*", version):
            _fail(f"offline APT package version is invalid: {name}")
        phases = row.get("phases")
        if not isinstance(phases, list) or not phases or any(
            not isinstance(phase, str) or phase not in allowed_phases for phase in phases
        ) or len(phases) != len(set(phases)):
            _fail(f"offline APT package phase binding is invalid: {name}")
        if not archive.startswith("apt/archives/") or archive not in files:
            _fail(f"offline APT .deb archive is missing: {name}")
        if not HEX64.fullmatch(digest) or files[archive][1] != digest:
            _fail(f"offline APT .deb hash mismatch: {name}")
        control = _read_deb_control(root / archive)
        architecture = str(row.get("architecture") or control.get("Architecture") or "")
        if control.get("Package") != name or control.get("Version") != version:
            _fail(f"offline APT .deb control metadata does not match package row: {name}")
        if architecture not in {"all", "amd64"} or control.get("Architecture") != architecture:
            _fail(f"offline APT .deb architecture is not linux/amd64-compatible: {name}")
        if row.get("size_bytes") is not None and row.get("size_bytes") != files[archive][0]:
            _fail(f"offline APT .deb size metadata mismatch: {name}")
        declared_archives.add(archive)
        package_rows[(name, version, architecture)] = row
        seen.add(name)
    if set(apt.get("required_names") or []) != seen:
        _fail("offline APT required_names do not match package rows")
    required_names = set(REQUIRED_APT_NAMES)
    runtime_contract = (handoff or {}).get("runtime_contract") if isinstance(handoff, dict) else None
    timezone_contract = runtime_contract.get("timezone") if isinstance(runtime_contract, dict) else None
    if isinstance(timezone_contract, dict) and timezone_contract.get("tzdata_required") is True:
        required_names.update(TIMEZONE_REQUIRED_APT_NAMES)
    if not required_names.issubset(seen):
        _fail("offline APT package lock omits a canonical Dockerfile dependency")
    if deb_files != declared_archives:
        _fail(f"offline APT archive closure has undeclared/missing .deb files: {sorted(deb_files ^ declared_archives)}")
    indexes = apt.get("indexes")
    if not isinstance(indexes, list) or not indexes:
        _fail("offline APT index lock metadata is missing")
    seen_indexes: set[str] = set()
    for row in indexes:
        if not isinstance(row, dict):
            _fail("offline APT index row is not an object")
        rel = safe_relative(row.get("path"), field="APT index")
        digest = str(row.get("sha256", "")).lower()
        size = row.get("size_bytes")
        if not rel.startswith("apt/lists/") or rel in seen_indexes or rel not in files:
            _fail(f"offline APT index lock is invalid: {rel}")
        if not HEX64.fullmatch(digest) or files[rel][1] != digest or size != files[rel][0]:
            _fail(f"offline APT index hash/size mismatch: {rel}")
        seen_indexes.add(rel)
    actual_indexes = {path for path in files if path.startswith("apt/lists/")}
    if actual_indexes != seen_indexes:
        _fail(f"offline APT index closure has undeclared/missing files: {sorted(actual_indexes ^ seen_indexes)}")

    package_indexes = apt.get("package_indexes")
    if not isinstance(package_indexes, list) or not package_indexes:
        _fail("offline APT Packages index closure metadata is missing")
    indexed_rows: dict[tuple[str, str, str], dict] = {}
    for index_row in package_indexes:
        if not isinstance(index_row, dict):
            _fail("offline APT Packages index row is invalid")
        index_path = safe_relative(index_row.get("path"), field="APT Packages index")
        if index_path not in seen_indexes:
            _fail(f"offline APT Packages index is not hash-covered: {index_path}")
        raw = (root / index_path).read_bytes()
        if index_path.lower().endswith(".gz"):
            raw = gzip.decompress(raw)
        elif index_path.lower().endswith(".bz2"):
            raw = bz2.decompress(raw)
        elif index_path.lower().endswith(".xz"):
            raw = lzma.decompress(raw)
        records = _parse_packages_index(raw, index_path)
        declared_index_rows = index_row.get("packages")
        if not isinstance(declared_index_rows, list) or not declared_index_rows:
            _fail(f"offline APT Packages index rows are missing: {index_path}")
        for expected in declared_index_rows:
            if not isinstance(expected, dict):
                _fail(f"offline APT Packages index package row is invalid: {index_path}")
            key = (str(expected.get("name")), str(expected.get("version")), str(expected.get("architecture") or "amd64"))
            matches = [record for record in records if (record.get("Package"), record.get("Version"), record.get("Architecture")) == key]
            if len(matches) != 1:
                _fail(f"offline APT Packages index package is missing/duplicated: {key[0]}")
            record = matches[0]
            archive = str(expected.get("archive") or package_rows.get(key, {}).get("archive") or "")
            if archive not in declared_archives or Path(archive).name != Path(str(record.get("Filename", ""))).name:
                _fail(f"offline APT Packages index Filename does not bind the .deb: {key[0]}")
            if str(record.get("Size", "")) != str(files[archive][0]) or str(record.get("SHA256", "")).lower() != files[archive][1]:
                _fail(f"offline APT Packages index size/SHA256 does not bind the .deb: {key[0]}")
            indexed_rows[key] = record
    if set(indexed_rows) != set(package_rows):
        _fail("offline APT Packages index closure has undeclared/missing packages")


def _verify_python(
    document: dict,
    files: dict[str, tuple[int, str]],
    handoff_root: Path | None,
    companion_root: Path,
    handoff: dict | None,
) -> None:
    policy = document.get("python")
    if not isinstance(policy, dict) or policy.get("wheelhouse") != "python-wheels" or policy.get("no_index") is not True or policy.get("require_hashes") is not True:
        _fail("offline Python wheelhouse contract is missing or network-enabled")
    pyproject_hash = str(policy.get("pyproject_sha256", "")).lower()
    if not HEX64.fullmatch(pyproject_hash):
        _fail("offline Python pyproject SHA256 is missing or invalid")
    if handoff is not None and handoff_root is not None:
        handoff_offline_pyproject = str(((handoff.get("offline_build") or {}).get("pyproject_sha256") or "")).lower()
        if handoff_offline_pyproject != pyproject_hash:
            _fail("offline Python pyproject SHA256 binding disagrees with the handoff")
        pyproject = handoff_root / "source" / "pyproject.toml"
        if pyproject.is_symlink() or not pyproject.is_file() or _hash_file(pyproject)[1] != pyproject_hash:
            _fail("offline Python pyproject SHA256 does not match the handoff source")
    wheel_paths = {
        rel: digest
        for rel, (_size, digest) in files.items()
        if rel.startswith("python-wheels/") and rel.lower().endswith(".whl")
    }
    wheel_hashes = set(wheel_paths.values())
    wheel_metadata: dict[tuple[str, str], tuple[str, str]] = {}

    def normalized_name(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).casefold()

    for rel, digest in wheel_paths.items():
        wheel = companion_root / rel
        try:
            with zipfile.ZipFile(wheel) as archive:
                metadata_names = [
                    name for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA") and name.count("/") == 1
                ]
                if len(metadata_names) != 1:
                    _fail(f"offline Python wheel metadata is missing or ambiguous: {rel}")
                metadata = archive.read(metadata_names[0]).decode("utf-8", "strict")
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
            _fail(f"offline Python wheel is not a valid wheel archive: {rel}: {exc}")
        fields: dict[str, str] = {}
        for line in metadata.splitlines():
            if ":" in line and not line[:1].isspace():
                key, value = line.split(":", 1)
                if key in {"Name", "Version"}:
                    fields[key] = value.strip()
        if not fields.get("Name") or not fields.get("Version"):
            _fail(f"offline Python wheel metadata lacks Name/Version: {rel}")
        identity = (normalized_name(fields["Name"]), fields["Version"])
        if identity in wheel_metadata:
            _fail(f"offline Python wheelhouse contains duplicate distribution/version: {identity[0]}=={identity[1]}")
        wheel_metadata[identity] = (rel, digest)
    lock_hashes: set[str] = set()
    lock_requirements: list[tuple[str, str, set[str], str]] = []
    for lock_key in ("build_requirements_lock", "runtime_requirements_lock"):
        lock_path = safe_relative(policy.get(lock_key), field=f"Python {lock_key}")
        if not lock_path.startswith("python/") or lock_path not in files:
            _fail(f"offline Python {lock_key} is missing from the verified companion")
        lock_file = companion_root / lock_path
        # Requirement locks are copied into the companion and consumed by the
        # Dockerfile with --require-hashes.  Every non-comment row therefore
        # needs an exact pinned version and at least one SHA256 hash.
        if lock_file.is_file():
            lock_text = lock_file.read_text(encoding="utf-8")
            if ("aoi" + "talk") in lock_text.casefold():
                _fail(f"offline Python {lock_key} contains an unbranded source package name")
            for line in lock_text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)", stripped)
                if match is None:
                    _fail(f"offline Python {lock_key} contains an unpinned row")
                hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group(3)))
                lock_hashes.update(hashes)
                lock_requirements.append((normalized_name(match.group(1)), match.group(2), hashes, lock_key))
    if not lock_hashes or not lock_hashes.issubset(wheel_hashes):
        _fail("offline Python requirement lock hashes do not match the verified wheelhouse")
    for name, version, hashes, lock_key in lock_requirements:
        wheel = wheel_metadata.get((name, version))
        if wheel is None or wheel[1] not in hashes:
            _fail(f"offline Python {lock_key} requirement has no matching wheel Name/Version/hash: {name}=={version}")
    rows = policy.get("wheels")
    if not isinstance(rows, list) or not rows:
        _fail("offline Python wheel hash lock is missing")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("offline Python wheel lock row is invalid")
        rel = safe_relative(row.get("path"), field="Python wheel")
        digest = str(row.get("sha256", "")).lower()
        if not rel.startswith("python-wheels/") or rel in seen or rel not in files or not rel.lower().endswith(".whl"):
            _fail(f"offline Python wheel lock path is invalid: {rel}")
        if not HEX64.fullmatch(digest) or files[rel][1] != digest:
            _fail(f"offline Python wheel hash mismatch: {rel}")
        seen.add(rel)
    wheel_files = set(wheel_paths)
    if wheel_files != seen:
        _fail("offline Python wheelhouse contains files not covered by the wheel lock")


def _verify_npm(
    document: dict,
    files: dict[str, tuple[int, str]],
    handoff_root: Path,
    handoff: dict,
    companion_root: Path,
) -> None:
    policy = document.get("npm")
    if not isinstance(policy, dict) or policy.get("cache") != "npm-cache" or policy.get("offline") is not True:
        _fail("offline npm cache contract is missing or network-enabled")
    lock = str(policy.get("package_lock_sha256", "")).lower()
    if not HEX64.fullmatch(lock):
        _fail("offline npm package-lock hash is missing or invalid")
    bound_lock = ((handoff.get("offline_build") or {}).get("npm_lock_sha256") or "").lower()
    if bound_lock != lock:
        _fail("offline npm package-lock SHA256 binding disagrees with the handoff")
    lock_path = handoff_root / "source" / "frontend" / "package-lock.json"
    if lock_path.is_symlink() or not lock_path.is_file():
        _fail("offline npm package-lock.json is missing or symlinked")
    if _hash_file(lock_path)[1] != lock:
        _fail("offline npm package-lock SHA256 does not match the handoff source")
    try:
        lock_document = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _fail(f"offline npm package-lock.json is invalid: {exc}")
    integrities: set[str] = set()
    def collect_integrity(value: object) -> None:
        if isinstance(value, dict):
            integrity = value.get("integrity")
            if isinstance(integrity, str) and integrity.startswith("sha512-"):
                try:
                    raw_digest = base64.b64decode(
                        integrity[7:] + "=" * (-len(integrity[7:]) % 4), validate=True
                    )
                    if len(raw_digest) != 64:
                        _fail("offline npm package-lock contains a non-SHA512 integrity")
                    integrities.add(raw_digest.hex())
                except Exception:
                    _fail("offline npm package-lock contains an invalid sha512 integrity")
            for child in value.values():
                collect_integrity(child)
        elif isinstance(value, list):
            for child in value:
                collect_integrity(child)
    collect_integrity(lock_document)
    for digest in integrities:
        candidate = f"npm-cache/_cacache/content-v2/sha512/{digest[:2]}/{digest[2:4]}/{digest[4:]}"
        if candidate not in files:
            _fail(f"offline npm cache is missing package integrity content: {candidate}")
        if hashlib.sha512((companion_root / candidate).read_bytes()).hexdigest() != digest:
            _fail(f"offline npm cache integrity content hash mismatch: {candidate}")
    # npm cache internals are content-addressed files.  Requiring at least one
    # lock-covered record prevents an empty cache from being accepted while
    # still allowing npm to add its normal _cacache metadata files.
    if not any(path.startswith("npm-cache/") for path in files):
        _fail("offline npm cache is empty")


def verify_companion(root: Path, handoff_manifest: Path, *, archive: Path | None = None) -> dict:
    """Verify a companion directory against its exact handoff binding.

    Returns the parsed offline manifest on success.  No files are modified.
    """

    if root.is_symlink() or not root.is_dir():
        _fail(f"offline input root is missing or symlinked: {root}")
    current = root.parent
    while current != current.parent:
        if current.is_symlink():
            _fail(f"offline input root path contains a symlink ancestor: {current}")
        current = current.parent
    if handoff_manifest.is_symlink() or not handoff_manifest.is_file():
        _fail(f"handoff manifest is missing or symlinked: {handoff_manifest}")
    current = handoff_manifest.parent
    while current != current.parent:
        if current.is_symlink():
            _fail(f"handoff manifest path contains a symlink ancestor: {current}")
        current = current.parent
    root = root.resolve()
    handoff_manifest = handoff_manifest.resolve()
    _assert_safe_tree(root)
    handoff = _load_json(handoff_manifest)
    manifest_candidates = [
        candidate
        for candidate in (root / "offline-build-manifest.json", root / "offline-input-manifest.json")
        if candidate.exists() or candidate.is_symlink()
    ]
    if len(manifest_candidates) != 1:
        _fail("offline companion must contain exactly one offline manifest")
    manifest_path = manifest_candidates[0]
    document = _load_json(manifest_path)
    source_commit = handoff.get("source_commit")
    if not isinstance(source_commit, str) or not SHA40.fullmatch(source_commit):
        _fail("handoff source_commit is invalid")
    offline = handoff.get("offline_build")
    if not isinstance(offline, dict) or offline.get("enabled") is not True:
        _fail("handoff offline_build is not enabled for the supplied input root")
    expected_format = str(offline.get("format") or "")
    if not expected_format.endswith(FORMAT_SUFFIX) or document.get("format") != expected_format or int(document.get("version", 0)) != VERSION:
        _fail("offline input manifest format/version is unsupported")
    if offline.get("source_commit") != source_commit or document.get("source_commit") != source_commit:
        _fail("offline input source_commit does not match the handoff")
    expected_handoff_binding = str(offline.get("handoff_binding_sha256") or "").lower()
    document_handoff_binding = str(document.get("handoff_binding_sha256") or "").lower()
    if not HEX64.fullmatch(expected_handoff_binding) or document_handoff_binding != expected_handoff_binding:
        _fail("offline handoff binding SHA256 is missing or inconsistent")
    if handoff_binding_digest(handoff) != expected_handoff_binding:
        _fail("offline handoff binding SHA256 does not match the immutable handoff contract")
    source_tree = handoff.get("source_tree")
    if not isinstance(source_tree, dict) or source_tree.get("path") != "source" or source_tree.get("commit") != source_commit:
        _fail("handoff source tree binding is missing")
    source_digest = str(offline.get("source_tree_sha256", "")).lower()
    document_source_digest = str(document.get("source_tree_sha256", "")).lower()
    if not HEX64.fullmatch(source_digest) or document_source_digest != source_digest:
        _fail("offline source tree SHA256 binding is missing or invalid")
    if source_tree_digest(handoff_manifest.parent / "source") != source_digest:
        _fail("offline source tree SHA256 does not match the handoff source")
    if document.get("platform") != "linux/amd64":
        _fail("offline input platform is not linux/amd64")
    expected_manifest_sha = str(offline.get("manifest_sha256") or "").lower()
    if not HEX64.fullmatch(expected_manifest_sha):
        _fail("offline input manifest SHA256 binding is missing")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_manifest_sha:
        _fail("offline input manifest SHA256 does not match the handoff")
    # An updater-activated companion lives under <install>/offline-inputs/<sha>
    # and has already had its archive hash checked.  Revalidation on later
    # starts may omit the original transport path; arbitrary external roots
    # still require the archive binding.
    updater_owned_root = root.parent.name == "offline-inputs"
    if str(offline.get("mode")) == "external-directory" and archive is None and not updater_owned_root:
        _fail("external offline input roots require the bound companion archive")
    if archive is not None:
        archive = archive.absolute()
        if archive.is_symlink() or not archive.is_file():
            _fail(f"offline companion archive is missing or symlinked: {archive}")
        current = archive.parent
        while current != current.parent:
            if current.is_symlink():
                _fail(f"offline companion archive path contains a symlink ancestor: {current}")
            current = current.parent
        if archive.name != str(offline.get("companion_archive") or ""):
            _fail("offline companion archive filename does not match the handoff")
        expected_archive_sha = str(offline.get("companion_sha256") or "").lower()
        if not HEX64.fullmatch(expected_archive_sha):
            _fail("offline companion archive SHA256 binding is missing")
        if _hash_file(archive)[1] != expected_archive_sha:
            _fail("offline companion archive SHA256 mismatch")

    files = _verify_file_coverage(root, document)
    _verify_image_pins(document, handoff, files, root)
    _verify_nodesource(document, handoff, files, root)
    _verify_apt(document, files, root, handoff)
    _verify_python(document, files, handoff_manifest.parent, root, handoff)
    _verify_npm(document, files, handoff_manifest.parent, handoff, root)
    if (handoff.get("offline_producer") or {}).get("version") == 1:
        import sys
        sys.dont_write_bytecode = True
        from produce_offline_inputs import verify_proof
        try:
            verify_proof(root, handoff, handoff_manifest.parent / "source")
        except ValueError as exc:
            _fail(str(exc))
    return document


def validate_payload(root: Path) -> dict:
    """Validate source-independent OCI/APT payload closure before packaging."""
    if root.is_symlink() or not root.is_dir():
        _fail(f"offline input root is missing or symlinked: {root}")
    _assert_safe_tree(root)
    candidates = [root / "offline-build-manifest.json", root / "offline-input-manifest.json"]
    manifests = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(manifests) != 1:
        _fail("offline input root must contain exactly one offline manifest")
    document = _load_json(manifests[0])
    files = _verify_file_coverage(root, document)
    synthetic_handoff = {
        "image_pins": [
            {"name": row.get("name"), "kind": "build-base" if str(row.get("name", "")).startswith("dockerfile-") else "dependency", "ref": row.get("ref")}
            for row in document.get("image_pins", [])
            if isinstance(row, dict)
        ],
        "build_reproducibility": {"nodesource_setup": document.get("nodesource", {})},
    }
    _verify_image_pins(document, synthetic_handoff, files, root)
    _verify_apt(document, files, root)
    _verify_python(document, files, None, root, None)
    return document


def _safe_zip_name(raw: str) -> tuple[str, bool]:
    normalized = raw.replace("\\", "/")
    is_dir = normalized.endswith("/")
    trimmed = normalized.rstrip("/")
    parts = trimmed.split("/") if trimmed else []
    if not parts or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized) or any(part in {"", ".", ".."} for part in parts):
        _fail(f"unsafe companion ZIP entry: {raw!r}")
    safe_relative("/".join(parts), field="ZIP path")
    return "/".join(parts), is_dir


def hydrate_archive(archive: Path, destination: Path) -> Path:
    """Atomically extract a companion ZIP into *destination*.

    The destination must not already exist; callers can choose a new sibling
    path for every update, preserving any prior offline input state.
    """

    archive = archive.absolute()
    if archive.is_symlink() or not archive.is_file():
        _fail(f"companion archive is missing or symlinked: {archive}")
    current = archive.parent
    while current != current.parent:
        if current.is_symlink():
            _fail(f"companion archive path contains a symlink ancestor: {current}")
        current = current.parent
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        _fail(f"hydration destination already exists: {destination}")
    current = destination.parent
    while current != current.parent:
        if current.is_symlink():
            _fail(f"hydration destination path contains a symlink ancestor: {current}")
        current = current.parent
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=str(destination.parent)))
    try:
        total = 0
        seen: set[str] = set()
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name, is_dir = _safe_zip_name(info.filename)
                key = name.casefold()
                if key in seen:
                    _fail(f"duplicate/case-colliding companion ZIP entry: {name}")
                seen.add(key)
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    _fail(f"companion ZIP entry is a symlink/special file: {name}")
                target = stage.joinpath(*name.split("/"))
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if is_dir:
                    target.mkdir(mode=0o700, exist_ok=True)
                    continue
                if target.exists() or target.is_symlink():
                    _fail(f"duplicate companion ZIP target: {name}")
                total += int(info.file_size)
                if total > MAX_UNCOMPRESSED_BYTES:
                    _fail("companion ZIP uncompressed size exceeds safety limit")
                with bundle.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                os.chmod(target, 0o600)
        _assert_safe_tree(stage)
        os.replace(stage, destination)
        os.chmod(destination, 0o700)
        return destination
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--handoff", required=True, type=Path)
    verify.add_argument("--archive", type=Path)
    hydrate = sub.add_parser("hydrate")
    hydrate.add_argument("--archive", required=True, type=Path)
    hydrate.add_argument("--destination", required=True, type=Path)
    payload = sub.add_parser("validate-payload")
    payload.add_argument("--root", required=True, type=Path)
    serve = sub.add_parser("serve")
    serve.add_argument("--root", required=True, type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=51181)
    serve.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify_companion(args.root, args.handoff, archive=args.archive)
            print("offline Enterprise inputs verified")
        elif args.command == "hydrate":
            print(hydrate_archive(args.archive, args.destination))
        elif args.command == "validate-payload":
            validate_payload(args.root)
            print("offline OCI/APT payload closure verified")
        else:
            serve_registry(args.root, args.port, args.host, args.token)
    except OfflineInputError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
