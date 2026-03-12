"""
Heartbeat API Routes

Heartbeatの一覧取得・詳細・作成・更新・削除・手動トリガー・ステータスを提供する REST API。
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


def create_heartbeat_router(require_auth) -> APIRouter:
    """Heartbeat API ルーターを作成"""
    router = APIRouter(prefix="/api/heartbeats", tags=["heartbeats"])

    @router.get("/status")
    async def get_runner_status(request: Request, _=Depends(require_auth)):
        """Runner全体のステータスを取得"""
        try:
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            return JSONResponse(content={"success": True, "status": runner.get_status()})
        except Exception as e:
            logger.error(f"Heartbeatステータス取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("")
    async def list_heartbeats(request: Request, _=Depends(require_auth)):
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
        except Exception as e:
            logger.error(f"Heartbeat一覧取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{name}")
    async def get_heartbeat(name: str, request: Request, _=Depends(require_auth)):
        """Heartbeat詳細を取得"""
        try:
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.runner import get_heartbeat_runner
            registry = get_heartbeat_registry()
            heartbeat = registry.get(name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=f"Heartbeat '{name}' が見つかりません")

            runner = get_heartbeat_runner()
            result = heartbeat.to_dict()
            result["last_result"] = runner.get_status().get("last_results", {}).get(name)
            return JSONResponse(content={"success": True, "heartbeat": result})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Heartbeat取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("")
    async def create_heartbeat(req: CreateHeartbeatRequest, request: Request, _=Depends(require_auth)):
        """新しいHeartbeatを作成"""
        try:
            from ..heartbeat.models import HeartbeatDefinition
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            if req.name in registry:
                raise HTTPException(status_code=409, detail=f"Heartbeat '{req.name}' は既に存在します")

            heartbeat = HeartbeatDefinition(
                name=req.name,
                description=req.description,
                checklist=req.checklist,
                interval_minutes=req.interval_minutes,
                enabled=req.enabled,
                active_hours=req.active_hours,
                notify_channel=req.notify_channel,
            )

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail="YAML保存に失敗しました")

            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()}, status_code=201)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Heartbeat作成エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{name}")
    async def update_heartbeat(name: str, req: UpdateHeartbeatRequest, request: Request, _=Depends(require_auth)):
        """Heartbeatを更新"""
        try:
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            heartbeat = registry.get(name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=f"Heartbeat '{name}' が見つかりません")

            if req.description is not None:
                heartbeat.description = req.description
            if req.checklist is not None:
                heartbeat.checklist = req.checklist
            if req.interval_minutes is not None:
                heartbeat.interval_minutes = req.interval_minutes
            if req.enabled is not None:
                heartbeat.enabled = req.enabled
            if req.active_hours is not None:
                heartbeat.active_hours = req.active_hours
            if req.notify_channel is not None:
                heartbeat.notify_channel = req.notify_channel

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail="YAML保存に失敗しました")

            registry.unregister(name)
            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Heartbeat更新エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/{name}")
    async def delete_heartbeat(name: str, request: Request, _=Depends(require_auth)):
        """Heartbeatを削除"""
        try:
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.loader import delete_heartbeat_yaml

            registry = get_heartbeat_registry()
            if name not in registry:
                raise HTTPException(status_code=404, detail=f"Heartbeat '{name}' が見つかりません")

            registry.unregister(name)
            delete_heartbeat_yaml(name)
            return JSONResponse(content={"success": True, "message": f"Heartbeat '{name}' を削除しました"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Heartbeat削除エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{name}/trigger")
    async def trigger_heartbeat(name: str, request: Request, _=Depends(require_auth)):
        """Heartbeatを手動で即時実行"""
        try:
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            result = await runner.trigger(name)
            if result is None:
                raise HTTPException(status_code=404, detail=f"Heartbeat '{name}' が見つかりません")
            return JSONResponse(content={"success": True, "result": result})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Heartbeatトリガーエラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
