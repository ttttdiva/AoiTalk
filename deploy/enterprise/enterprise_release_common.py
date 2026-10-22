"""Dependency-free file primitives for the Enterprise release interfaces."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import stat
import shutil
import subprocess
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


def _publish_windows_drive_from_wsl(source: Path, destination: Path) -> bool:
    """Use Windows' no-replace directory move where DrvFs lacks renameat2 flags."""
    if platform.system() != "Linux" or "microsoft" not in platform.release().casefold():
        return False
    converter, powershell = shutil.which("wslpath"), shutil.which("powershell.exe")
    if not converter or not powershell:
        return False
    paths = []
    for path in (source, destination):
        converted = subprocess.run(
            [converter, "-w", str(path)], capture_output=True, text=True,
            encoding="utf-8", errors="strict", check=False, timeout=30,
        )
        value = converted.stdout.removesuffix("\n").removesuffix("\r")
        windows = PureWindowsPath(value)
        if (converted.returncode or not re.fullmatch(r"[A-Za-z]:", windows.drive)
                or not windows.is_absolute() or any(c in value for c in "\0\r\n")
                or any(part.endswith((" ", ".")) or ":" in part for part in windows.parts[1:])):
            return False
        paths.append(value)
    if PureWindowsPath(paths[0]).drive.casefold() != PureWindowsPath(paths[1]).drive.casefold():
        return False
    # Literal quoting keeps spaces, Unicode, apostrophes and shell metacharacters
    # in paths as data. Do not alter PowerShell's execution policy or WSL settings.
    literals = ["'" + value.replace("'", "''") + "'" for value in paths]
    command = "$ErrorActionPreference = 'Stop'; [System.IO.Directory]::Move(" + ", ".join(literals) + ")"
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        if destination.exists():
            raise FileExistsError(errno.EEXIST, "release destination already exists", str(destination))
        raise OSError(errno.EIO, "Windows no-replace directory publication failed", str(destination))
    return True


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
            unsupported = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}
            if error not in unsupported or not _publish_windows_drive_from_wsl(source, destination):
                raise OSError(error, os.strerror(error), str(destination))
    fsync_directory(destination.parent)


def preflight_publication(parent: Path) -> None:
    """Check the output filesystem before any large acquisition or packaging."""
    parent = no_links(parent)
    with tempfile.TemporaryDirectory(prefix=".enterprise-publish-probe-", dir=parent) as raw:
        root = Path(raw)
        source, target = root / "source", root / "target"
        source.mkdir()
        (source / "marker").write_bytes(b"publication-probe")
        publish_directory(source, target)
        if source.exists() or (target / "marker").read_bytes() != b"publication-probe":
            raise ContractError("directory publication did not preserve the source")
        source.mkdir()
        (source / "new").write_bytes(b"must-not-replace")
        try:
            publish_directory(source, target)
        except FileExistsError:
            pass
        else:
            raise ContractError("directory publication did not refuse an existing target")
        if (target / "marker").read_bytes() != b"publication-probe" or not (source / "new").is_file():
            raise ContractError("directory publication changed an existing target")
