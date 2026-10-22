"""Stage the fixed playback package in the APK build's owned workspace.

The mobile package uses file:../resources/seethrough-rig/runtime.  Its build
copy lives in <build-root>/m so this dependency never resolves into shared TEMP.
No authoring checkout, user model, or inference environment is consulted.
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
from pathlib import Path

PACKAGE_NAME = "@aoitalk/seethrough-rig-runtime"
MAX_FILES = 128
MAX_TOTAL_BYTES = 8 * 1024 * 1024
ALLOWED_SUFFIXES = {".js", ".mjs", ".json", ".ts", ".md", ".txt"}


def _plain_path(path: Path) -> None:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Links/reparse points are not allowed in build inputs: {part}")


def stage(source_root: Path, build_root: Path) -> Path:
    source_root = source_root.absolute()
    build_root = build_root.absolute()
    _plain_path(source_root)
    _plain_path(build_root)
    if not source_root.is_dir() or not build_root.is_dir():
        raise ValueError("Runtime source and owned build root must already exist")
    mobile = build_root / "m"
    _plain_path(mobile)
    manifest_path = mobile / "package.json"
    _plain_path(manifest_path)
    mobile_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dependency = mobile_manifest.get("dependencies", {}).get(PACKAGE_NAME)
    if dependency != "file:../resources/seethrough-rig/runtime":
        raise ValueError("Unexpected mobile runtime dependency; refusing alternate staging paths")
    package_path = source_root / "package.json"
    _plain_path(package_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("name") != PACKAGE_NAME or package.get("version") not in {"0.1.0", "0.2.0", "0.3.0", "0.4.0"}:
        raise ValueError("Unexpected runtime identity/version")
    files: list[Path] = []
    total = 0
    for path in source_root.rglob("*"):
        _plain_path(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Only regular runtime files are allowed")
        if path.suffix.lower() not in ALLOWED_SUFFIXES and path.name not in {"LICENSE", "NOTICE"}:
            raise ValueError(f"Unexpected runtime payload: {path.name}")
        files.append(path)
        total += path.stat().st_size
        if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("Playback package exceeds its build payload limit")
    if not files:
        raise ValueError("Runtime package is empty")
    destination = build_root / "resources" / "seethrough-rig" / "runtime"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Runtime staging destination already exists; refusing to overwrite")
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
        parser.exit(1, f"Runtime staging failed: {exc}\n")
    print(f"Staged fixed playback package: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
