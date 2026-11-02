"""Stage fixed shared TypeScript sources beside the owned Mobile app workspace.

The Windows APK builder copies Mobile into ``<build-root>/m``. Preserve the
repository-relative shared imports by copying the canonical snapshot's entire
``shared`` source tree into ``<build-root>/shared``, never the launch checkout.
"""
from __future__ import annotations

import argparse
import shutil
import stat
from pathlib import Path


MAX_FILES = 128
MAX_TOTAL_BYTES = 4 * 1024 * 1024
ALLOWED_SUFFIXES = {".ts", ".tsx"}
REQUIRED_FILES = {"task-date-parser.ts"}


def _plain_path(path: Path) -> None:
    """Reject symlinks/reparse points on a path and every existing ancestor."""

    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Links/reparse points are not allowed in build inputs: {part}")


def _source_files(source_root: Path) -> list[Path]:
    files: list[Path] = []
    total = 0
    for path in sorted(source_root.rglob("*")):
        _plain_path(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Only regular shared source files are allowed")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"Unexpected shared source payload: {path.name}")
        files.append(path)
        total += path.stat().st_size
        if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("Shared source package exceeds its build payload limit")
    names = {path.relative_to(source_root).as_posix() for path in files}
    missing = sorted(REQUIRED_FILES - names)
    if missing:
        raise ValueError(f"Shared source package is missing required files: {', '.join(missing)}")
    return files


def stage(source_root: Path, build_root: Path) -> Path:
    """Copy canonical shared sources into an unused owned build destination."""

    source_root = source_root.absolute()
    build_root = build_root.absolute()
    _plain_path(source_root)
    _plain_path(build_root)
    if not source_root.is_dir() or not build_root.is_dir():
        raise ValueError("Shared source and owned build root must already exist")

    files = _source_files(source_root)
    destination = build_root / "shared"
    _plain_path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Shared staging destination already exists; refusing to overwrite")
    destination.mkdir()
    for path in files:
        target = destination / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = stage(args.source_root, args.build_root)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Shared source staging failed: {exc}\n")
    print(f"Staged fixed shared source package: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
