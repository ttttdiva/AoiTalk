"""Authenticated one-click CrowdWorks marketplace setup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .agent_action_policy_routes import policy_actor
from ..services.integration_action_registry import ActionPolicyError
from ..services.marketplace.crowdworks_bootstrap import CrowdWorksBootstrapService


def create_crowdworks_marketplace_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    *,
    policy_service=None,
    config=None,
):
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    service = CrowdWorksBootstrapService(
        manager,
        config=config,
        policy_service=policy_service,
    )
    router = APIRouter(
        prefix="/api/marketplace/crowdworks",
        tags=["marketplace-crowdworks"],
        dependencies=[Depends(require_auth_dependency)],
    )

    async def actor(request: Request):
        return await policy_actor(request, get_user_from_request)

    @router.get("/status")
    async def status(request: Request):
        user_id = await actor(request)
        try:
            return {"success": True, "result": await service.status(user_id)}
        except ActionPolicyError as exc:
            raise HTTPException(exc.status_code, exc.code) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/bootstrap")
    async def bootstrap(request: Request):
        user_id = await actor(request)
        try:
            return {"success": True, "result": await service.bootstrap(user_id)}
        except ActionPolicyError as exc:
            raise HTTPException(exc.status_code, exc.code) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    return router


__all__ = ["create_crowdworks_marketplace_router"]
