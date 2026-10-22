"""Authenticated character rig API; no public filesystem paths or cross-owner assets."""

from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse

from ...aoirig import service, production
from ...aoirig.playback_settings import PlaybackSettingsReset, PlaybackSettingsUpdate, PlaybackSettingsView
from ...aoirig.spec import Record
from ...aoirig.production_worker import ProductionWorker
from .rig_production_routes import build_rig_production_router


class BuildRequest(Record):
    revision: str | None = None


class EditorView(Record):
    url: str
    web_url: str | None = None


class ActivationRequest(Record):
    revision: UUID | None = None


class RigMutationResult(Record):
    success: bool = True


def build_character_rig_router(app_instance, require_auth):
    router = APIRouter(prefix="/api/characters/manage/{character_id}/rig", tags=["character-rig"])
    worker = getattr(app_instance, "_seethrough_production_worker", None)
    if worker is None:
        worker = ProductionWorker()
        app_instance._seethrough_production_worker = worker
    if isinstance(getattr(app_instance, "_startup_background_tasks", None), list) and not getattr(app_instance, "_seethrough_worker_hooks_registered", False):
        app_instance._seethrough_worker_hooks_registered = True
        app_instance._startup_background_tasks.append(worker.start)
        app_instance._shutdown_background_tasks.append(worker.stop)

    async def owner(request: Request, _=Depends(require_auth)) -> str:
        if getattr(app_instance, "auth_enabled", True) is False:
            return "local"
        info = await app_instance._get_user_info_from_request(request)
        if not isinstance(info, dict) or not info.get("id"):
            raise HTTPException(401, "認証が必要です")
        return str(info["id"])

    async def call(fn, *args):
        try:
            return await fn(*args)
        except (service.RigError, production.ProductionError) as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("", response_model=service.RigView)
    async def get_rig(character_id: str, principal: str = Depends(owner)):
        return await call(service.status, character_id, principal)

    @router.post("/source", response_model=RigMutationResult)
    async def upload_source(character_id: str, file: UploadFile = File(...), principal: str = Depends(owner)):
        try:
            data = await file.read(16 * 1024 * 1024 + 1)
            await call(service.upload, character_id, principal, data)
        finally:
            await file.close()
        return RigMutationResult()

    @router.post("/builds", response_model=service.BuildView, status_code=202)
    async def create_build(character_id: str, body: BuildRequest | None = None, principal: str = Depends(owner)):
        result = await call(service.enqueue, character_id, principal, body.revision if body else None)
        await worker.start()
        return result

    @router.post("/import", response_model=service.BuildView)
    async def import_rig(character_id: str, file: UploadFile = File(...), principal: str = Depends(owner)):
        from ...aoirig.package_import import MAX_UPLOAD
        try:
            data = await file.read(MAX_UPLOAD + 1)
            return await call(service.import_package, character_id, principal, data)
        finally:
            await file.close()

    @router.delete("/builds/{build_id}", response_model=RigMutationResult)
    async def delete_build(character_id: str, build_id: UUID, principal: str = Depends(owner)):
        await call(service.archive, character_id, principal, build_id)
        return RigMutationResult()

    @router.post("/builds/{build_id}/cancel", response_model=service.BuildView)
    async def cancel_build(character_id: str, build_id: UUID, principal: str = Depends(owner)):
        return await call(service.control, character_id, principal, build_id, "cancel")

    @router.post("/builds/{build_id}/resume", response_model=service.BuildView)
    async def resume_build(character_id: str, build_id: UUID, principal: str = Depends(owner)):
        result = await call(service.control, character_id, principal, build_id, "resume")
        await worker.start()
        return result

    @router.post("/builds/{build_id}/edit", response_model=EditorView)
    async def edit_build(request: Request, character_id: str, build_id: UUID, principal: str = Depends(owner)):
        from ...aoirig import production_editor
        relative = await call(production_editor.edit_url, app_instance, request, character_id, principal, build_id)
        return EditorView(url=relative, web_url=await production_editor.external_url(relative))

    @router.put("/active", response_model=RigMutationResult)
    async def set_active(character_id: str, body: ActivationRequest, principal: str = Depends(owner)):
        await call(service.activate, character_id, principal, body.revision)
        return RigMutationResult()

    @router.get("/builds/{build_id}/playback-settings", response_model=PlaybackSettingsView, response_model_exclude_unset=True)
    async def get_playback_settings(character_id: str, build_id: UUID, response: Response,
                                    mode: Literal["model", "conversation-v1", "comparison-v1"] = "model", principal: str = Depends(owner)):
        response.headers["Cache-Control"] = "private, no-store"
        return await call(service.get_playback_settings, character_id, principal, build_id, mode)

    @router.patch("/builds/{build_id}/playback-settings", response_model=PlaybackSettingsView, response_model_exclude_unset=True)
    async def patch_playback_settings(character_id: str, build_id: UUID, body: PlaybackSettingsUpdate, response: Response,
                                      mode: Literal["model", "conversation-v1", "comparison-v1"] = "model", principal: str = Depends(owner)):
        response.headers["Cache-Control"] = "private, no-store"
        return await call(service.save_playback_settings, character_id, principal, build_id, mode,
                          body.manifest_sha256, body.expected_revision, body.settings.model_dump(exclude_unset=True))

    @router.delete("/builds/{build_id}/playback-settings", response_model=PlaybackSettingsView, response_model_exclude_unset=True)
    async def reset_playback_settings(character_id: str, build_id: UUID, body: PlaybackSettingsReset, response: Response,
                                      mode: Literal["model", "conversation-v1", "comparison-v1"] = "model", principal: str = Depends(owner)):
        response.headers["Cache-Control"] = "private, no-store"
        return await call(service.save_playback_settings, character_id, principal, build_id, mode,
                          body.manifest_sha256, body.expected_revision, None)

    @router.get("/source.png")
    async def source_image(character_id: str, principal: str = Depends(owner)):
        path = await call(service.asset, character_id, principal, None, "source.png")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/assets/{revision}/{name}")
    async def get_asset(character_id: str, revision: UUID, name: str, principal: str = Depends(owner)):
        path = await call(service.asset, character_id, principal, revision, name)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(build_rig_production_router(app_instance, require_auth))
    from ...aoirig.production_editor import register_editor_routes
    register_editor_routes(combined, owner, call)
    return combined
