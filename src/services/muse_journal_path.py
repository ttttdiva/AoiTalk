"""Private path and readiness checks for the server-owned Muse journal."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import hashlib
import os
import sqlite3
import stat
import sys
from pathlib import Path


class MuseJournalUnavailable(ValueError):
    """The journal requires provisioning or migration before use."""


def _reject_links(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        try:
            st = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")


def _muse_journal_path(workspace_root: Path, *, platform: str | None = None,
                       environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Resolve without creating files; optional inputs are deterministic test seams."""
    if not isinstance(workspace_root, Path) or not workspace_root.is_absolute() or ".." in workspace_root.parts:
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    env = os.environ if environ is None else environ
    system = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else home
    if system == "win32":
        base = Path(env.get("LOCALAPPDATA", ""))
    elif system == "darwin":
        base = user_home / "Library" / "Application Support"
    else:
        value = env.get("XDG_STATE_HOME", "")
        base = Path(value) if value else user_home / ".local" / "state"
    if not base.is_absolute() or ".." in base.parts:
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    _reject_links(base)
    if base.is_relative_to(root) or root.is_relative_to(base):
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    # normcase makes Windows spelling/case aliases share the same workspace key.
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    target = base / "AoiTalk" / "state" / "muse" / key / "muse-tool-loop.sqlite3"
    _reject_links(target)
    if target.is_relative_to(root) or root.is_relative_to(target.parent):
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    return target



def _validate_journal_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        try:
            sidecar_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (not path.is_file() or not stat.S_ISREG(sidecar_stat.st_mode)
                or getattr(sidecar_stat, "st_file_attributes", 0) & 0x400):
            raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")


def _ready_muse_journal(
    workspace_root: Path, *, managed_repository_root: Path | None = None,
) -> Path:
    """Require an existing trusted parent and, for legacy data, a migrated DB.

    This does not open the legacy journal or create the target. Migration and
    provisioning are separate operational steps.
    """
    if managed_repository_root is not None:
        for root in (workspace_root, managed_repository_root):
            if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
                raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
            _reject_links(root)
        workspace = workspace_root.resolve(strict=True)
        managed = managed_repository_root.resolve(strict=True)
        if not workspace.is_dir() or not managed.is_dir():
            raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
        if os.path.normcase(str(workspace)) != os.path.normcase(str(managed)):
            path = workspace / ".local" / "muse-tool-loop.sqlite3"
            _reject_links(path)
            if not path.parent.is_dir() or path.parent.resolve(strict=True) != path.parent:
                raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(path_stat.st_mode):
                    raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
            _validate_journal_sidecars(path)
            return path
    path = _muse_journal_path(workspace_root)
    if not path.parent.is_dir() or path.parent.resolve(strict=True) != path.parent:
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    if path.exists() and not path.is_file():
        raise MuseJournalUnavailable("muse_tool_loop_journal_unavailable")
    legacy = workspace_root / ".local" / "muse-tool-loop.sqlite3"
    _reject_links(legacy.parent)
    try:
        legacy.lstat()
    except FileNotFoundError:
        _validate_journal_sidecars(path)
        return path
    if not path.is_file() or path.stat().st_size == 0:
        raise MuseJournalUnavailable("muse_tool_loop_journal_migration_required")
    try:
        _validate_journal_sidecars(path)
        # The target is a live database: honor SQLite locking and WAL changes.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"muse_tool_receipts", "muse_tool_loop_state"} <= tables:
                raise ValueError("missing journal schema")
            required = {
                "muse_tool_receipts": {"operation_id", "schema_version", "owner_id", "session_id", "run_id", "tool", "intent_digest", "authority_revision", "state", "status", "claim_token", "reason", "result_json", "created_at", "updated_at"},
                "muse_tool_loop_state": {"scope_key", "schema_version", "version", "state_json", "updated_at"},
            }
            for table, columns in required.items():
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise ValueError("invalid journal schema")
    except (sqlite3.Error, OSError, ValueError):
        raise MuseJournalUnavailable("muse_tool_loop_journal_migration_required") from None
    return path
