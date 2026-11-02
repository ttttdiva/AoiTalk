"""Minimal owner-authenticated Chat send adapter for one dedicated DOTS chat."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...services.dots_dialogue_service import (
    DedicatedDotsPrincipal,
    DotsDialogueError,
    DotsDialogueService,
)


class DedicatedDotsChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=32_768)


def create_dots_dialogue_chat_router(
    *,
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[[Request], Awaitable[dict[str, Any]]],
    require_auth_dependency: Callable[..., Any],
    principal: DedicatedDotsPrincipal | None,
    enabled: bool = False,
) -> APIRouter:
    """Expose only the one server-bound conversation's send operation.

    History is read through the existing ``GET /api/conversations/{id}/messages``
    route. Neither the owner, binding nor conversation can be supplied in the
    body. The path is checked against the immutable server-owned principal.
    """
    router = APIRouter(prefix="/api/conversations", tags=["dedicated DOTS Chat"])
    service = DotsDialogueService(enabled=enabled is True)

    async def run(callback: Callable[[Any], Awaitable[Any]]) -> Any:
        manager = get_db_manager()
        session = manager.get_session()
        if hasattr(session, "__await__"):
            session = await session
        try:
            result = await callback(session)
            await session.commit()
            return result
        except DotsDialogueError as exc:
            await session.rollback()
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from None
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post("/{conversation_id}/dots-dialogue/messages", status_code=202,
                 dependencies=[Depends(require_auth_dependency)])
    async def send_dots_message(conversation_id: UUID, payload: DedicatedDotsChatMessage,
                                request: Request):
        if enabled is not True or not isinstance(principal, DedicatedDotsPrincipal):
            raise HTTPException(status_code=404, detail={"code": "dots_dialogue_disabled"})
        try:
            actor = await get_user_from_request(request)
            owner_id = UUID(str(actor["id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"}) from None
        if conversation_id != principal.conversation_id:
            raise HTTPException(status_code=404, detail={"code": "conversation_unavailable"})
        receipt = await run(lambda db: service.enqueue_dedicated_request(
            db,
            owner_user_id=owner_id,
            principal=principal,
            idempotency_key=payload.idempotency_key,
            message=payload.message,
        ))
        return {
            "request_id": str(receipt.request_id),
            "conversation_id": str(receipt.conversation_id),
            "state": receipt.state,
            "created_at": receipt.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    return router


__all__ = ["DedicatedDotsChatMessage", "create_dots_dialogue_chat_router"]
