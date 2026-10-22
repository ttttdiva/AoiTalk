"""Offline, hash-checked recipient notices for the portable executable.

The manifest is a packaging inventory, not a signature or a legal opinion.
Only explicitly reviewed text/source files are included; no directory is copied.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

DESTINATION = "resources/pc-bridge-notices"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{x}{n}" for x in ("COM", "LPT") for n in range(1, 10))}


class LicenseBundleError(ValueError):
    """The provided notices are absent, incomplete, or do not match the manifest."""


def default_root() -> Path:
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return base / DESTINATION


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise LicenseBundleError("Invalid license path")
    parts = value.split("/")
    if any(not _PART.fullmatch(p) or p in (".", "..") or p.endswith((".", " "))
           or p.split(".")[0].upper() in _RESERVED for p in parts):
        raise LicenseBundleError("Invalid license path")
    if value.casefold() == "manifest.json":
        raise LicenseBundleError("Reserved license path")
    return value


def _read(path: Path, maximum: int) -> bytes:
    try:
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
               for p in (path, *path.parents)) or not path.is_file():
            raise LicenseBundleError("Linked or missing license file")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if not data or len(data) > maximum:
            raise LicenseBundleError("Invalid license file size")
        return data
    except OSError as exc:
        raise LicenseBundleError("Cannot read license file") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LicenseBundleError("Duplicate manifest field")
        result[key] = value
    return result


def load_bundle(root: Path | None = None, *, require_complete: bool = False):
    """Return the validated manifest and exact original bytes, without side effects."""
    root = (root if root is not None else default_root()).absolute()
    manifest_bytes = _read(root / "manifest.json", MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise LicenseBundleError("Invalid license manifest") from exc
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise LicenseBundleError("Unsupported license manifest")
    if manifest.get("product") != "AoiTalk-PC-Bridge":
        raise LicenseBundleError("Wrong license product")
    if require_complete and manifest.get("review_status") != "complete":
        raise LicenseBundleError("Distribution component review is incomplete")
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise LicenseBundleError("Invalid license inventory")
    result, seen, total = {}, set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes", "kind"}:
            raise LicenseBundleError("Invalid license inventory entry")
        relative = _relative(item["path"])
        if relative.casefold() in seen or item["kind"] not in ("notice", "source", "inventory"):
            raise LicenseBundleError("Duplicate or invalid license entry")
        seen.add(relative.casefold())
        size, sha = item["size_bytes"], item["sha256"]
        if type(size) is not int or not 1 <= size <= MAX_FILE_BYTES or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise LicenseBundleError("Invalid license hash or size")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise LicenseBundleError("License bundle is too large")
        data = _read(root / relative, size)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise LicenseBundleError("License file does not match manifest: " + relative)
        result[relative] = data
    if not any(item["kind"] == "notice" for item in files):
        raise LicenseBundleError("No recipient notice")
    inventory = manifest.get("component_inventory")
    if require_complete and (not isinstance(inventory, str) or inventory not in result or not any(
            item["path"] == inventory and item["kind"] == "inventory" for item in files)):
        raise LicenseBundleError("Reviewed component inventory is missing")
    result["manifest.json"] = manifest_bytes
    return manifest, result


def notices_text(root: Path | None = None) -> str:
    manifest, files = load_bundle(root)
    parts = ["AoiTalk PC Bridge: third-party licenses and source availability\n"]
    for item in manifest["files"]:
        if item["kind"] == "notice":
            data = files[item["path"]]
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                # Some upstream notices (including rav1e's PATENTS) retain
                # Windows-1252 punctuation. Export always keeps the raw bytes.
                text = data.decode("cp1252", errors="replace")
            parts.append("\n===== " + item["path"] + " =====\n" + text)
    return "\n".join(parts)


def export_notices(destination: Path, root: Path | None = None) -> Path:
    """Create a new ZIP only. Never overwrite a recipient's existing file."""
    _, files = load_bundle(root)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sorted(files.items()):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    destination = destination.absolute()
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
           for p in (destination, *destination.parents)):
        raise LicenseBundleError("Linked export destination is not allowed")
    with destination.open("xb") as stream:
        stream.write(output.getvalue())
    return destination


def build_arguments(root: Path, *, require_complete: bool = False) -> list[str]:
    _, files = load_bundle(root, require_complete=require_complete)
    arguments = []
    for relative in sorted(files):
        parent = PurePosixPath(relative).parent
        destination = DESTINATION if str(parent) == "." else DESTINATION + "/" + str(parent)
        arguments.extend(("--add-data", str(root.absolute() / relative) + ";" + destination))
    return arguments
