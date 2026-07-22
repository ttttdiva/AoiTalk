"""品質検証ルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...services.quality_verification_service import QualityVerificationService
from .schemas import UpdateQualityConfigRequest, VerifyRequest

logger = logging.getLogger(__name__)


def build_quality_router(require_auth: Callable[..., Any]) -> APIRouter:
    """品質検証の APIRouter を構築する。"""

    quality_router = APIRouter(
        prefix="/api/quality",
        tags=["quality"],
    )

    @quality_router.post("/verify")
    async def verify_response(
        req: VerifyRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """レスポンスの品質を検証"""
        try:
            service = QualityVerificationService()
            report = await service.verify_response(
                user_input=req.user_input,
                response=req.response,
                context=req.context,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "report": report.to_dict(),
                }
            )
        except Exception as e:
            logger.error("品質検証エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @quality_router.get("/config")
    async def get_quality_config(
        request: Request,
        _=Depends(require_auth),
    ):
        """品質検証の設定を取得"""
        try:
            service = QualityVerificationService()
            return JSONResponse(
                content={
                    "success": True,
                    "config": {"enabled": service.enabled},
                }
            )
        except Exception as e:
            logger.error("品質検証設定取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @quality_router.put("/config")
    async def update_quality_config(
        req: UpdateQualityConfigRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """品質検証の設定を更新"""
        try:
            service = QualityVerificationService()
            service.enabled = req.enabled
            return JSONResponse(
                content={
                    "success": True,
                    "config": {"enabled": service.enabled},
                }
            )
        except Exception as e:
            logger.error("品質検証設定更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return quality_router
