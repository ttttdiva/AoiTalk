"""Small admission fence for in-flight work while an admin restart is prepared."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any


class AdminRestartPendingError(ValueError):
    """Safe request rejection while a process restart is being prepared."""

    def __init__(self) -> None:
        super().__init__("サーバー再起動の準備中のため、会話を開始できません。")


_initialization_lock = threading.Lock()


def _admission_state(server: Any) -> tuple[threading.RLock, dict[asyncio.Task[Any], int]]:
    with _initialization_lock:
        lock = getattr(server, "_admin_restart_admission_lock", None)
        if lock is None:
            lock = threading.RLock()
            server._admin_restart_admission_lock = lock
        counts = getattr(server, "_admin_restart_chat_requests", None)
        if not isinstance(counts, dict):
            counts = {}
            server._admin_restart_chat_requests = counts
        if not hasattr(server, "_admin_restart_pending"):
            server._admin_restart_pending = False
        return lock, counts


def reserve_admin_restart(server: Any) -> bool:
    lock, _ = _admission_state(server)
    with lock:
        if server._admin_restart_pending:
            return False
        server._admin_restart_pending = True
        return True


def release_admin_restart(server: Any) -> None:
    lock, _ = _admission_state(server)
    with lock:
        server._admin_restart_pending = False


def begin_chat_request(server: Any) -> asyncio.Task[Any]:
    lock, counts = _admission_state(server)
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Chat request has no asyncio task")
    with lock:
        if server._admin_restart_pending:
            raise AdminRestartPendingError()
        counts[task] = counts.get(task, 0) + 1
    return task


def finish_chat_request(server: Any, task: asyncio.Task[Any]) -> None:
    lock, counts = _admission_state(server)
    with lock:
        count = counts.get(task, 0)
        if count <= 1:
            counts.pop(task, None)
        else:
            counts[task] = count - 1


def active_chat_requests(server: Any) -> tuple[asyncio.Task[Any], ...]:
    lock, counts = _admission_state(server)
    with lock:
        return tuple(task for task in counts if not task.done())


@asynccontextmanager
async def conversation_work_lease(server: Any) -> AsyncIterator[None]:
    """Count one side-effecting Chat/Agent continuation for its full span."""

    task = begin_chat_request(server)
    try:
        yield None
    finally:
        finish_chat_request(server, task)
