"""応答を画面に出したあとのタイトル生成と記憶索引。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


def schedule_after_visible_response(
    work: Union[Callable[[], Awaitable[Any]], Awaitable[Any]],
    *,
    name: str,
) -> None:
    """表示を待たせずに後処理を始める。強参照を残し、失敗はログだけにする。

    コルーチンはタスクが動き始めてから作る。呼び出し時点で作ると、イベント
    ループがタスクを走らせる前に捨てられたときだけ警告になる。
    """

    async def _run() -> None:
        try:
            result = work() if callable(work) and not inspect.iscoroutine(work) else work
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Post-response work failed: %s", name, exc_info=True)

    try:
        task = asyncio.create_task(_run(), name=name)
    except RuntimeError:
        close = getattr(work, "close", None)
        if callable(close):
            close()
        logger.warning("Post-response work was not scheduled: %s", name)
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_deferred_session_index(memory_manager: Any, session_id: Optional[str]) -> None:
    if not session_id or memory_manager is None:
        return
    schedule = getattr(memory_manager, "schedule_deferred_cross_session_indexes", None)
    if not callable(schedule):
        return
    try:
        schedule(str(session_id))
    except Exception:
        logger.warning(
            "Deferred conversation index was not scheduled",
            exc_info=True,
        )
