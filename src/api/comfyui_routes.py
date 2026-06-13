"""
ComfyUI Management API Routes
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.comfyui_service import get_comfyui_service

logger = logging.getLogger(__name__)

class ComfyUIConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    url: Optional[str] = None
    default_workflow: Optional[str] = None

class WorkflowSaveRequest(BaseModel):
    name: str
    workflow: dict

def create_comfyui_router(app_instance: Any) -> APIRouter:
    """ComfyUI管理用APIRouterを作成する。"""

    router = APIRouter(prefix="/api/comfyui", tags=["comfyui"])

    def require_auth(request: Request) -> None:
        app_instance._enforce_cookie_auth(request)

    @router.get("/status")
    async def get_status(_: None = Depends(require_auth)):
        """ComfyUIサーバーの接続状態を確認する"""
        enabled = bool(app_instance.config.get("comfyui.enabled", True))
        service = get_comfyui_service(app_instance.config)
        is_available = await service.is_available() if enabled else False
        return JSONResponse({
            "success": True,
            "enabled": enabled,
            "is_available": is_available,
            "url": service.base_url
        })

    @router.get("/workflows")
    async def list_workflows(_: None = Depends(require_auth)):
        """保存されているワークフロー一覧を取得する"""
        service = get_comfyui_service(app_instance.config)
        workflows = await service.list_workflows()
        return JSONResponse({
            "success": True,
            "workflows": workflows
        })

    @router.post("/workflows")
    async def upload_workflow(
        request: Request,
        file: UploadFile = File(None),
        _=Depends(require_auth)
    ):
        """ワークフローをアップロードまたは保存する"""
        service = get_comfyui_service(app_instance.config)
        
        try:
            if file:
                # ファイルアップロードの場合
                content = await file.read()
                name = file.filename
                path = await service.save_workflow(name, content.decode("utf-8"))
            else:
                # JSONボディの場合
                body = await request.json()
                name = body.get("name")
                workflow = body.get("workflow")
                if not name or not workflow:
                    raise HTTPException(status_code=400, detail="name and workflow are required")
                path = await service.save_workflow(name, workflow)
            
            return JSONResponse({
                "success": True,
                "name": name,
                "path": path
            })
        except Exception as e:
            logger.error("Workflow upload error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/workflows/{name}")
    async def delete_workflow(name: str, _: None = Depends(require_auth)):
        """ワークフローを削除する"""
        service = get_comfyui_service(app_instance.config)
        success = await service.delete_workflow(name)
        if not success:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return JSONResponse({"success": True})

    @router.get("/config")
    async def get_config(_: None = Depends(require_auth)):
        """現在のComfyUI設定を取得する"""
        service = get_comfyui_service(app_instance.config)
        return JSONResponse({
            "success": True,
            "enabled": bool(app_instance.config.get("comfyui.enabled", True)),
            "url": service.base_url,
            "default_workflow": service.default_workflow_path
        })

    @router.put("/config")
    async def update_config(payload: ComfyUIConfigUpdate, _: None = Depends(require_auth)):
        """ComfyUI設定を更新し、config.yamlに保存する"""
        service = get_comfyui_service(app_instance.config)
        
        try:
            # サービスの状態を更新
            service.update_config(
                enabled=payload.enabled,
                base_url=payload.url,
                default_workflow_path=payload.default_workflow
            )
            
            # config.yamlに保存
            if payload.enabled is not None:
                app_instance.config.save_to_file("comfyui.enabled", payload.enabled)
            if payload.url is not None:
                app_instance.config.save_to_file("comfyui.url", payload.url)
            if payload.default_workflow is not None:
                app_instance.config.save_to_file("comfyui.default_workflow", payload.default_workflow)
            
            return JSONResponse({
                "success": True,
                "enabled": bool(app_instance.config.get("comfyui.enabled", True)),
                "url": service.base_url,
                "default_workflow": service.default_workflow_path
            })
        except Exception as e:
            logger.error("Config update error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return router
