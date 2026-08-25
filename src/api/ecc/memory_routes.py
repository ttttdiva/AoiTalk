"""Authenticated Scoped Memory API with legacy-compatible paths."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...services.scoped_memory_service import ScopedMemoryError, ScopedMemoryService

logger = logging.getLogger(__name__)


def build_memory_router(
    require_auth: Callable[..., Any],
    get_user_id: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/memories", tags=["scoped-memory"])
    service = ScopedMemoryService()

    async def actor(request: Request) -> str:
        return str(await get_user_id(request))

    def mapped_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ScopedMemoryError):
            return HTTPException(status_code=exc.status_code, detail=str(exc))
        logger.exception("Scoped Memory API error")
        return HTTPException(status_code=500, detail="memory operation failed")

    @router.get("/settings")
    async def get_settings(
        request: Request,
        project_id: str | None = None,
        _=Depends(require_auth),
    ):
        try:
            return {
                "success": True,
                "settings": await service.get_settings(
                    actor_id=await actor(request), project_id=project_id
                ),
            }
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.patch("/settings")
    async def patch_settings(request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            settings = await service.update_settings(
                actor_id=await actor(request),
                user_auto_enabled=body.get("user_auto_enabled"),
                project_id=body.get("project_id"),
                project_auto_enabled=body.get("project_auto_enabled"),
            )
            return {"success": True, "settings": settings}
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.get("/jobs")
    async def list_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        _=Depends(require_auth),
    ):
        try:
            jobs = await service.list_jobs(actor_id=await actor(request), limit=limit)
            return {"success": True, "jobs": jobs}
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("/corrections")
    async def record_correction(request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            result = await service.record_correction(
                actor_id=await actor(request),
                subject=body.get("subject", ""),
                desired=body.get("desired", ""),
                evidence=body.get("evidence"),
                utterance=body.get("utterance"),
                project_id=body.get("project_id"),
                task_id=body.get("task_id"),
                session_id=body.get("session_id"),
            )
            return JSONResponse(content=result, status_code=201)
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.get("")
    async def list_memories(
        request: Request,
        scope: str | None = None,
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        include_history: bool = False,
        limit: int = Query(default=200, ge=1, le=1000),
        _=Depends(require_auth),
    ):
        try:
            memories = await service.list_memories(
                actor_id=await actor(request),
                scope_type=scope,
                scope_id=scope_id,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                status=status,
                include_history=include_history,
                limit=limit,
            )
            return {"success": True, "memories": memories}
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("")
    async def create_memory(request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            result = await service.upsert_memory(
                actor_id=await actor(request),
                content=body.get("content", ""),
                scope_type=body.get("scope", body.get("scope_type", "user")),
                scope_id=body.get("scope_id"),
                project_id=body.get("project_id"),
                task_id=body.get("task_id"),
                session_id=body.get("session_id"),
                memory_type=body.get("memory_type", "fact"),
                title=body.get("title"),
                structured_data=body.get("structured_data"),
                source_type="manual",
                source_ref=body.get("source_ref"),
                evidence_refs=body.get("evidence_refs"),
                evidence_span=body.get("evidence_span"),
                confidence=body.get("confidence", 1.0),
                importance=body.get("importance", 7),
                is_pinned=body.get("is_pinned", False),
                status=body.get("status", "active"),
                idempotency_key=body.get("idempotency_key"),
            )
            return JSONResponse(content=result, status_code=201)
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.delete("/all")
    async def forget_all(request: Request, _=Depends(require_auth)):
        try:
            actor_id = await actor(request)
            rows = await service.list_memories(actor_id=actor_id, limit=1000)
            for row in rows:
                await service.forget_memory(
                    row["id"],
                    actor_id=actor_id,
                    expected_version=int(row.get("version") or 1),
                    reason="explicit_forget_all",
                )
            return {"success": True, "forgotten": len(rows), "deleted": 0}
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.get("/{memory_id}")
    async def get_memory(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            return {
                "success": True,
                "memory": await service.get_memory(
                    memory_id, actor_id=await actor(request)
                ),
            }
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.patch("/{memory_id}")
    async def update_memory(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            if body.get("version") is None:
                raise HTTPException(status_code=428, detail="version is required")
            changes = {key: value for key, value in body.items() if key != "version"}
            return await service.update_memory(
                memory_id,
                actor_id=await actor(request),
                changes=changes,
                expected_version=int(body["version"]),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.delete("/{memory_id}")
    async def forget_memory(
        memory_id: str,
        request: Request,
        version: int | None = None,
        _=Depends(require_auth),
    ):
        try:
            if version is None:
                raise HTTPException(status_code=428, detail="version is required")
            return await service.forget_memory(
                memory_id,
                actor_id=await actor(request),
                expected_version=version,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("/{memory_id}/approve")
    async def approve(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            if body.get("version") is None:
                raise HTTPException(status_code=428, detail="version is required")
            return await service.decide_candidate(
                memory_id,
                actor_id=await actor(request),
                approve=True,
                expected_version=int(body["version"]),
                reason=body.get("reason"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("/{memory_id}/reject")
    async def reject(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            if body.get("version") is None:
                raise HTTPException(status_code=428, detail="version is required")
            return await service.decide_candidate(
                memory_id,
                actor_id=await actor(request),
                approve=False,
                expected_version=int(body["version"]),
                reason=body.get("reason"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("/{memory_id}/move-scope")
    async def move_scope(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            if body.get("version") is None:
                raise HTTPException(status_code=428, detail="version is required")
            return await service.move_scope(
                memory_id,
                actor_id=await actor(request),
                expected_version=int(body["version"]),
                scope_type=body.get("scope", "user"),
                scope_id=body.get("scope_id"),
                project_id=body.get("project_id"),
                task_id=body.get("task_id"),
                session_id=body.get("session_id"),
                reason=body.get("reason", "scope_move"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.post("/{memory_id}/promote")
    async def promote(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
            if body.get("version") is None:
                raise HTTPException(status_code=428, detail="version is required")
            return await service.promote_to_project_information(
                memory_id,
                actor_id=await actor(request),
                expected_version=int(body["version"]),
                target_section=body.get("target_section"),
                source_refs=body.get("source_refs"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise mapped_error(exc) from exc

    @router.get("/{memory_id}/explain")
    async def explain(memory_id: str, request: Request, _=Depends(require_auth)):
        try:
            return {
                "success": True,
                **(await service.explain(memory_id, actor_id=await actor(request))),
            }
        except Exception as exc:
            raise mapped_error(exc) from exc

    return router
