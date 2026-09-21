"""Shared cross-process Skill target synchronization and atomic publication."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from .app_operation_lock import ACQUIRE_TIMEOUT_ENV_KEY, DEFAULT_ACQUIRE_TIMEOUT
from .app_storage import get_workspaces_root

_POLL_INTERVAL = 0.05


class SkillTargetIOError(RuntimeError):
    """Base failure for Skill target synchronization/publication."""


class SkillTargetLockTimeout(SkillTargetIOError):
    """The per-target cross-process lock could not be acquired in time."""


class SkillTargetTransitionPending(SkillTargetIOError):
    """A proposal file/DB transition has not yet been reconciled."""


class _SkillTargetAcquireCancelled(Exception):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _target_key(path: str | os.PathLike[str]) -> str:
    target = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(os.path.normpath(str(target)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _lock_root() -> Path:
    root = get_workspaces_root() / ".locks"
    if _is_link_or_reparse(root):
        raise SkillTargetIOError(
            "Skill lock directory cannot be a link/reparse point"
        )
    root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(root):
        raise SkillTargetIOError(
            "Skill lock directory cannot be a link/reparse point"
        )
    return root


def skill_target_lock_path(path: str | os.PathLike[str]) -> Path:
    return _lock_root() / f"skill_target_{_target_key(path)}.lock"


def skill_transition_journal_path(path: str | os.PathLike[str]) -> Path:
    return _lock_root() / f"skill_target_{_target_key(path)}.journal.json"


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        return float(timeout)
    raw = os.environ.get(ACQUIRE_TIMEOUT_ENV_KEY)
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = DEFAULT_ACQUIRE_TIMEOUT
        if parsed >= 0:
            return parsed
    return DEFAULT_ACQUIRE_TIMEOUT


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.normpath(str(path)))
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _is_contention(exc: OSError) -> bool:
    if os.name == "nt":
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            return winerror in {32, 33, 167}
        return exc.errno in {
            errno.EACCES,
            getattr(errno, "EDEADLK", errno.EACCES),
            getattr(errno, "EDEADLOCK", errno.EACCES),
            errno.EAGAIN,
            getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        }
    return isinstance(exc, BlockingIOError) or exc.errno in {
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }


class SkillTargetLock:
    """Synchronous process/thread lock shared by every Skill target writer."""

    def __init__(self, target_path: str | os.PathLike[str]) -> None:
        self.target_path = Path(target_path)
        self.path = skill_target_lock_path(self.target_path)
        self._local = _local_lock_for(self.path)
        self._handle: Any = None
        self._local_acquired = False
        self._cancel_event = threading.Event()

    def cancel_wait(self) -> None:
        self._cancel_event.set()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    def _acquire_local(self, deadline: float | None) -> None:
        while True:
            if self._cancel_event.is_set():
                raise _SkillTargetAcquireCancelled()
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise SkillTargetLockTimeout(
                    f"Skill target lock timed out: {self.target_path}"
                )
            wait_for = (
                _POLL_INTERVAL
                if remaining is None
                else min(_POLL_INTERVAL, max(remaining, 0.001))
            )
            if self._local.acquire(timeout=wait_for):
                return

    @staticmethod
    def _lock_handle(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, timeout: float | None = None) -> None:
        limit = _resolve_timeout(timeout)
        deadline = None if math.isinf(limit) else time.monotonic() + limit
        self._cancel_event.clear()
        self._acquire_local(deadline)
        self._local_acquired = True
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
            while True:
                if self._cancel_event.is_set():
                    raise _SkillTargetAcquireCancelled()
                try:
                    self._lock_handle(handle)
                    self._handle = handle
                    return
                except OSError as exc:
                    if not _is_contention(exc):
                        raise
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise SkillTargetLockTimeout(
                        f"Skill target lock timed out: {self.target_path}"
                    )
                self._cancel_event.wait(
                    _POLL_INTERVAL
                    if remaining is None
                    else min(_POLL_INTERVAL, max(remaining, 0.001))
                )
        except BaseException:
            if handle is not None:
                handle.close()
            if self._local_acquired:
                self._local.release()
                self._local_acquired = False
            raise

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        try:
            if handle is not None:
                try:
                    self._unlock_handle(handle)
                finally:
                    handle.close()
        finally:
            if self._local_acquired:
                self._local.release()
                self._local_acquired = False

    def __enter__(self) -> "SkillTargetLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def skill_target_lock(path: str | os.PathLike[str]) -> SkillTargetLock:
    return SkillTargetLock(path)


@asynccontextmanager
async def async_skill_target_lock(path: str | os.PathLike[str]):
    """Async wrapper around the same sync/OS lock used by direct writers."""
    lock = skill_target_lock(path)
    worker = asyncio.create_task(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        lock.cancel_wait()
        try:
            await asyncio.shield(worker)
        except (_SkillTargetAcquireCancelled, SkillTargetLockTimeout):
            pass
        else:
            await asyncio.to_thread(lock.release)
        raise
    try:
        yield lock
    finally:
        await asyncio.to_thread(lock.release)


def _fsync_directory(directory: Path) -> None:
    flags = getattr(os, "O_RDONLY", 0)
    try:
        fd = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_replace_text(path: str | os.PathLike[str], text: str) -> None:
    """Publish complete UTF-8 text by fsyncing a same-directory temp then replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.chmod(temp_path, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fd = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_remove(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(target.parent)


def read_target_text(path: str | os.PathLike[str]) -> Optional[str]:
    target = Path(path)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_target_hash(path: str | os.PathLike[str]) -> Optional[str]:
    text = read_target_text(path)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_skill_transition_journal(
    path: str | os.PathLike[str],
) -> Optional[dict[str, Any]]:
    journal_path = skill_transition_journal_path(path)
    try:
        raw = journal_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillTargetTransitionPending(
            f"Skill transition journal is unreadable: {Path(path)}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not str(payload.get("proposal_id") or "").strip()
        or payload.get("transition") not in {"apply", "rollback"}
    ):
        raise SkillTargetTransitionPending(
            f"Skill transition journal is invalid: {Path(path)}"
        )
    return payload


def assert_skill_target_stable(path: str | os.PathLike[str]) -> None:
    if read_skill_transition_journal(path) is not None:
        raise SkillTargetTransitionPending(
            f"Skill target has an unfinished transition: {Path(path)}"
        )


def read_stable_skill_text(path: str | os.PathLike[str]) -> Optional[str]:
    """Read an atomic target only if no transition exists before or after read."""
    assert_skill_target_stable(path)
    text = read_target_text(path)
    assert_skill_target_stable(path)
    return text


def write_skill_transition_journal(
    path: str | os.PathLike[str],
    *,
    proposal_id: str,
    transition: str,
    before_hash: Optional[str],
    after_hash: Optional[str],
) -> None:
    if transition not in {"apply", "rollback"}:
        raise ValueError("invalid Skill transition")
    payload = {
        "version": 1,
        "proposal_id": str(proposal_id),
        "transition": transition,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    atomic_replace_text(
        skill_transition_journal_path(path),
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )


def clear_skill_transition_journal(path: str | os.PathLike[str]) -> None:
    atomic_remove(skill_transition_journal_path(path))


def publish_skill_text(path: str | os.PathLike[str], text: str) -> None:
    """Direct writer publication: lock, reject in-flight proposal, atomic replace."""
    with skill_target_lock(path):
        assert_skill_target_stable(path)
        atomic_replace_text(path, text)


def delete_skill_text(path: str | os.PathLike[str]) -> bool:
    """Direct writer deletion under the same per-target lock."""
    with skill_target_lock(path):
        assert_skill_target_stable(path)
        target = Path(path)
        existed = target.exists()
        atomic_remove(target)
        return existed


__all__ = [
    "SkillTargetIOError",
    "SkillTargetLockTimeout",
    "SkillTargetTransitionPending",
    "async_skill_target_lock",
    "assert_skill_target_stable",
    "atomic_remove",
    "atomic_replace_text",
    "clear_skill_transition_journal",
    "delete_skill_text",
    "publish_skill_text",
    "read_stable_skill_text",
    "read_skill_transition_journal",
    "read_target_hash",
    "read_target_text",
    "skill_target_lock",
    "skill_target_lock_path",
    "skill_transition_journal_path",
    "write_skill_transition_journal",
]
