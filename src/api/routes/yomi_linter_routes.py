"""Yomi Linter状態、共通読み辞書、未解決候補API。"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..router_helpers import cookie_auth_dependency
from ...tts.yomi_linter import get_yomi_preflight_service
from ...tts.yomi_linter.repository import YomiRepository


class DictionaryCreate(BaseModel):
    surface: str = Field(min_length=1, max_length=255)
    reading: str = Field(min_length=1, max_length=255)
    accent_type: Optional[int] = Field(default=None, ge=0)
    enabled: bool = True
    target_tts: List[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)


class DictionaryUpdate(BaseModel):
    surface: Optional[str] = Field(default=None, min_length=1, max_length=255)
    reading: Optional[str] = Field(default=None, min_length=1, max_length=255)
    accent_type: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None
    target_tts: Optional[List[str]] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class CandidateUpdate(BaseModel):
    status: str


def _validate_reading(reading: str) -> str:
    value = reading.strip()
    if not re.fullmatch(r"[ァ-ヴー・]+", value):
        raise HTTPException(status_code=400, detail="読みは全角カタカナで入力してください")
    return value


def _config_dict(config: Any) -> Dict[str, Any]:
    return config.config if hasattr(config, "config") else config


def register_yomi_linter_routes(app: FastAPI, server: Any) -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    repository = YomiRepository()

    async def require_admin(request: Request) -> None:
        server._enforce_cookie_auth(request)
        if not await server._is_admin_user(request):
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )

    @app.get("/api/tts/yomi-linter/status")
    async def get_status(_: None = Depends(require_auth)):
        return get_yomi_preflight_service().status(_config_dict(server.config))

    @app.get("/api/tts/yomi-dictionary")
    async def list_dictionary(_: None = Depends(require_admin)):
        try:
            return {"items": await asyncio.to_thread(repository.list_dictionary)}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/tts/yomi-dictionary", status_code=201)
    async def create_dictionary(payload: DictionaryCreate, _: None = Depends(require_admin)):
        data = payload.model_dump()
        data["surface"] = data["surface"].strip()
        data["reading"] = _validate_reading(data["reading"])
        data["target_tts"] = sorted({str(item).lower() for item in data["target_tts"]})
        try:
            return await asyncio.to_thread(repository.create_dictionary, data)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.patch("/api/tts/yomi-dictionary/{entry_id}")
    async def update_dictionary(entry_id: str, payload: DictionaryUpdate, _: None = Depends(require_admin)):
        data = payload.model_dump(exclude_unset=True)
        if "surface" in data:
            data["surface"] = data["surface"].strip()
        if "reading" in data:
            data["reading"] = _validate_reading(data["reading"])
        if "target_tts" in data:
            data["target_tts"] = sorted({str(item).lower() for item in data["target_tts"]})
        try:
            result = await asyncio.to_thread(repository.update_dictionary, entry_id, data)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="不正なIDです") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="辞書項目が見つかりません")
        return result

    @app.delete("/api/tts/yomi-dictionary/{entry_id}", status_code=204)
    async def delete_dictionary(entry_id: str, _: None = Depends(require_admin)):
        try:
            deleted = await asyncio.to_thread(repository.delete_dictionary, entry_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="不正なIDです") from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="辞書項目が見つかりません")

    @app.get("/api/tts/yomi-candidates")
    async def list_candidates(
        status: Optional[str] = Query(default="unresolved"),
        _: None = Depends(require_admin),
    ):
        return {"items": await asyncio.to_thread(repository.list_candidates, status)}

    @app.patch("/api/tts/yomi-candidates/{candidate_id}")
    async def update_candidate(candidate_id: str, payload: CandidateUpdate, _: None = Depends(require_admin)):
        if payload.status not in {"unresolved", "resolved", "ignored"}:
            raise HTTPException(status_code=400, detail="不正な状態です")
        try:
            result = await asyncio.to_thread(
                repository.update_candidate, candidate_id, payload.status
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="不正なIDです") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="候補が見つかりません")
        return result
