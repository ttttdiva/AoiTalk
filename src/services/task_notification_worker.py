"""Background worker for task reminders and overdue notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .task_management_service import TaskManagementService

logger = logging.getLogger(__name__)


class TaskNotificationWorker:
    """Polls due task notifications and delivers them."""

    def __init__(self, db_manager, broadcaster=None, poll_interval_seconds: int = 60):
        self._db_manager = db_manager
        self._broadcaster = broadcaster
        self._poll_interval_seconds = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def run_once(self, *, now=None) -> dict[str, int]:
        service = TaskManagementService(broadcaster=self._broadcaster)
        session = await self._db_manager.get_session()
        try:
            # Auto-close runs before notification generation.  A task that was
            # completed at its due instant must not emit an overdue notice in
            # the same tick; row locks and the status guard keep this pass
            # idempotent when multiple workers overlap.
            close_stats = await service.auto_close_due_tasks(session, now=now)
            notification_stats = await service.deliver_due_notifications(
                session, now=now
            )
            stats = {**close_stats, **notification_stats}
            return stats
        finally:
            await session.close()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("TaskNotificationWorker started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("TaskNotificationWorker stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Task notification worker tick failed: {exc}")

            try:
                await asyncio.sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                break
