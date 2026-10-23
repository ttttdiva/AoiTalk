"""Sync v3 の書き込みAPI（POST /api/sync/v3/ops）。"""
from __future__ import annotations

import json
import logging
import os
from typing import Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..sync_v3 import contract
from ..services.task_management_service import TaskManagementService
from ..sync_v3.ops import BodyInvalid, apply_ops
from .sync_v3_appliers import build_v3_appliers

logger = logging.getLogger(__name__)


def create_sync_v3_ops_router(
    enabled_check: Callable[[AsyncSession], Awaitable[bool]],
    *,
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    workspace_root: str | os.PathLike[str] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/sync/v3", tags=["sync-v3"])
    service = TaskManagementService()

    async def _current_user(request: Request) -> tuple[UUID, dict]:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(str(user_info["id"])), user_info

    @router.post("/ops", operation_id="sync_v3_push_ops")
    async def sync_v3_push_ops(request: Request, _auth=Depends(require_auth_dependency)):
        user_id, user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            enabled = await enabled_check(session)
        finally:
            await session.rollback()
            await session.close()
        if not enabled:
            return JSONResponse(status_code=503, content={"code": contract.SYNC_DISABLED_CODE})
        raw = await request.body()
        if len(raw) > contract.MAX_OPS_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid body") from None
        if isinstance(body, dict) and isinstance(body.get("ops"), list):
            if len(body["ops"]) > contract.MAX_OPS_PER_REQUEST:
                raise HTTPException(status_code=413, detail="too many ops")
        try:
            session_factory = get_db_manager().get_session
            results = await apply_ops(
                session_factory,
                user_id,
                user_info,
                body,
                appliers=build_v3_appliers(service, workspace_root, session_factory),
            )
        except BodyInvalid as exc:
            raise HTTPException(status_code=400, detail=f"invalid {exc}") from None
        return {"results": results}

    return router
