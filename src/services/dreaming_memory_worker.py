"""Idle Dreaming Memory worker lifecycle.

This worker is intentionally small.  Database/model imports stay inside the
service's lazy paths so importing the application does not make a rolling
deployment depend on the new Dreaming tables being migrated already.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from contextlib import suppress
from typing import Any, Callable

from .dreaming_consolidation_service import (
    DEFAULT_DREAMING_IDLE_SECONDS,
    DreamingConsolidationService,
)

logger = logging.getLogger(__name__)


class DreamingMemoryWorker:
    """Run independent per-user Dreaming jobs until the server stops."""

    def __init__(
        self,
        *,
        service: DreamingConsolidationService | Any | None = None,
        config: Any | None = None,
        llm_client: Any | None = None,
        llm_client_factory: Callable[..., Any] | None = None,
        interval_seconds: float | None = None,
        idle_seconds: float | None = None,
        batch_size: int | None = None,
        max_users_per_run: int | None = None,
        retry_base_seconds: float = 30.0,
        max_retry_seconds: float = 6 * 60 * 60,
    ) -> None:
        configured_interval = interval_seconds
        if configured_interval is None:
            configured_interval = float(
                os.getenv("AOITALK_DREAMING_POLL_INTERVAL_SECONDS", DEFAULT_DREAMING_IDLE_SECONDS)
            )
        configured_batch = batch_size
        if configured_batch is None:
            configured_batch = int(os.getenv("AOITALK_DREAMING_HISTORY_BATCH_MESSAGES", "120"))
        configured_idle = idle_seconds
        if configured_idle is None:
            configured_idle = float(
                os.getenv("AOITALK_DREAMING_IDLE_SECONDS", DEFAULT_DREAMING_IDLE_SECONDS)
            )
        configured_users = max_users_per_run
        if configured_users is None:
            configured_users = int(os.getenv("AOITALK_DREAMING_USERS_PER_SWEEP", "3"))
        self.service = service or DreamingConsolidationService(
            config=config,
            llm_client=llm_client,
            llm_client_factory=llm_client_factory,
            batch_size=max(1, int(configured_batch)),
            idle_seconds=(
                float(configured_idle)
            ),
            retry_base_seconds=retry_base_seconds,
            max_retry_seconds=max_retry_seconds,
        )
        self.llm_client = llm_client
        self.llm_client_factory = llm_client_factory
        self.interval_seconds = max(0.1, float(configured_interval))
        self.reconcile_interval_seconds = max(
            self.interval_seconds,
            float(os.getenv("AOITALK_DREAMING_RECONCILE_INTERVAL_SECONDS", "86400")),
        )
        self.batch_size = max(1, int(configured_batch))
        self.max_users_per_run = max(1, int(configured_users))
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def _invoke_process(self, user_id: str, *, trigger: str = "idle") -> Any:
        process = getattr(self.service, "process_dreaming_user", None)
        if not callable(process):
            raise RuntimeError("DreamingConsolidationService.process_dreaming_user is unavailable")
        kwargs: dict[str, Any] = {"trigger": trigger}
        if self.llm_client is not None:
            kwargs["llm_client"] = self.llm_client
        if self.llm_client_factory is not None and hasattr(self.service, "llm_client_factory"):
            # The service resolves a per-user factory itself.  Passing both a
            # shared client and a factory would make retries nondeterministic.
            kwargs.pop("llm_client", None)
        try:
            parameters = inspect.signature(process).parameters
            if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        except (TypeError, ValueError):
            pass
        try:
            result = process(str(user_id), **kwargs)
        except TypeError:
            result = process(str(user_id))
        if inspect.isawaitable(result):
            return await result
        return result

    async def run_once(self, *, trigger: str = "idle") -> dict[str, Any]:
        """Process an idle batch; one user's failure never aborts siblings."""

        load = getattr(self.service, "load_idle_users", None)
        if not callable(load):
            return {"status": "skipped", "users": [], "results": []}
        try:
            try:
                users = load(limit=self.max_users_per_run)
            except TypeError:
                users = load()
            users = await users if inspect.isawaitable(users) else users
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Dreaming idle-user load failed: %s", exc)
            return {"status": "failed", "users": [], "results": [], "error": str(exc)}

        user_ids = [str(value) for value in (users or []) if str(value).strip()]
        results: list[dict[str, Any]] = []
        for user_id in user_ids[: self.max_users_per_run]:
            try:
                result = await self._invoke_process(user_id, trigger=trigger)
                results.append(result if isinstance(result, dict) else {"user_id": user_id, "result": result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Service normally returns a durable failed result, but this
                # guard also covers test doubles and unexpected adapters.
                logger.warning("Dreaming worker user=%s failed: %s", user_id, exc)
                results.append({"user_id": user_id, "status": "failed", "error": str(exc)})
        failed = sum(1 for item in results if item.get("status") == "failed")
        return {
            "status": "failed" if failed and failed == len(results) else "completed",
            "users": user_ids,
            "results": results,
            "processed": len(results),
            "failed": failed,
        }

    async def _run_loop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        # Startup is intentionally non-blocking from the server's lifespan:
        # this task performs the first bounded sweep in the background.
        recover = getattr(self.service, "recover_stale_runs", None)
        if callable(recover):
            try:
                result = recover()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.warning("Dreaming stale-run recovery failed: %s", type(exc).__name__)
        await self.run_once(trigger="startup")
        next_reconcile = time.monotonic() + self.reconcile_interval_seconds
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                trigger = "reconcile" if time.monotonic() >= next_reconcile else "idle"
                if trigger == "reconcile":
                    next_reconcile = time.monotonic() + self.reconcile_interval_seconds
                # The next loop iteration performs the selected bounded sweep.
                try:
                    await self.run_once(trigger=trigger)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover
                    logger.warning("Dreaming worker scheduled sweep failed: %s", type(exc).__name__)
            except asyncio.CancelledError:
                raise

    async def start(self) -> None:
        """Start the tracked loop; repeated starts are idempotent."""

        if self._task is not None and not self._task.done():
            self._running = True
            return
        self._stop_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="aoitalk-dreaming-memory")

    async def stop(self) -> None:
        """Cancel and await the worker task without leaking background work."""

        task = self._task
        self._task = None
        self._running = False
        event = self._stop_event
        self._stop_event = None
        if event is not None:
            event.set()
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = ["DreamingMemoryWorker"]
