import asyncio
from typing import Any, Awaitable, Callable, TypeVar

from fastapi import Depends, HTTPException, Request

from ..services.admin_restart_admission import (
    AdminRestartPendingError,
    begin_chat_request,
    finish_chat_request,
)


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


def restart_work_admission_dependency(
    server: Any,
    *,
    rejection_message: str = "サーバー再起動の準備中のため、新しい処理を開始できません。",
):
    """Fence a side-effecting API operation while admin restart is prepared."""

    async def require_restart_work_admission():
        try:
            task = begin_chat_request(server)
        except AdminRestartPendingError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "admin_restart_pending", "message": rejection_message},
            ) from exc
        try:
            yield None
        finally:
            finish_chat_request(server, task)

    return require_restart_work_admission


def conversation_restart_admission_dependency(server: Any, require_auth):
    """Keep an authenticated conversation request visible to restart checks."""

    require_restart_work = restart_work_admission_dependency(
        server,
        rejection_message="サーバー再起動の準備中のため、会話を開始できません。",
    )

    async def require_conversation_admission(
        _auth: None = Depends(require_auth),
        _work: None = Depends(require_restart_work),
    ):
        yield None

    return require_conversation_admission
