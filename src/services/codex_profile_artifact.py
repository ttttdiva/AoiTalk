"""Offline verification of the fixed official Windows package; no download."""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .codex_app_profiles import CodexProfileError, _directory

PACKAGE_NAME = "codex-package-x86_64-pc-windows-msvc.tar.gz"
PACKAGE_SHA256 = "7f7fbbc8d6fd4ea2f3b13855ef47ea59663ba7e61fb2e9821df37163b8030891"
CHECKSUMS_SHA256 = "82da8661fd9603329785350cce676e066d4b3419484c75d59314209e8edf8fcd"
EXE_SHA256 = "fdda5fa3cf3fb3d000b876720742857676293e4315e4b045fae6f8bd7e866d1d"
PACKAGE_BYTES, CHECKSUMS_BYTES = 157444460, 1631
RELEASE_URL = "https://github.com/openai/codex/releases/tag/rust-v0.160.0"
METADATA = {"layoutVersion": 1, "version": "0.160.0", "target": "x86_64-pc-windows-msvc",
            "variant": "codex", "entrypoint": "bin/codex.exe",
            "resourcesDir": "codex-resources", "pathDir": "codex-path"}
REQUIRED = frozenset({"codex-package.json", "bin/codex.exe", "bin/codex-code-mode-host.exe",
                     "codex-path/rg.exe", "codex-resources/codex-command-runner.exe",
                     "codex-resources/codex-windows-sandbox-setup.exe"})
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def member_path(name: str) -> str:
    name = name.removeprefix("./").rstrip("/")
    parts = PurePosixPath(name).parts
    if (not name or len(name) > 240 or "\\" in name or ":" in name or name.startswith("/")
            or any(x in {".", ".."} or x.endswith((".", " ")) or _RESERVED.match(x)
                   or any(ord(c) < 32 for c in x) for x in parts)):
        raise CodexProfileError("codex_profile_unsafe_package_member")
    if "/".join(parts) != name:
        raise CodexProfileError("codex_profile_unsafe_package_member")
    return name


def file_digest(path: Path) -> str:
    try:
        _directory(path.parent)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or int(getattr(before, "st_file_attributes", 0)) & 0x400:
            raise OSError()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError()
        return digest
    except (OSError, ValueError, RuntimeError):
        raise CodexProfileError("codex_profile_artifact_unverified") from None


