"""Runtime service manager for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.features import Features

from . import lifecycle

logger = logging.getLogger(__name__)


class DiscordBotServiceManager:
    """Starts and stops the Discord bot inside the active Python runtime."""

    def __init__(self) -> None:
        self._config: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def configure(self, config: Any) -> None:
        self._config = config

    def status(self) -> dict[str, Any]:
        task_running = bool(self._task and not self._task.done())
        return lifecycle.snapshot(task_running=task_running)

    async def ensure_started(self, config: Optional[Any] = None) -> dict[str, Any]:
        if config is not None:
            self.configure(config)

        async with self._lock:
            if self._task and not self._task.done():
                return self.status()

            if not Features.discord_bot():
                message = "Discord Bot機能は無効化されています (FEATURE_DISCORD_BOT=false)"
                lifecycle.mark_failed(message)
                logger.warning(message)
                return self.status()

            if self._config is None:
                lifecycle.mark_failed("Discord Bot設定が初期化されていません")
                return self.status()

            lifecycle.mark_starting()
            self._task = asyncio.create_task(
                self._run(self._config),
                name="discord-bot-service",
            )
            return self.status()

    async def stop(self) -> dict[str, Any]:
        task: Optional[asyncio.Task]
        async with self._lock:
            task = self._task
            if not task or task.done():
                lifecycle.mark_stopped()
                return self.status()
            lifecycle.mark_stopping()
            task.cancel()

        try:
            await asyncio.wait_for(task, timeout=15)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            lifecycle.mark_failed("Discord Botの停止がタイムアウトしました")
            logger.warning("Discord bot stop timed out")
        except Exception as exc:
            lifecycle.mark_failed(exc)
            logger.warning("Discord bot stopped with error: %s", exc)

        return self.status()

    async def _run(self, config: Any) -> None:
        try:
            from src.bot.discord_bot import run_bot

            await run_bot(config)
            current = lifecycle.snapshot()
            if current.get("state") not in {"stopping", "stopped", "failed"}:
                lifecycle.mark_stopped()
        except asyncio.CancelledError:
            lifecycle.mark_stopped()
            raise
        except Exception as exc:
            lifecycle.mark_failed(exc)
            logger.error("Discord bot service failed: %s", exc, exc_info=True)


discord_bot_service = DiscordBotServiceManager()
