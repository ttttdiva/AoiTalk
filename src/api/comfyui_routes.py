"""
ComfyUI Management API Routes
"""

from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.comfyui_service import (
    get_comfyui_service,
    validate_comfyui_base_url,
    validate_comfyui_connection_url,
)

logger = logging.getLogger(__name__)

class ComfyUIConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    url: Optional[str] = None
    default_workflow: Optional[str] = Field(default=None, max_length=4096)

    @field_validator("default_workflow")
    @classmethod
    def validate_default_workflow(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("default_workflowにcontrol characterは指定できません")
        return value

class WorkflowSaveRequest(BaseModel):
    name: str
    workflow: dict

def create_comfyui_router(app_instance: Any) -> APIRouter:
    """ComfyUI管理用APIRouterを作成する。"""

    router = APIRouter(prefix="/api/comfyui", tags=["comfyui"])

    def require_auth(request: Request) -> None:
        app_instance._enforce_cookie_auth(request)

    async def require_admin(request: Request) -> None:
        app_instance._enforce_cookie_auth(request)
        if not await app_instance._is_admin_user(request):
            raise HTTPException(
                status_code=403,
                detail="Administrator privileges required",
            )

    def comfy_config_value(key: str, default: Any = None) -> Any:
        section = app_instance.config.get("comfyui", {})
        if isinstance(section, Mapping) and key in section:
            return section[key]
        return app_instance.config.get(f"comfyui.{key}", default)

    def restore_config_memory(snapshot: Mapping[str, Any]) -> None:
        setter = getattr(app_instance.config, "set", None)
        if callable(setter):
            setter("comfyui", dict(snapshot))
            return
        section = app_instance.config.get("comfyui", None)
        if isinstance(section, dict):
            section.clear()
            section.update(snapshot)

    def validated_stored_url() -> tuple[str | None, str | None]:
        raw_url = comfy_config_value("url", "http://127.0.0.1:8188")
        try:
            return validate_comfyui_base_url(raw_url), None
        except (TypeError, ValueError):
            logger.error("Stored ComfyUI URL is invalid; outbound status probe was blocked")
            return None, "stored_url_invalid"

    @router.get("/status")
    async def get_status(_: None = Depends(require_auth)):
        """ComfyUIサーバーの接続状態を確認する"""
        enabled = bool(comfy_config_value("enabled", True))
        validated_url, config_error = validated_stored_url()
        if validated_url is None:
            return JSONResponse({
                "success": True,
                "enabled": enabled,
                "is_available": False,
                "url": None,
                "config_valid": False,
                "config_error": config_error,
            })
        service = get_comfyui_service(app_instance.config)
        is_available = await service.is_available() if enabled else False
        return JSONResponse({
            "success": True,
            "enabled": enabled,
            "is_available": is_available,
            "url": validated_url,
            "config_valid": True,
        })

    @router.get("/workflows")
    async def list_workflows(_: None = Depends(require_admin)):
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
        _=Depends(require_admin)
    ):
        """ワークフローをアップロードまたは保存する"""
        service = get_comfyui_service(app_instance.config)
        
        try:
            if file:
                # ファイルアップロードの場合
                content = await file.read()
                name = file.filename
                workflow = await service.save_workflow(name, content.decode("utf-8"))
            else:
                # JSONボディの場合
                body = await request.json()
                name = body.get("name")
                workflow = body.get("workflow")
                if not name or not workflow:
                    raise HTTPException(status_code=400, detail="name and workflow are required")
                workflow = await service.save_workflow(name, workflow)
            
            return JSONResponse({
                "success": True,
                "name": workflow["name"],
                "workflow": workflow,
            })
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Workflow upload error: %s", e)
            raise HTTPException(status_code=500, detail="Workflow upload failed") from e

    @router.delete("/workflows/{name}")
    async def delete_workflow(name: str, _: None = Depends(require_admin)):
        """ワークフローを削除する"""
        service = get_comfyui_service(app_instance.config)
        try:
            success = await service.delete_workflow(name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not success:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return JSONResponse({"success": True})

    @router.get("/config")
    async def get_config(_: None = Depends(require_auth)):
        """現在のComfyUI設定を取得する"""
        validated_url, config_error = validated_stored_url()
        return JSONResponse({
            "success": True,
            "enabled": bool(comfy_config_value("enabled", True)),
            "url": validated_url,
            "default_workflow": comfy_config_value("default_workflow"),
            "config_valid": validated_url is not None,
            "config_error": config_error,
        })

    @router.put("/config")
    async def update_config(payload: ComfyUIConfigUpdate, _: None = Depends(require_admin)):
        """ComfyUI設定を更新し、config.yamlに保存する"""
        try:
            stored_url, _ = validated_stored_url()
            current_section = app_instance.config.get("comfyui", {})
            snapshot = dict(current_section) if isinstance(current_section, Mapping) else {}
            updated = dict(snapshot)
            if payload.url is not None:
                updated["url"] = await validate_comfyui_connection_url(payload.url)
            elif stored_url is None:
                raise ValueError("保存済みComfyUI URLが不正なため、正しいurlを指定してください")
            else:
                updated["url"] = stored_url
            if payload.enabled is not None:
                updated["enabled"] = payload.enabled
            if payload.default_workflow is not None:
                updated["default_workflow"] = payload.default_workflow

            # One DB-backed config write is the atomic persistence boundary.  The
            # singleton is not touched until that write has succeeded.
            if not app_instance.config.save_to_file("comfyui", updated):
                restore_config_memory(snapshot)
                raise RuntimeError("ComfyUI設定を保存できませんでした")
            service = get_comfyui_service(app_instance.config)
            service.update_config(
                enabled=bool(updated.get("enabled", True)),
                base_url=str(updated["url"]),
                default_workflow_path=updated.get("default_workflow"),
            )

            return JSONResponse({
                "success": True,
                "enabled": bool(comfy_config_value("enabled", True)),
                "url": service.base_url,
                "default_workflow": service.default_workflow_path
            })
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Config update error: %s", e)
            raise HTTPException(status_code=500, detail="ComfyUI設定の更新に失敗しました") from e

    return router
