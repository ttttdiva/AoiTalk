"""Resolve a dedicated pytest directory without creating or removing it."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path


class PytestBasetempError(ValueError):
    """The dedicated temporary path cannot be safely resolved."""


def _check_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in (None, *path.parts[1:]):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PytestBasetempError("pytest_basetemp_uninspectable") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PytestBasetempError("pytest_basetemp_link_rejected")


def resolve_pytest_basetemp(*, purpose: str, identity: str, repository_root: Path) -> Path:
    if purpose not in {"qa", "approved"} or not isinstance(identity, str) or not identity:
        raise PytestBasetempError("pytest_basetemp_identity_invalid")
    repo = Path(repository_root)
    temp = Path(tempfile.gettempdir())
    for path in (repo, temp):
        if not path.is_absolute() or ".." in path.parts:
            raise PytestBasetempError("pytest_basetemp_path_invalid")
        _check_chain(path)
        try:
            if not path.is_dir() or path.resolve(strict=True) != path:
                raise PytestBasetempError("pytest_basetemp_directory_invalid")
        except OSError as exc:
            raise PytestBasetempError("pytest_basetemp_uninspectable") from exc
    if temp == repo or repo in temp.parents:
        raise PytestBasetempError("pytest_basetemp_temp_root_unsafe")
    digest = lambda value, size: hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]
    candidate = temp / f"pytest-muse-{purpose}-{digest(os.path.normcase(str(repo)), 16)}-{digest(identity, 24)}"
    if candidate == temp or candidate == repo or repo in candidate.parents or candidate in repo.parents:
        raise PytestBasetempError("pytest_basetemp_path_unsafe")
    _check_chain(candidate)
    if candidate.exists() and not candidate.is_dir():
        raise PytestBasetempError("pytest_basetemp_not_directory")
    return candidate
