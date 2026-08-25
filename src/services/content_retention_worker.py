"""Bounded, cancellable retention housekeeping for first-party content.

The Next.js application owns conversation/Docs cleanup.  This worker only
handles the Python-side file trash and an optional canonical task-purge hook,
when one is provided by the task lifecycle implementation.  Keeping the hook
optional lets rolling deployments start safely before the task module exposes
its purge helper.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any, Callable, Mapping

from ..tools.file_explorer.file_explorer_service import (
    get_trash_retention_days,
    purge_trash,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_RETENTION_SWEEP_BATCH_SIZE = 100
MAX_RETENTION_SWEEP_BATCH_SIZE = 10_000

# Keep this list deliberately narrow.  It is a compatibility bridge for the
# task worker being introduced alongside this worker, not a second task
# deletion implementation and not a discovery mechanism for arbitrary code.
_TASK_PURGE_HOOK_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "src.services.task_management.tasks",
        ("purge_expired_tasks", "purge_deleted_tasks"),
    ),
    (
        "src.services.task_management_service",
        ("purge_expired_tasks", "purge_deleted_tasks"),
    ),
    (
        "src.services.task_retention",
        ("purge_expired_tasks", "purge_deleted_tasks"),
    ),
)


def _read_positive_int(
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    """Read a bounded positive integer environment setting safely."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return default
    if value <= 0 or value > maximum:
        return default
    return value