@dataclass(frozen=True)
class CodexArtifact:
    # Local paths are trusted deployment dependencies, never request fields.
    package: Path
    checksums: Path

    def disclosure(self) -> dict:
        return {"release": RELEASE_URL, "package": PACKAGE_NAME, "sha256": PACKAGE_SHA256,
                "entrypoint": METADATA["entrypoint"], "exe_sha256": EXE_SHA256}

    def _snapshot(self) -> tuple[bytes, bytes]:
        def read(path, limit):
            _directory(path.parent)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                raise CodexProfileError("codex_profile_artifact_unverified")
            with path.open("rb") as source:
                value = source.read(limit + 1)
            if len(value) != limit:
                raise CodexProfileError("codex_profile_artifact_pin_mismatch")
            return value
        return read(self.package, PACKAGE_BYTES), read(self.checksums, CHECKSUMS_BYTES)

    def _inventory(self, package: bytes | None = None, checksums: bytes | None = None) -> dict[str, str]:
        if package is None or checksums is None:
            package, checksums = self._snapshot()
        if (self.package.name != PACKAGE_NAME or hashlib.sha256(package).hexdigest() != PACKAGE_SHA256
                or self.checksums.name != "codex-package_SHA256SUMS"
                or hashlib.sha256(checksums).hexdigest() != CHECKSUMS_SHA256):
            raise CodexProfileError("codex_profile_artifact_pin_mismatch")
        lines = checksums.decode("ascii").splitlines()
        if any(not re.fullmatch(r"[a-f0-9]{64}  [A-Za-z0-9_.-]+", line) for line in lines):
            raise CodexProfileError("codex_profile_artifact_pin_mismatch")
        entries = [line.split("  ") for line in lines]
        if len({name for _, name in entries}) != len(entries) or dict((name, sha) for sha, name in entries).get(PACKAGE_NAME) != PACKAGE_SHA256:
            raise CodexProfileError("codex_profile_artifact_pin_mismatch")
        inventory, names, total = {}, set(), 0
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= 4096 or member.size > 600 * 1024**2:
                    raise CodexProfileError("codex_profile_package_limits")
                name = member_path(member.name)
                if name.casefold() in names or not (member.isdir() or member.isfile()):
                    raise CodexProfileError("codex_profile_unsafe_package_member")
                names.add(name.casefold())
                if member.isdir():
                    continue
                total += member.size
                if total > 2 * 1024**3:
                    raise CodexProfileError("codex_profile_package_limits")
                stream = archive.extractfile(member)
                if stream is None:
                    raise CodexProfileError("codex_profile_unsafe_package_member")
                with stream:
                    inventory[name] = hashlib.file_digest(stream, "sha256").hexdigest()
                if name == "codex-package.json":
                    if member.size > 8192:
                        raise CodexProfileError("codex_profile_package_limits")
                    with archive.extractfile(member) as metadata:
                        if json.load(metadata) != METADATA:
                            raise CodexProfileError("codex_profile_package_layout_mismatch")
        if not REQUIRED <= inventory.keys() or inventory.get("bin/codex.exe") != EXE_SHA256:
            raise CodexProfileError("codex_profile_package_layout_mismatch")
        return inventory

    def prepare(self, destination: Path, storage) -> "CodexArtifactLease":
        # One bounded immutable snapshot is both hashed and consumed. Never
        # reopen an input path between pin verification and extraction.
        package, checksums = self._snapshot()
        inventory = self._inventory(package, checksums)
        if not destination.exists():
            storage.create_directory(destination)
            with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
                names, total = set(), 0
                for index, member in enumerate(archive):
                    name = member_path(member.name)
                    total += member.size
                    if (index >= 4096 or member.size > 600 * 1024**2 or total > 2 * 1024**3
                            or name.casefold() in names or not (member.isdir() or member.isfile())
                            or (member.isfile() and name not in inventory)):
                        raise CodexProfileError("codex_profile_unsafe_package_member")
                    names.add(name.casefold())
                    target = destination / name
                    for parent in reversed(target.parent.parents):
                        if destination in parent.parents or parent == destination:
                            if not parent.exists():
                                storage.create_directory(parent)
                    if not target.parent.exists():
                        storage.create_directory(target.parent)
                    if member.isdir():
                        if not target.exists():
                            storage.create_directory(target)
                        continue
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
        lease = CodexArtifactLease(destination, inventory, storage)
        lease.check()
        return lease


@dataclass
class CodexArtifactLease:
    root: Path
    inventory: dict[str, str]
    storage: object

    @property
    def executable(self) -> Path:
        return self.root / "bin" / "codex.exe"

    def check(self) -> None:
        self.storage.verify_directory(self.root)
        observed = set()
        for path in self.root.rglob("*"):
            info = path.lstat()
            if int(getattr(info, "st_file_attributes", 0)) & 0x400 or path.is_symlink():
                raise CodexProfileError("codex_profile_artifact_unverified")
            if path.is_dir():
                self.storage.verify_directory(path)
            elif path.is_file():
                name = path.relative_to(self.root).as_posix()
                if name not in self.inventory or file_digest(path) != self.inventory[name]:
                    raise CodexProfileError("codex_profile_artifact_changed")
                observed.add(name)
            else:
                raise CodexProfileError("codex_profile_artifact_unverified")
        if observed != self.inventory.keys() or not self.executable.is_absolute():
            raise CodexProfileError("codex_profile_artifact_changed")
