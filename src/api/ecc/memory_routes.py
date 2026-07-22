"""Dreaming メモリルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...services.dreaming_memory_service import (
    create_memory,
    delete_all_memories,
    delete_memory,
    list_memories,
    update_memory,
)

logger = logging.getLogger(__name__)


def build_memory_router(
    require_auth: Callable[..., Any],
    get_user_id: Callable[[Request], Any],
) -> APIRouter:
    """Dreaming メモリの APIRouter を構築する。"""

    memory_router = APIRouter(
        prefix="/api/memories",
        tags=["dreaming-memories"],
    )

    @memory_router.get("")
    async def list_dreaming_memories(
        request: Request,
        _=Depends(require_auth),
    ):
        """Dreamingメモリ一覧を取得"""
        try:
            user_id = await get_user_id(request)
            memories = await list_memories(user_id)
            return JSONResponse(content={"success": True, "memories": memories})
        except Exception as e:
            logger.error("メモリ一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @memory_router.post("")
    async def create_dreaming_memory(
        request: Request,
        _=Depends(require_auth),
    ):
        """メモリを手動作成"""
        try:
            body = await request.json()
            content = body.get("content", "").strip()
            if not content:
                raise HTTPException(status_code=400, detail="content は必須です")
            user_id = await get_user_id(request)
            mem = await create_memory(
                user_id=user_id,
                content=content,
                source_type="manual",
                memory_type=body.get("memory_type", "fact"),
                title=body.get("title"),
                importance=body.get("importance", 7),
            )
            return JSONResponse(
                content={"success": True, "memory": mem}, status_code=201
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("メモリ作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @memory_router.patch("/{memory_id}")
    async def update_dreaming_memory(
        memory_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """メモリを更新"""
        try:
            body = await request.json()
            user_id = await get_user_id(request)
            mem = await update_memory(memory_id, body, user_id=user_id)
            if mem is None:
                raise HTTPException(status_code=404, detail="メモリが見つかりません")
            return JSONResponse(content={"success": True, "memory": mem})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("メモリ更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @memory_router.delete("/all")
    async def delete_all_dreaming_memories(
        request: Request,
        _=Depends(require_auth),
    ):
        """ユーザーの全メモリを削除"""
        try:
            user_id = await get_user_id(request)
            count = await delete_all_memories(user_id)
            return JSONResponse(content={"success": True, "deleted": count})
        except Exception as e:
            logger.error("メモリ全削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @memory_router.delete("/{memory_id}")
    async def delete_dreaming_memory(
        memory_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """メモリを削除"""
        try:
            user_id = await get_user_id(request)
            ok = await delete_memory(memory_id, user_id=user_id)
            if not ok:
                raise HTTPException(status_code=404, detail="メモリが見つかりません")
            return JSONResponse(content={"success": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("メモリ削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return memory_router
