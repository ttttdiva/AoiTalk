"""Bounded non-following source scans; incomplete enumeration never means deletion."""
from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..services.storage_io import StorageError, StorageUnavailable, check_io_cancelled, storage_io
from ..services.storage_roots import assert_no_links, is_link, lexical_path, read_registry


@dataclass(frozen=True)
class SourceScan:
    root: Path
    paths: tuple[Path, ...]
    complete: bool
    errors: tuple[str, ...]


def source_io_key(root_path: Path | str) -> str:
    root = lexical_path(root_path)
    mount = read_registry().containing(root)
    return mount.io_key if mount else f"knowledge:{os.path.normcase(str(root))}"


def require_source_online(root_path: Path | str) -> Path:
    root = lexical_path(root_path)
    # Registry identity is additive: existing unregistered KnowledgeSource
    # roots remain valid. A configured root never degrades to a local fallback.
    mount = read_registry().containing(root)
    if mount:
        mount.require_online()
    try:
        assert_no_links(root)
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise StorageUnavailable("Knowledge Sourceがディレクトリではありません")
        return root
    except OSError as exc:
        raise StorageUnavailable("Knowledge Sourceのストレージが利用できません") from exc


async def verify_source_online(root_path: Path | str) -> None:
    await storage_io.run(source_io_key(root_path), lambda: require_source_online(root_path), timeout=3)


async def scan_source(root_path: Path | str, include: list[str], exclude: list[str],
                      max_files: int, matches: Callable[[Path, Path, list[str], list[str]], bool]) -> SourceScan:
    def scan():
        root = require_source_online(root_path)
        candidates: list[Path] = []
        visited = 0
        complete = True
        errors: list[str] = []
        def onerror(error: OSError):
            raise error
        try:
            for directory, directories, names in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
                check_io_cancelled()
                current = Path(directory)
                assert_no_links(current)
                safe = []
                for name in directories:
                    child = current / name
                    rel = child.relative_to(root).as_posix()
                    if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern) for pattern in exclude):
                        continue
                    if not is_link(child.lstat()):
                        assert_no_links(child)
                        safe.append(name)
                directories[:] = safe
                for name in names:
                    visited += 1
                    if visited > max(10000, min(1_000_000, max_files * 100)):
                        complete = False
                        errors.append("Source scan incomplete: enumeration limit reached; existing documents preserved")
                        break
                    path = current / name
                    info = path.lstat()
                    if is_link(info) or not stat.S_ISREG(info.st_mode):
                        continue
                    if not matches(path, root, include, exclude):
                        continue
                    if len(candidates) >= max_files:
                        complete = False
                        errors.append("Source scan incomplete: max_files reached; unvisited documents preserved")
                        break
                    candidates.append(path)
                if not complete:
                    break
            require_source_online(root)
        except (OSError, StorageError) as exc:
            # Preserve visited and unvisited records. Do not use an empty
            # directory listing after a failed network filesystem syscall.
            complete = False
            errors.append("Knowledge Source scan unavailable; existing documents preserved")
        return SourceScan(root, tuple(sorted(candidates, key=lambda p: str(p).lower())), complete, tuple(errors))
    return await storage_io.run(source_io_key(root_path), scan, timeout=15)


async def read_source_file(root_path: Path | str, path: Path,
                           reader: Callable[[Path], tuple[str, str | None]]):
    def read():
        root = require_source_online(root_path)
        lexical = lexical_path(path)
        try:
            lexical.relative_to(root)
        except ValueError as exc:
            raise StorageError("unsafe_source_path", "Knowledge Sourceのroot外です", 403) from exc
        assert_no_links(lexical)
        before = lexical.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise StorageError("unsafe_source_path", "通常のファイルではありません", 403)
        text, error = reader(lexical)
        require_source_online(root)
        assert_no_links(lexical)
        after = lexical.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise StorageUnavailable("読み込み中に資料が更新されました。再同期してください")
        check_io_cancelled()
        return text, error, after
    try:
        return await storage_io.run(source_io_key(root_path), read, timeout=15)
    except FileNotFoundError as exc:
        await verify_source_online(root_path)
        raise StorageError("knowledge_document_missing", "資料の正本ファイルが見つかりません", 404) from exc
    except OSError as exc:
        raise StorageUnavailable("Knowledge Sourceのファイルを読み込めません") from exc
