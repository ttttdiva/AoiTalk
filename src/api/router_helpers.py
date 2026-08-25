import asyncio
from typing import Awaitable, Callable, TypeVar

from fastapi import Request


T = TypeVar("T")


async def await_task_completion_before_cancellation(awaitable: Awaitable[T]) -> T:
    """Do not orphan an atomic operation when a request is cancelled.

    The operation is allowed to finish, including under repeated cancellation,
    and the original cancellation is re-raised only after its result is safe.
    """
    worker = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.uncancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if current_task is not None:
                    current_task.uncancel()
        worker.result()
        raise cancellation


def cookie_auth_dependency(enforce_cookie_auth: Callable[[Request], None]):
    """Build a FastAPI dependency that delegates to the app cookie auth guard."""

    def require_auth(request: Request) -> None:
        enforce_cookie_auth(request)

    return require_auth
