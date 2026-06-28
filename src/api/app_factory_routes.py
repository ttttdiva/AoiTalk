"""App factory artifact API routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from ..services.app_factory_service import (
    resolve_artifact_download,
    resolve_artifact_preview,
)

logger = logging.getLogger(__name__)


def create_app_factory_router(
    *,
    require_auth_dependency,
    config: Any,
) -> APIRouter:
    router = APIRouter(prefix="/api/app-factory", tags=["app-factory"])

    @router.get("/artifacts/{artifact_id}/download")
    async def download_artifact(
        artifact_id: str,
        request: Request,
        _=Depends(require_auth_dependency),
    ):
        try:
            zip_path, filename = resolve_artifact_download(artifact_id, config=config)
            return FileResponse(
                zip_path,
                media_type="application/zip",
                filename=filename,
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("App factory download failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/artifacts/{artifact_id}/preview", response_class=HTMLResponse)
    async def preview_artifact(
        artifact_id: str,
        request: Request,
        _=Depends(require_auth_dependency),
    ):
        try:
            preview_path = resolve_artifact_preview(artifact_id, config=config)
            return HTMLResponse(
                content=preview_path.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-store"},
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("App factory preview failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
