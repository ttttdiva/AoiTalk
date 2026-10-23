"""Opt-in owner-private in-app reminder delivery loop.

This is not an Agent or an autonomous Agent runner. It only reconciles
explicitly owner-scheduled reminder events after the staged lifecycle schema
is present. The default is disabled; an unavailable worker never authorizes a
new ``scheduled`` event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..utils.config_values import strict_config_bool
from .personal_reminder_service import PersonalReminderService

logger = logging.getLogger(__name__)


class PersonalReminderWorker:
    """Run due private reminders only after explicit server configuration."""

    def __init__(
        self,
        service: PersonalReminderService,
        *,
        config: Any,
        interval_seconds: float = 30.0,
    ) -> None:
        if not isinstance(service, PersonalReminderService):
            raise TypeError("PersonalReminderService required")
        self._service = service
        self._config = config
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError, OverflowError):
            interval = 30.0
        self._interval_seconds = min(300.0, max(5.0, interval))
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def _enabled(self) -> bool:
        if isinstance(self._config, Mapping):
            section = self._config.get("personal_reminders")
            value = section.get("worker_enabled", False) if isinstance(section, Mapping) else False
            return strict_config_bool(value, default=False)
        getter = getattr(self._config, "get", None)
        if not callable(getter):
            return False
        try:
            value = getter("personal_reminders.worker_enabled", False)
        except TypeError:
            try:
                value = getter("personal_reminders.worker_enabled")
            except Exception:
                return False
        except Exception:
            return False
        return strict_config_bool(value, default=False)

    async def run_once(self) -> bool:
        """Reconcile one sweep and publish readiness only on a complete success."""

        # Configuration can be revoked after startup.  Do not let a direct
        # run_once() call or an already running loop deliver while disabled.
        if not self._enabled():
            self._ready = False
            return False
        try:
            await self._service.deliver_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log owner IDs, reminder payloads, or exception text that
            # may include a SQL statement or decrypted event content.
            logger.warning("Personal reminder sweep unavailable (%s)", type(exc).__name__)
            self._ready = False
            return False
        self._ready = True
        return True

    async def start(self) -> None:
        """Start a lifecycle-owned loop only when explicitly enabled."""

        if not self._enabled():
            self._ready = False
            task = self._task
            if task is not None and not task.done():
                await self.stop()
            else:
                self._task = None
                self._stop_event = None
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(self._stop_event),
            name="aoitalk-personal-reminder-delivery",
        )

    async def stop(self) -> None:
        task, event = self._task, self._stop_event
        self._task = None
        self._stop_event = None
        self._ready = False
        if task is None:
            return
        if event is not None:
            event.set()
        try:
            await asyncio.wait_for(task, timeout=max(5.0, self._interval_seconds + 1.0))
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Personal reminder worker shutdown failed (%s)", type(exc).__name__)

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        finally:
            self._ready = False


__all__ = ["PersonalReminderWorker"]
