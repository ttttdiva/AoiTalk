"""トークン使用量ルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
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

    async def _require_admin(request: Request) -> None:
        _, is_admin = await _usage_scope(request)
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")

    @usage_router.get("/dashboard")
    async def get_usage_dashboard(
        request: Request,
        user_id: Optional[str] = Query(None),
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """ダッシュボードサマリーを取得（今日/月次 + 30日推移 + 30日モデル別）"""
        try:
            scoped_user_id, is_admin = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            summary = await service.get_dashboard_summary(
                scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """日別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_daily_summary(
                start, end, scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """モデル別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_model(
                start, end, scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """プロジェクト別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_project(
                start, end, scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """エージェント別使用量サマリーを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_summary_by_agent(
                start, end, scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """指定期間の合計コストを取得"""
        try:
            _parse_date(start)
            _parse_date(end)
            scoped_user_id, _ = await _usage_scope(request, user_id)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_total_cost(
                start, end, scoped_user_id, include_free_incentive=include_free_incentive
            )
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
        include_free_incentive: bool = Query(True),
        _=Depends(require_auth),
    ):
        """管理者向けユーザー別使用量。

        無料枠は請求スコープ全体で先に割り当ててから、ユーザー単位へ集約する。
        """
        try:
            await _require_admin(request)

            _parse_date(start)
            _parse_date(end)
            data = await token_tracking_service.get_token_tracking_service().get_summary_by_user(
                start, end, include_free_incentive=include_free_incentive
            )
            return JSONResponse(content={"success": True, "by_user": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("ユーザー別使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ──────── 無料枠 ────────

    @usage_router.get("/free-tier")
    async def get_free_tier(
        request: Request,
        _=Depends(require_auth),
    ):
        """OpenAIデータ共有無料枠の本日（UTC日界）の使用量・上限・残量。"""
        try:
            await _usage_scope(request)
            service = token_tracking_service.get_token_tracking_service()
            data = await service.get_free_tier_status()
            return JSONResponse(content={"success": True, **data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("無料枠状態取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ──────── 料金カタログ ────────

    @usage_router.get("/pricing/status")
    async def get_pricing_status(
        request: Request,
        _=Depends(require_auth),
    ):
        """料金カタログ版・最終更新日時・無料枠設定を返す。"""
        try:
            await _usage_scope(request)
            service = token_tracking_service.get_token_tracking_service()
            pricing = await service.get_pricing_status()
            free_tier = await service.get_free_tier_status()
            return JSONResponse(
                content={"success": True, "pricing": pricing, "free_tier": free_tier}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("料金カタログ状態取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.post("/pricing/refresh")
    async def refresh_pricing(
        request: Request,
        force: bool = Query(False, description="TTLを無視して強制更新"),
        _=Depends(require_auth),
    ):
        """管理者向け: 料金表を更新する。

        `config/pricing_catalog.json` を再同期し、OpenRouter は公式 Models API から
        取り込む。失敗しても last-known-good の料金表は維持される。
        """
        try:
            await _require_admin(request)
            from ...services.pricing.catalog import sync_catalog_to_db
            from ...services.pricing.updater import refresh_openrouter_catalog

            catalog_result = await sync_catalog_to_db()
            openrouter_result = await refresh_openrouter_catalog(force=force)
            service = token_tracking_service.get_token_tracking_service()
            status = await service.get_pricing_status()
            ok = catalog_result.get("status") != "error"
            return JSONResponse(
                content={
                    "success": ok,
                    "result": {
                        "catalog": catalog_result,
                        "openrouter": openrouter_result,
                    },
                    "pricing": status,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("料金表更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.post("/pricing/import")
    async def import_pricing(
        request: Request,
        payload: Dict[str, Any] = Body(...),
        dry_run: bool = Query(True, description="Trueなら差分確認のみ"),
        _=Depends(require_auth),
    ):
        """管理者向け: 料金カタログJSONを取り込む（差分確認つき）。"""
        try:
            await _require_admin(request)
            from ...services.pricing.updater import import_catalog_json

            result = await import_catalog_json(payload, dry_run=dry_run)
            return JSONResponse(
                content={
                    "success": result.get("status") != "error",
                    "dry_run": dry_run,
                    "diff": result,
                }
            )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("料金カタログ取り込みエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ──────── 既存履歴の再計算 ────────

    @usage_router.post("/backfill")
    async def backfill_costs(
        request: Request,
        payload: Optional[Dict[str, Any]] = Body(None),
        _=Depends(require_auth),
    ):
        """管理者向け: 既存の $0 履歴を明示的に再計算する。

        body 例: ``{"dry_run": true, "start": "2026-01-01", "end": "2026-07-01",
        "provider": "openai", "model": "gpt-5.6", "only_zero_cost": true}``

        `provider_reported_cost` を持つ行は上書きしない。
        """
        try:
            await _require_admin(request)
            from ...services.pricing.backfill import (
                BackfillFilter,
                backfill_token_usage_costs,
            )
            from ...services.token_tracking_service import (
                _to_datetime,
                _to_exclusive_end,
            )

            body = payload or {}
            dry_run = bool(body.get("dry_run", True))
            start_raw = body.get("start")
            end_raw = body.get("end")
            _parse_date(start_raw)
            _parse_date(end_raw)

            filt = BackfillFilter(
                start=_to_datetime(start_raw),
                end=_to_exclusive_end(end_raw),
                provider=body.get("provider") or None,
                model=body.get("model") or None,
                only_zero_cost=bool(body.get("only_zero_cost", True)),
            )
            result = await backfill_token_usage_costs(
                filt, dry_run=dry_run, limit=body.get("limit")
            )
            return JSONResponse(content={"success": True, "result": result})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("コスト再計算エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return usage_router
