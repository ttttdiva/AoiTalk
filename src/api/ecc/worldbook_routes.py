"""ワールドブックルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...services.worldbook_service import (
    EntryNotFoundError,
    WorldBookError,
    WorldBookNotFoundError,
    create_entry,
    create_worldbook,
    delete_entry,
    delete_worldbook,
    get_worldbook,
    link_character,
    list_worldbooks,
    unlink_character,
    update_entry,
    update_worldbook,
)
from .schemas import (
    CreateEntryRequest,
    CreateWorldBookRequest,
    LinkCharacterRequest,
    UpdateEntryRequest,
    UpdateWorldBookRequest,
)

logger = logging.getLogger(__name__)


def build_worldbook_router(require_auth: Callable[..., Any]) -> APIRouter:
    """ワールドブックの APIRouter を構築する。"""

    worldbook_router = APIRouter(
        prefix="/api/worldbooks",
        tags=["worldbooks"],
    )

    @worldbook_router.get("")
    async def list_worldbooks_endpoint(
        request: Request,
        scenario_id: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """ワールドブック一覧を取得"""
        try:
            books = await list_worldbooks(scenario_id=scenario_id)
            return JSONResponse(content={"success": True, "worldbooks": books})
        except Exception as e:
            logger.error("ワールドブック一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.post("")
    async def create_worldbook_endpoint(
        req: CreateWorldBookRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """ワールドブックを作成"""
        try:
            wb = await create_worldbook(req.model_dump())
            return JSONResponse(
                content={"success": True, "worldbook": wb},
                status_code=201,
            )
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("ワールドブック作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.get("/{worldbook_id}")
    async def get_worldbook_endpoint(
        worldbook_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """ワールドブック詳細を取得（エントリ含む）"""
        try:
            wb = await get_worldbook(worldbook_id)
            return JSONResponse(content={"success": True, "worldbook": wb})
        except WorldBookNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("ワールドブック取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.put("/{worldbook_id}")
    async def update_worldbook_endpoint(
        worldbook_id: str,
        req: UpdateWorldBookRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """ワールドブックを更新"""
        try:
            data = {k: v for k, v in req.model_dump().items() if v is not None}
            wb = await update_worldbook(worldbook_id, data)
            return JSONResponse(content={"success": True, "worldbook": wb})
        except WorldBookNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("ワールドブック更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.delete("/{worldbook_id}")
    async def delete_worldbook_endpoint(
        worldbook_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """ワールドブックを削除"""
        try:
            await delete_worldbook(worldbook_id)
            return JSONResponse(
                content={"success": True, "message": "ワールドブックを削除しました"}
            )
        except WorldBookNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("ワールドブック削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── ワールドブック エントリ ──

    @worldbook_router.post("/{worldbook_id}/entries")
    async def create_entry_endpoint(
        worldbook_id: str,
        req: CreateEntryRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """ワールドブックにエントリを追加"""
        try:
            entry = await create_entry(worldbook_id, req.model_dump())
            return JSONResponse(
                content={"success": True, "entry": entry},
                status_code=201,
            )
        except WorldBookNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("エントリ作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.put("/entries/{entry_id}")
    async def update_entry_endpoint(
        entry_id: str,
        req: UpdateEntryRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """エントリを更新"""
        try:
            data = {k: v for k, v in req.model_dump().items() if v is not None}
            entry = await update_entry(entry_id, data)
            return JSONResponse(content={"success": True, "entry": entry})
        except EntryNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("エントリ更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.delete("/entries/{entry_id}")
    async def delete_entry_endpoint(
        entry_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """エントリを削除"""
        try:
            await delete_entry(entry_id)
            return JSONResponse(
                content={"success": True, "message": "エントリを削除しました"}
            )
        except EntryNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("エントリ削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── ワールドブック キャラクターリンク ──

    @worldbook_router.post("/{worldbook_id}/link")
    async def link_character_endpoint(
        worldbook_id: str,
        req: LinkCharacterRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターとワールドブックを紐づける"""
        try:
            link = await link_character(worldbook_id, req.character_id)
            return JSONResponse(
                content={"success": True, "link": link},
                status_code=201,
            )
        except WorldBookNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("キャラクターリンクエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @worldbook_router.delete("/{worldbook_id}/link/{character_id}")
    async def unlink_character_endpoint(
        worldbook_id: str,
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターとワールドブックの紐づけを解除"""
        try:
            await unlink_character(worldbook_id, character_id)
            return JSONResponse(
                content={"success": True, "message": "紐づけを解除しました"}
            )
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("キャラクターリンク解除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return worldbook_router
