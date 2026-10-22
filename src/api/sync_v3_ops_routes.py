"""Sync v3 の書き込みAPI（POST /api/sync/v3/ops）。"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..sync_v3 import contract
from ..sync_v3.ops import BodyInvalid, apply_ops

logger = logging.getLogger(__name__)


def create_sync_v3_ops_router(
    enabled_check: Callable[[AsyncSession], Awaitable[bool]],
    *,
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/sync/v3", tags=["sync-v3"])

    async def _current_user(request: Request) -> UUID:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(str(user_info["id"]))

    @router.post("/ops", operation_id="sync_v3_push_ops")
    async def sync_v3_push_ops(request: Request, _auth=Depends(require_auth_dependency)):
        user_id = await _current_user(request)
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
            results = await apply_ops(get_db_manager().get_session, user_id, body)
        except BodyInvalid as exc:
            raise HTTPException(status_code=400, detail=f"invalid {exc}") from None
        return {"results": results}

    return router