def _supported_kwargs(
    function: Callable[..., Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only keyword arguments accepted by a hook/callable.

    The optional task helper is intentionally allowed to evolve from a
    no-argument function to one accepting ``retention_days`` and
    ``batch_size``.  Inspecting the signature avoids turning a helper's
    unrelated TypeError into a duplicate invocation.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return {}

    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return dict(values)
    return {key: value for key, value in values.items() if key in parameters}


def resolve_task_purge_hook() -> Callable[..., Any] | None:
    """Find the canonical Python task purge helper, if the app exposes one."""

    for module_name, attribute_names in _TASK_PURGE_HOOK_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        for attribute_name in attribute_names:
            hook = getattr(module, attribute_name, None)
            if callable(hook):
                return hook
    # The canonical purge implementation is a service mixin method, so expose
    # a small database-lifecycle adapter rather than attempting to duplicate
    # its SQL here.  This keeps the daily worker effective in the normal
    # FastAPI process while remaining import-safe for lightweight tests.
    return purge_expired_tasks


async def purge_expired_tasks(
    *, batch_size: int = DEFAULT_RETENTION_SWEEP_BATCH_SIZE,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Invoke the canonical TaskManagementService purge once.

    Chat/Docs remain owned by Next.js; this adapter only opens a Python DB
    session and delegates Task row cleanup to the service method.
    """

    from ..memory.database import get_database_manager
    from .task_management_service import TaskManagementService

    manager = get_database_manager()
    session = await manager.get_session()
    try:
        service = TaskManagementService()
        result = await service.purge_expired_task_deletions(
            session,
            retention_days=retention_days,
            limit=batch_size,
        )
        return result
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


class ContentRetentionWorker:
    """Run file-trash/task retention once per day until shutdown.

    ``start`` and ``stop`` are lifecycle hooks suitable for
    ``WebChatServer._startup_background_tasks`` and
    ``_shutdown_background_tasks``.  Every sweep is bounded by
    ``batch_size``; cancellation is awaited so the server never leaves an
    untracked housekeeping task behind.
    """

    def __init__(
        self,
        *,
        purge_trash_fn: Callable[..., Any] = purge_trash,
        task_purge_hook: Callable[..., Any] | None = None,
        interval_seconds: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._purge_trash = purge_trash_fn
        self._task_purge_hook = (
            task_purge_hook if task_purge_hook is not None else resolve_task_purge_hook()
        )
        configured_interval = _read_positive_int(
            "AOITALK_RETENTION_SWEEP_INTERVAL_SECONDS",
            DEFAULT_RETENTION_SWEEP_INTERVAL_SECONDS,
            maximum=7 * DEFAULT_RETENTION_SWEEP_INTERVAL_SECONDS,
        )
        configured_batch = _read_positive_int(
            "AOITALK_RETENTION_SWEEP_BATCH_SIZE",
            DEFAULT_RETENTION_SWEEP_BATCH_SIZE,
            maximum=MAX_RETENTION_SWEEP_BATCH_SIZE,
        )
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None and interval_seconds > 0
            else configured_interval
        )
        self.batch_size = int(
            batch_size
            if batch_size is not None and 0 < batch_size <= MAX_RETENTION_SWEEP_BATCH_SIZE
            else configured_batch
        )
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        """Expose the tracked loop for lifecycle/tests without mutating it."""

        return self._task

    @property
    def task_purge_hook(self) -> Callable[..., Any] | None:
        return self._task_purge_hook

    async def _invoke(
        self,
        function: Callable[..., Any],
        values: Mapping[str, Any],
    ) -> Any:
        kwargs = _supported_kwargs(function, values)
        result = function(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def run_once(self) -> dict[str, Any]:
        """Run one bounded sweep and return per-hook results.

        File and task cleanup are independent best-effort operations.  A
        failure in one is reported but cannot prevent the other from running.
        """

        results: dict[str, Any] = {"trash": None, "tasks": None, "next": None}
        try:
            results["trash"] = await asyncio.to_thread(
                self._invoke_sync,
                self._purge_trash,
                {"max_entries": self.batch_size},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive runtime path
            logger.warning("ファイルゴミ箱の定期掃除に失敗しました: %s", exc)

        hook = self._task_purge_hook
        if hook is not None:
            try:
                results["tasks"] = await self._invoke(
                    hook,
                    {
                        "batch_size": self.batch_size,
                        "retention_days": get_trash_retention_days(),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive runtime path
                logger.warning("Pythonタスクの定期掃除に失敗しました: %s", exc)
        try:
            results["next"] = await asyncio.to_thread(self._run_next_housekeeping)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive runtime path
            logger.warning("Next.jsコンテンツ保持期間掃除に失敗しました: %s", exc)
        return results

    @staticmethod
    def _run_next_housekeeping() -> dict[str, Any] | None:
        """Ask Next.js to run its canonical Chat/Docs cleanup once.

        No request is made when the internal key is absent (for local
        FastAPI-only or test processes).  The call is bounded and carries no
        content; Next.js owns all SQL and audit writes for its domains.
        """

        internal_key = os.environ.get("INTERNAL_API_KEY", "").strip()
        if not internal_key:
            return None
        base_url = os.environ.get("NEXTJS_URL", "http://127.0.0.1:3002").rstrip("/")
        url = f"{base_url}/api/internal/content-retention"
        request = urllib.request.Request(
            url,
            method="POST",
            headers={
                "x-internal-auth": internal_key,
                "content-type": "application/json",
            },
            data=b"{}",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
                return {"status": response.status, "body": payload[:4096]}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Next.js retention endpoint returned {exc.code}") from exc

    @staticmethod
    def _invoke_sync(function: Callable[..., Any], values: Mapping[str, Any]) -> Any:
        """Invoke a synchronous file purge with only supported kwargs."""

        kwargs = _supported_kwargs(function, values)
        result = function(**kwargs)
        # ``purge_trash`` is synchronous, but test doubles and embedding
        # callers may provide an async function.  The worker's thread must not
        # leak an un-awaited coroutine in that case.
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    async def _run_loop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        """Start the tracked loop; repeated starts are idempotent."""

        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="aoitalk-content-retention",
        )

    async def stop(self) -> None:
        """Cancel and await the housekeeping loop."""

        task = self._task
        self._task = None
        stop_event = self._stop_event
        self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "ContentRetentionWorker",
    "DEFAULT_RETENTION_SWEEP_INTERVAL_SECONDS",
    "DEFAULT_RETENTION_SWEEP_BATCH_SIZE",
    "resolve_task_purge_hook",
    "purge_expired_tasks",
]
