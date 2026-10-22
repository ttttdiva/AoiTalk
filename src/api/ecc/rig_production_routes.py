"""Authenticated admin host settings, separate from character production ownership."""
from fastapi import APIRouter, Depends, HTTPException, Request

from ...aoirig import production


def build_rig_production_router(app_instance, require_auth):
    router = APIRouter(prefix="/api/rig-production", tags=["rig-production"])

    async def can_manage(request):
        checker = getattr(app_instance, "_is_admin_user", None)
        return bool(await checker(request)) if callable(checker) else False

    async def admin(request: Request, _=Depends(require_auth)):
        if not await can_manage(request):
            raise HTTPException(403, "管理者権限が必要です")
        try:
            production.require_personal()
        except production.ProductionError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    async def call(fn, *args):
        try:
            return await fn(*args)
        except production.ProductionError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("/settings", response_model=production.ProductionSettingsView)
    async def get_settings(request: Request, _=Depends(require_auth)):
        return await call(production.view, await can_manage(request))

    @router.put("/settings", response_model=production.ProductionSettingsView)
    async def put_settings(body: production.ProductionSettingsRequest, _=Depends(admin)):
        return await call(production.save_settings, body)

    @router.post("/doctor", response_model=production.ProductionSettingsView)
    async def doctor(body: production.ProductionOperationRequest | None = None, _=Depends(admin)):
        return await call(production.doctor, body.path if body else None)

    @router.post("/install", response_model=production.ProductionSettingsView)
    async def install(body: production.ProductionOperationRequest | None = None, _=Depends(admin)):
        return await call(production.operate, "install", body.path if body else None)

    @router.post("/prepare", response_model=production.ProductionSettingsView)
    async def prepare(body: production.ProductionOperationRequest | None = None, _=Depends(admin)):
        return await call(production.operate, "prepare", body.path if body else None)

    @router.post("/update", response_model=production.ProductionSettingsView)
    async def update(body: production.ProductionOperationRequest | None = None, _=Depends(admin)):
        return await call(production.operate, "update", body.path if body else None)

    return router
