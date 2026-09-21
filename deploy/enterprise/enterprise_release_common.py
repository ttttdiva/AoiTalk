"""Dependency-free file primitives for the Enterprise release interfaces."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

BLOCK = 1024 * 1024
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """An input failed the release contract; no fallback is permitted."""


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ContractError("missing or symlinked JSON input")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ContractError("JSON input must be an object")
    return value


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ContractError("invalid relative path")
    if "\\" in value or ":" in value or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ContractError("non-canonical relative path")
    return value


def no_links(path: Path) -> Path:
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ContractError("symlink in release path")
    return Path(os.path.abspath(path))


def regular_files(root: Path) -> list[Path]:
    no_links(root)
    if not root.is_dir():
        raise ContractError("release directory is missing")
    seen = set()
    result = []
    for path in root.rglob("*"):
        rel = relative_path(path.relative_to(root).as_posix())
        if rel.casefold() in seen:
            raise ContractError("case-colliding release path")
        seen.add(rel.casefold())
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            result.append(path)
        elif not stat.S_ISDIR(mode):
            raise ContractError("release tree contains a link or special file")
    return sorted(result, key=lambda p: (p.relative_to(root).as_posix().casefold(), p.relative_to(root).as_posix()))


def tree_digest(root: Path) -> str:
    records = [f"{hash_file(p)}  {p.relative_to(root).as_posix()}\n" for p in regular_files(root)]
    if not records:
        raise ContractError("empty source tree")
    return hashlib.sha256("".join(records).encode("utf-8")).hexdigest()


def binding_digest(document: dict) -> str:
    copy = dict(document)
    copy.pop("offline_build", None)
    copy.pop("generated_at_utc", None)
    return hashlib.sha256(json.dumps(copy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, document: dict) -> None:
    no_links(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def file_records(root: Path, exclude: set[str] | None = None) -> list[dict]:
    exclude = exclude or set()
    return [{"path": p.relative_to(root).as_posix(), "size_bytes": p.stat().st_size, "sha256": hash_file(p)}
            for p in regular_files(root) if p.relative_to(root).as_posix() not in exclude]


def verify_checksums(root: Path) -> None:
    actual = {row["path"]: row["sha256"] for row in file_records(root, {"SHA256SUMS"})}
    expected = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ContractError("malformed SHA256SUMS")
        rel = relative_path(match[2])
        if rel in expected or rel == "SHA256SUMS":
            raise ContractError("duplicate or self-referencing checksum")
        expected[rel] = match[1]
    if not actual or expected != actual:
        raise ContractError("handoff checksum or exact coverage mismatch")


def write_checksums(root: Path) -> None:
    rows = file_records(root, {"SHA256SUMS"})
    (root / "SHA256SUMS").write_text("".join(f"{r['sha256']}  {r['path']}\n" for r in rows), encoding="utf-8", newline="\n")


def write_zip(root: Path, destination: Path) -> None:
    # ZIP64 is mandatory for multi-GiB OCI layers. Never buffer those in RAM.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in regular_files(root):
            archive.write(path, path.relative_to(root).as_posix())
    # Windows FlushFileBuffers requires a write-capable handle.
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())


def publish_directory(source: Path, destination: Path) -> None:
    """Atomic no-replace publish; an output race must never erase an old tree."""
    no_links(source)
    no_links(destination)
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses an existing directory.
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ContractError("atomic no-replace publication requires Linux renameat2 or Windows")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    fsync_directory(destination.parent)
