"""Bounded, deterministic code workspace archive I/O.

Callers must supply a workspace already authorized by their domain. This module
does not resolve grants, artifact ownership, or provenance.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from ..security.sensitive_files import is_sensitive_relative_path

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_ARTIFACT_FILES = 10_000

_EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".aoitalk", ".next", ".nuxt", ".turbo", ".cache", ".parcel-cache", ".verify",
}


def _is_excluded_dir_name(name: str) -> bool:
    return name in _EXCLUDED_DIRS or name.startswith(".aoitalk-harness-scratch-")


def _path_has_excluded_component(path: Path) -> bool:
    return any(_is_excluded_dir_name(part) for part in path.parts)
_BUILD_DIRS = ("dist", "build", "out")

class ArtifactArchiveTooLarge(ValueError):
    """The bounded artifact archive cannot be materialized safely."""


class ArtifactArchiveUnsafe(ValueError):
    """The archive contains a file that cannot cross the publication boundary."""



def _iter_files(base: Path, *, exclude_build: bool = False):
    for current, dirs, files in os.walk(base):
        rel_dir = Path(current).relative_to(base)
        dirs[:] = sorted(
            d for d in dirs
            if not _is_excluded_dir_name(d)
            and not (exclude_build and rel_dir == Path(".") and d in _BUILD_DIRS)
            and not Path(current, d).is_symlink()
            and not os.path.isjunction(Path(current, d))
        )
        for name in sorted(files):
            path = Path(current, name)
            if path.is_symlink():
                continue
            yield path, path.relative_to(base)



def zip_tree(base: Path, *, exclude_build: bool) -> tuple[bytes, list[str], list[str]]:
    buffer = io.BytesIO()
    included: list[str] = []
    excluded_sensitive: list[str] = []
    source_bytes = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, rel in _iter_files(base, exclude_build=exclude_build):
            if is_sensitive_relative_path(str(rel)):
                excluded_sensitive.append(rel.as_posix())
                continue
            try:
                stat = path.stat()
                file_size = stat.st_size
            except OSError:
                continue
            if file_size < 0 or stat.st_nlink > 1:
                raise ArtifactArchiveUnsafe(
                    f"unsafe hardlink in artifact source: {rel.as_posix()}"
                )
            if source_bytes + file_size > MAX_ARTIFACT_BYTES:
                raise ArtifactArchiveTooLarge(
                    f"artifact source exceeds {MAX_ARTIFACT_BYTES} bytes"
                )
            if len(included) >= MAX_ARTIFACT_FILES:
                raise ArtifactArchiveTooLarge(
                    f"artifact archive exceeds {MAX_ARTIFACT_FILES} files"
                )
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            try:
                with path.open("rb") as handle:
                    content = handle.read(MAX_ARTIFACT_BYTES - source_bytes + 1)
            except OSError:
                continue
            if len(content) > MAX_ARTIFACT_BYTES - source_bytes:
                raise ArtifactArchiveTooLarge(
                    f"artifact source exceeds {MAX_ARTIFACT_BYTES} bytes"
                )
            if len(content) != file_size:
                # A concurrent truncation is bounded, but the archive must
                # never silently claim a stale stat as evidence.
                raise ArtifactArchiveUnsafe(
                    f"artifact file changed while reading: {rel.as_posix()}"
                )
            archive.writestr(info, content)
            source_bytes += len(content)
            included.append(rel.as_posix())
            if buffer.tell() > MAX_ARTIFACT_BYTES:
                raise ArtifactArchiveTooLarge(
                    f"artifact archive exceeds {MAX_ARTIFACT_BYTES} bytes"
                )
    archive_bytes = buffer.getvalue()
    if len(archive_bytes) > MAX_ARTIFACT_BYTES:
        raise ArtifactArchiveTooLarge(f"artifact archive exceeds {MAX_ARTIFACT_BYTES} bytes")
    return archive_bytes, included, excluded_sensitive
