"""Stage the fixed chat-background source into an owned APK workspace.

Native Mobile sources are copied into ``<build-root>/m`` while shared
resources live beside that directory.  The APK build therefore stages the
tracked ``resources/chat-background`` package into
``<build-root>/resources/chat-background`` before Expo resolves relative
imports.  The source is intentionally a small TypeScript-only package: no
authoring checkout, generated output, links, or user media are accepted.
"""
from __future__ import annotations

import argparse
import shutil
import stat
from pathlib import Path


MAX_FILES = 32
MAX_TOTAL_BYTES = 1 * 1024 * 1024
ALLOWED_SUFFIXES = {".ts"}
REQUIRED_FILES = {"index.ts"}


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
            raise ValueError("Only regular chat-background files are allowed")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"Unexpected chat-background payload: {path.name}")
        files.append(path)
        total += path.stat().st_size
        if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("Chat-background package exceeds its build payload limit")
    if not files:
        raise ValueError("Chat-background package is empty")
    names = {path.name for path in files}
    missing = sorted(REQUIRED_FILES - names)
    if missing:
        raise ValueError(f"Chat-background package is missing required files: {', '.join(missing)}")
    return files


def stage(source_root: Path, build_root: Path) -> Path:
    """Copy ``source_root`` to the owned build root and return its destination."""

    source_root = source_root.absolute()
    build_root = build_root.absolute()
    _plain_path(source_root)
    _plain_path(build_root)
    if not source_root.is_dir() or not build_root.is_dir():
        raise ValueError("Chat-background source and owned build root must already exist")

    files = _source_files(source_root)
    destination = build_root / "resources" / "chat-background"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Chat-background staging destination already exists; refusing to overwrite")
    for parent in reversed(destination.parents):
        if parent.exists():
            _plain_path(parent)

    destination.parent.mkdir(parents=True, exist_ok=True)
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
        parser.exit(1, f"Chat-background staging failed: {exc}\n")
    print(f"Staged fixed chat-background package: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
