"""トークン使用量ルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...services import token_tracking_service
from ..ecc_helpers import parse_date as _parse_date

logger = logging.getLogger(__name__)


def build_usage_router(app_instance: Any, require_auth: Callable[..., Any]) -> APIRouter:
    """トークン使用量の APIRouter を構築する。"""

    usage_router = APIRouter(
        prefix="/api/usage",
        tags=["token-usage"],
    )

    async def _usage_scope(request: Request, requested_user_id: Optional[str] = None):
        user_info = await app_instance._get_user_info_from_request(request)
        if user_info is None and not app_instance.auth_enabled:
            user_info = {"id": "default_user", "role": "admin"}
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        is_admin = str(user_info.get("role") or "") == "admin"
        if requested_user_id and not is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        return (requested_user_id if is_admin else str(user_info["id"])), is_admin

    @usage_router.get("/dashboard")
    async def get_usage_dashboard(
        request: Request,
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """ダッシュボードサマリーを取得（今日 + 7日推移 + 30日モデル別）"""
        try:
            scoped_user_id, is_admin = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            summary = await service.get_dashboard_summary(scoped_user_id)
            return JSONResponse(content={"success": True, "scope": "all" if is_admin and not scoped_user_id else "user", **summary})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("使用量ダッシュボード取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/daily")
    async def get_usage_daily(
        request: Request,
        start: Optional[str] = Query(None, description="開始日 (YYYY-MM-DD)"),
        end: Optional[str] = Query(None, description="終了日 (YYYY-MM-DD)"),
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """日別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_daily_summary(start, end, scoped_user_id)
            return JSONResponse(content={"success": True, "daily": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("日別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/by-model")
    async def get_usage_by_model(
        request: Request,
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """モデル別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_model(start, end, scoped_user_id)
            return JSONResponse(content={"success": True, "by_model": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("モデル別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/by-project")
    async def get_usage_by_project(
        request: Request,
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """プロジェクト別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_project(start, end, scoped_user_id)
            return JSONResponse(content={"success": True, "by_project": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("プロジェクト別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/by-agent")
    async def get_usage_by_agent(
        request: Request,
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """エージェント別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_agent(start, end, scoped_user_id)
            return JSONResponse(content={"success": True, "by_agent": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("エージェント別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/total")
    async def get_usage_total(
        request: Request,
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """指定期間の合計コストを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_total_cost(start, end, scoped_user_id)
            return JSONResponse(content={"success": True, "total": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("合計使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/by-user")
    async def get_usage_by_user(
        request: Request,
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """管理者向けユーザー別使用量。"""
        try:
            _, is_admin = await _usage_scope(request)
            if not is_admin:
                raise HTTPException(status_code=403, detail="Admin access required")

            _parse_date(start)
            _parse_date(end)
            data = await token_tracking_service.get_token_tracking_service().get_summary_by_user(
                start, end
            )
            return JSONResponse(content={"success": True, "by_user": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("ユーザー別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return usage_router
