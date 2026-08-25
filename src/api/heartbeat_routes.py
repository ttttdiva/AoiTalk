"""
Heartbeat API Routes

Heartbeatの一覧取得・詳細・作成・更新・削除・手動トリガー・ステータスを提供する REST API。
"""
import logging
from typing import Optional, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..heartbeat.security import (
    GENERIC_REQUEST_ERROR,
    HeartbeatSecurityError,
    validate_heartbeat_name,
)

logger = logging.getLogger(__name__)


class CreateHeartbeatRequest(BaseModel):
    """Heartbeat作成リクエスト"""
    name: str
    description: str
    checklist: str
    interval_minutes: int = 30
    enabled: bool = True
    active_hours: Optional[dict] = None
    notify_channel: str = "websocket"


class UpdateHeartbeatRequest(BaseModel):
    """Heartbeat更新リクエスト"""
    description: Optional[str] = None
    checklist: Optional[str] = None
    interval_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    active_hours: Optional[dict] = None
    notify_channel: Optional[str] = None


def create_heartbeat_router(
    require_admin: Callable[..., Awaitable[None]],
) -> APIRouter:
    """Heartbeat API ルーターを作成（全エンドポイント管理者限定）"""
    router = APIRouter(prefix="/api/heartbeats", tags=["heartbeats"])

    @router.get("/status")
    async def get_runner_status(request: Request, _=Depends(require_admin)):
        """Runner全体のステータスを取得"""
        try:
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            return JSONResponse(content={"success": True, "status": runner.get_status()})
        except Exception:
            logger.exception("Heartbeatステータス取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.get("")
    async def list_heartbeats(request: Request, _=Depends(require_admin)):
        """全Heartbeat一覧を取得"""
        try:
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.runner import get_heartbeat_runner
            registry = get_heartbeat_registry()
            runner = get_heartbeat_runner()
            status = runner.get_status()

            heartbeats = []
            for h in registry.get_all():
                item = h.to_dict()
                item["last_result"] = status.get("last_results", {}).get(h.name)
                heartbeats.append(item)

            return JSONResponse(content={"success": True, "heartbeats": heartbeats})
        except Exception:
            logger.exception("Heartbeat一覧取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.get("/{name}")
    async def get_heartbeat(name: str, request: Request, _=Depends(require_admin)):
        """Heartbeat詳細を取得"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.runner import get_heartbeat_runner
            registry = get_heartbeat_registry()
            heartbeat = registry.get(safe_name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            runner = get_heartbeat_runner()
            result = heartbeat.to_dict()
            result["last_result"] = runner.get_status().get("last_results", {}).get(safe_name)
            return JSONResponse(content={"success": True, "heartbeat": result})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.post("")
    async def create_heartbeat(
        req: CreateHeartbeatRequest,
        request: Request,
        _=Depends(require_admin),
    ):
        """新しいHeartbeatを作成（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(req.name)
            from ..heartbeat.models import HeartbeatDefinition
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            if safe_name in registry:
                raise HTTPException(status_code=409, detail=GENERIC_REQUEST_ERROR)

            heartbeat = HeartbeatDefinition(
                name=safe_name,
                description=req.description,
                checklist=req.checklist,
                interval_minutes=req.interval_minutes,
                enabled=req.enabled,
                active_hours=req.active_hours,
                notify_channel=req.notify_channel,
                actions=[],
            )

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()}, status_code=201)
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat作成エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.put("/{name}")
    async def update_heartbeat(
        name: str,
        req: UpdateHeartbeatRequest,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを更新（管理者のみ。actions は YAML 管理のまま保持）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            heartbeat = registry.get(safe_name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            if req.description is not None:
                heartbeat.description = req.description
            if req.checklist is not None:
                heartbeat.checklist = req.checklist
            if req.interval_minutes is not None:
                heartbeat.interval_minutes = req.interval_minutes
            if req.enabled is not None:
                heartbeat.enabled = req.enabled
            if "active_hours" in req.model_fields_set:
                heartbeat.active_hours = req.active_hours
            if req.notify_channel is not None:
                heartbeat.notify_channel = req.notify_channel

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

            registry.unregister(safe_name)
            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat更新エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.delete("/{name}")
    async def delete_heartbeat(
        name: str,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを削除（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.loader import delete_heartbeat_yaml

            registry = get_heartbeat_registry()
            if safe_name not in registry:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            registry.unregister(safe_name)
            delete_heartbeat_yaml(safe_name)
            return JSONResponse(content={"success": True, "message": "Heartbeat を削除しました"})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat削除エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.post("/{name}/trigger")
    async def trigger_heartbeat(
        name: str,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを手動で即時実行（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            result = await runner.trigger(safe_name)
            if result is None:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)
            return JSONResponse(content={"success": True, "result": result})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeatトリガーエラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    return router
