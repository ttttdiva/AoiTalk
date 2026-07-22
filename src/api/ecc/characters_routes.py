"""統合キャラクター管理ルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from ...services.character_card_service import (
    export_as_png,
    export_character_card_v2,
    import_character_card_v2,
)
from ...services.character_service import (
    CharacterError,
    CharacterNotFoundError,
    create_character,
    delete_character,
    get_character,
    list_characters,
    update_character,
)
from .schemas import CreateCharacterRequest, UpdateCharacterRequest

logger = logging.getLogger(__name__)


def build_characters_router(require_auth: Callable[..., Any]) -> APIRouter:
    """統合キャラクター管理の APIRouter を構築する。"""

    characters_router = APIRouter(
        prefix="/api/characters/manage",
        tags=["characters"],
    )

    @characters_router.get("")
    async def list_characters_endpoint(
        request: Request,
        type: Optional[str] = Query(None),
        enabled_only: bool = Query(False),
        _=Depends(require_auth),
    ):
        """キャラクター一覧を取得"""
        try:
            chars = await list_characters(type_filter=type, enabled_only=enabled_only)
            return JSONResponse(content={"success": True, "characters": chars})
        except Exception as e:
            logger.error("キャラクター一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.get("/{character_id}")
    async def get_character_endpoint(
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクター詳細を取得（IDまたはslug）"""
        try:
            char = await get_character(character_id)
            return JSONResponse(content={"success": True, "character": char})
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("キャラクター取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.post("")
    async def create_character_endpoint(
        req: CreateCharacterRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターを作成"""
        try:
            char = await create_character(req.model_dump())
            return JSONResponse(
                content={"success": True, "character": char},
                status_code=201,
            )
        except CharacterError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("キャラクター作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.put("/{character_id}")
    async def update_character_endpoint(
        character_id: str,
        req: UpdateCharacterRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターを更新"""
        try:
            data = {k: v for k, v in req.model_dump().items() if v is not None}
            char = await update_character(character_id, data)
            return JSONResponse(content={"success": True, "character": char})
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except CharacterError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("キャラクター更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.delete("/{character_id}")
    async def delete_character_endpoint(
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターを削除"""
        try:
            await delete_character(character_id)
            return JSONResponse(
                content={"success": True, "message": "キャラクターを削除しました"}
            )
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("キャラクター削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.post("/{character_id}/toggle")
    async def toggle_character_endpoint(
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターの有効/無効を切り替え"""
        try:
            current = await get_character(character_id)
            new_state = not current.get("is_enabled", True)
            updated = await update_character(character_id, {"is_enabled": new_state})
            return JSONResponse(content={"success": True, "character": updated})
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("キャラクタートグルエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── Character Card V2 エクスポート / インポート ──

    @characters_router.get("/{character_id}/export")
    async def export_character_card_v2_endpoint(
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターを Character Card V2 JSON としてエクスポート"""
        try:
            v2_data = await export_character_card_v2(character_id)
            return JSONResponse(content={"success": True, "card": v2_data})
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("CC V2 エクスポートエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.get("/{character_id}/export-png")
    async def export_character_card_v2_png_endpoint(
        character_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """キャラクターを Character Card V2 PNG としてエクスポート"""
        try:
            char = await get_character(character_id)
            png_bytes = await export_as_png(character_id)
            filename = f"{char.get('slug', 'character')}.png"
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        except CharacterNotFoundError as e:
            raise HTTPException(status_code=404, detail=e.message)
        except Exception as e:
            logger.error("CC V2 PNGエクスポートエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @characters_router.post("/import")
    async def import_character_card_v2_endpoint(
        request: Request,
        _=Depends(require_auth),
    ):
        """Character Card V2 をインポート（JSON body または PNG UploadFile）"""
        try:
            content_type = request.headers.get("content-type", "")

            if "multipart/form-data" in content_type:
                # ファイルアップロード（PNG）
                form = await request.form()
                file = form.get("file")
                if not file:
                    raise HTTPException(
                        status_code=400, detail="ファイルが指定されていません"
                    )
                file_bytes = await file.read()
                char = await import_character_card_v2(file_bytes)
            else:
                # JSON body
                body = await request.json()
                char = await import_character_card_v2(body)

            return JSONResponse(
                content={"success": True, "character": char},
                status_code=201,
            )
        except CharacterError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("CC V2 インポートエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return characters_router
