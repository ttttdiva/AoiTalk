"""ECC (Extended Command Center) API routes."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .ecc_helpers import (
    ecc_cookie_auth_dependency,
    model_to_dict as _model_to_dict,
    parse_date as _parse_date,
    parse_uuid as _parse_uuid,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════
# Pydantic リクエストモデル
# ════════════════════════════════════════════════════

# ── 統合キャラクター ──


class CreateCharacterRequest(BaseModel):
    name: str
    slug: str
    character_type: str = "assistant"
    system_prompt: str = ""
    model: str = ""
    allowed_tools: List[str] = []


class ContextBuildPreviewRequest(BaseModel):
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    message: str = ""
    max_chars: int = Field(default=12000, ge=1000, le=30000)
    is_enabled: bool = True
    # 音声
    voice_engine: str = ""
    voice_name: str = ""
    voice_id: str = ""
    speaker_id: Optional[int] = None
    voice_parameters: dict = {}
    # 性格
    greeting: str = ""
    invalid_content_reply: str = ""
    fallback_reply: str = ""
    goodbye_reply: str = ""
    recognition_aliases: List[str] = []
    # ロールプレイ
    description: str = ""
    personality_summary: str = ""
    first_message: str = ""
    alternate_greetings: List[str] = []
    example_messages: str = ""
    scenario: str = ""
    # 外見・画像生成
    appearance_tags: str = ""
    negative_tags: str = ""
    image_gen_engine: str = ""
    comfyui_config: dict = {}
    avatar_image_path: str = ""


class UpdateCharacterRequest(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    character_type: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    is_enabled: Optional[bool] = None
    # 音声
    voice_engine: Optional[str] = None
    voice_name: Optional[str] = None
    voice_id: Optional[str] = None
    speaker_id: Optional[int] = None
    voice_parameters: Optional[dict] = None
    # 性格
    greeting: Optional[str] = None
    invalid_content_reply: Optional[str] = None
    fallback_reply: Optional[str] = None
    goodbye_reply: Optional[str] = None
    recognition_aliases: Optional[List[str]] = None
    # ロールプレイ
    description: Optional[str] = None
    personality_summary: Optional[str] = None
    first_message: Optional[str] = None
    alternate_greetings: Optional[List[str]] = None
    example_messages: Optional[str] = None
    scenario: Optional[str] = None
    # 外見・画像生成
    appearance_tags: Optional[str] = None
    negative_tags: Optional[str] = None
    image_gen_engine: Optional[str] = None
    comfyui_config: Optional[dict] = None
    avatar_image_path: Optional[str] = None


# ── 5. スキル拡張 ──


class CreateCategoryRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    icon: str = ""
    color: str = ""
    sort_order: int = 0


class CreateChainRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    steps: List[dict]


class UpdateChainRequest(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[List[dict]] = None


class ExecuteChainRequest(BaseModel):
    input: str = ""
    parameters: dict = {}


# ── 7. 品質検証 ──


class VerifyRequest(BaseModel):
    user_input: str
    response: str
    context: Optional[str] = None


class UpdateQualityConfigRequest(BaseModel):
    enabled: bool


# ── ワールドブック ──


class CreateWorldBookRequest(BaseModel):
    scenario_id: Optional[str] = None
    name: str
    description: str = ""
    is_enabled: bool = True


class UpdateWorldBookRequest(BaseModel):
    scenario_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None


class CreateEntryRequest(BaseModel):
    name: str = ""
    keywords: List[str] = []
    secondary_keywords: List[str] = []
    content: str
    is_enabled: bool = True
    priority: int = 0
    case_sensitive: bool = False
    constant: bool = False
    insertion_position: str = "before_scenario"


class UpdateEntryRequest(BaseModel):
    name: Optional[str] = None
    keywords: Optional[List[str]] = None
    secondary_keywords: Optional[List[str]] = None
    content: Optional[str] = None
    is_enabled: Optional[bool] = None
    priority: Optional[int] = None
    case_sensitive: Optional[bool] = None
    constant: Optional[bool] = None
    insertion_position: Optional[str] = None


class LinkCharacterRequest(BaseModel):
    character_id: str


def create_ecc_router(app_instance: Any) -> APIRouter:
    """ECC 全機能の APIRouter を作成する。

    Args:
        app_instance: WebInterface インスタンス。
            認証・DB・MCP プラグイン等へのアクセスに使用する。

    Returns:
        全 ECC ルートを含む APIRouter。
    """

    # ── 認証依存関数 ──
    require_auth = ecc_cookie_auth_dependency(app_instance)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. 統合キャラクター
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
            from ..services.character_service import list_characters

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
            from ..services.character_service import (
                get_character,
                CharacterNotFoundError,
            )

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
            from ..services.character_service import create_character, CharacterError

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
            from ..services.character_service import (
                update_character,
                CharacterNotFoundError,
                CharacterError,
            )

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
            from ..services.character_service import (
                delete_character,
                CharacterNotFoundError,
            )

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
            from ..services.character_service import (
                get_character,
                update_character,
                CharacterNotFoundError,
            )

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
            from ..services.character_card_service import export_character_card_v2
            from ..services.character_service import CharacterNotFoundError

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
            from ..services.character_card_service import export_as_png
            from ..services.character_service import (
                CharacterNotFoundError,
                get_character,
            )
            from fastapi.responses import Response

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
            from ..services.character_card_service import import_character_card_v2
            from ..services.character_service import CharacterError

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

    usage_router = APIRouter(
        prefix="/api/usage",
        tags=["token-usage"],
    )

    @usage_router.get("/dashboard")
    async def get_usage_dashboard(
        request: Request,
        _=Depends(require_auth),
    ):
        """ダッシュボードサマリーを取得（今日 + 7日推移 + 30日モデル別）"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            service = get_token_tracking_service()
            summary = await service.get_dashboard_summary()
            return JSONResponse(content={"success": True, **summary})
        except Exception as e:
            logger.error("使用量ダッシュボード取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @usage_router.get("/daily")
    async def get_usage_daily(
        request: Request,
        start: Optional[str] = Query(None, description="開始日 (YYYY-MM-DD)"),
        end: Optional[str] = Query(None, description="終了日 (YYYY-MM-DD)"),
        _=Depends(require_auth),
    ):
        """日別使用量サマリーを取得"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            _parse_date(start)
            _parse_date(end)
            service = get_token_tracking_service()
            data = await service.get_daily_summary(start, end)
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
        _=Depends(require_auth),
    ):
        """モデル別使用量サマリーを取得"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            _parse_date(start)
            _parse_date(end)
            service = get_token_tracking_service()
            data = await service.get_summary_by_model(start, end)
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
        _=Depends(require_auth),
    ):
        """プロジェクト別使用量サマリーを取得"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            _parse_date(start)
            _parse_date(end)
            service = get_token_tracking_service()
            data = await service.get_summary_by_project(start, end)
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
        _=Depends(require_auth),
    ):
        """エージェント別使用量サマリーを取得"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            _parse_date(start)
            _parse_date(end)
            service = get_token_tracking_service()
            data = await service.get_summary_by_agent(start, end)
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
        _=Depends(require_auth),
    ):
        """指定期間の合計コストを取得"""
        try:
            from ..services.token_tracking_service import get_token_tracking_service

            _parse_date(start)
            _parse_date(end)
            service = get_token_tracking_service()
            data = await service.get_total_cost(start, end)
            return JSONResponse(content={"success": True, "total": data})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("合計使用量取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 4. スキル拡張（カテゴリ・プリセット・チェーン）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    skill_ext_router = APIRouter(
        prefix="/api/skills",
        tags=["skill-extensions"],
    )

    # ── カテゴリ ──

    @skill_ext_router.get("/categories")
    async def list_skill_categories(
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルカテゴリ一覧を取得"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillCategory

            async with await get_db_session() as session:
                stmt = select(SkillCategory).order_by(
                    SkillCategory.sort_order, SkillCategory.name
                )
                result = await session.execute(stmt)
                categories = result.scalars().all()
                return JSONResponse(
                    content={
                        "success": True,
                        "categories": [c.to_dict() for c in categories],
                    }
                )
        except Exception as e:
            logger.error("スキルカテゴリ一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.post("/categories")
    async def create_skill_category(
        req: CreateCategoryRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルカテゴリを作成"""
        try:
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillCategory

            async with await get_db_session() as session:
                category = SkillCategory(
                    name=req.name,
                    display_name=req.display_name,
                    description=req.description,
                    icon=req.icon,
                    color=req.color,
                    sort_order=req.sort_order,
                )
                session.add(category)
                await session.commit()
                await session.refresh(category)
                return JSONResponse(
                    content={"success": True, "category": category.to_dict()},
                    status_code=201,
                )
        except Exception as e:
            logger.error("スキルカテゴリ作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── プリセット ──

    @skill_ext_router.get("/presets")
    async def list_skill_presets(
        request: Request,
        category: Optional[str] = Query(None),
        _=Depends(require_auth),
    ):
        """プリセットスキルライブラリ一覧を取得"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillPreset

            async with await get_db_session() as session:
                stmt = select(SkillPreset).order_by(SkillPreset.name)
                if category:
                    stmt = stmt.where(SkillPreset.category == category)
                result = await session.execute(stmt)
                presets = result.scalars().all()
                return JSONResponse(
                    content={
                        "success": True,
                        "presets": [p.to_dict() for p in presets],
                    }
                )
        except Exception as e:
            logger.error("プリセット一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.post("/presets/install/{preset_id}")
    async def install_skill_preset(
        preset_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """プリセットをアクティブスキルとしてインストール"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillPreset
            from ..skills.models import SkillDefinition, SkillTriggerMode
            from ..skills.registry import get_skill_registry, register_skill
            from ..skills.loader import save_skill_to_yaml

            uid = _parse_uuid(preset_id)

            async with await get_db_session() as session:
                stmt = select(SkillPreset).where(SkillPreset.id == uid)
                result = await session.execute(stmt)
                preset = result.scalar_one_or_none()

                if preset is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"プリセットが見つかりません: {preset_id}",
                    )

                # スキルとしてインストール
                try:
                    trigger_mode = SkillTriggerMode(preset.trigger_mode or "manual")
                except ValueError:
                    trigger_mode = SkillTriggerMode.BOTH

                skill = SkillDefinition(
                    name=preset.name,
                    description=preset.description or "",
                    prompt_template=preset.prompt_template,
                    trigger_mode=trigger_mode,
                    aliases=preset.aliases or [],
                    bound_tools=preset.bound_tools or [],
                    examples=preset.examples or [],
                    tags=preset.tags or [],
                    parameters=preset.parameters or {},
                )

                if not save_skill_to_yaml(skill):
                    raise HTTPException(
                        status_code=500, detail="スキルのYAML保存に失敗しました"
                    )

                register_skill(skill)

                # インストールカウントを増加
                preset.install_count = (preset.install_count or 0) + 1
                await session.commit()

                return JSONResponse(
                    content={
                        "success": True,
                        "message": f"プリセット '{preset.display_name}' をインストールしました",
                        "skill": skill.to_dict(),
                    }
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("プリセットインストールエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── チェーン ──

    @skill_ext_router.get("/chains")
    async def list_skill_chains(
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルチェーン一覧を取得"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillChain

            async with await get_db_session() as session:
                stmt = select(SkillChain).order_by(SkillChain.name)
                result = await session.execute(stmt)
                chains = result.scalars().all()
                return JSONResponse(
                    content={
                        "success": True,
                        "chains": [c.to_dict() for c in chains],
                    }
                )
        except Exception as e:
            logger.error("チェーン一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.post("/chains")
    async def create_skill_chain(
        req: CreateChainRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルチェーンを作成"""
        try:
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillChain

            if not req.steps:
                raise HTTPException(status_code=400, detail="ステップが空です")

            async with await get_db_session() as session:
                chain = SkillChain(
                    name=req.name,
                    display_name=req.display_name,
                    description=req.description,
                    steps=req.steps,
                )
                session.add(chain)
                await session.commit()
                await session.refresh(chain)
                return JSONResponse(
                    content={"success": True, "chain": chain.to_dict()},
                    status_code=201,
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("チェーン作成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.put("/chains/{chain_id}")
    async def update_skill_chain(
        chain_id: str,
        req: UpdateChainRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルチェーンを更新"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillChain

            uid = _parse_uuid(chain_id)

            async with await get_db_session() as session:
                stmt = select(SkillChain).where(SkillChain.id == uid)
                result = await session.execute(stmt)
                chain = result.scalar_one_or_none()

                if chain is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"チェーンが見つかりません: {chain_id}",
                    )

                if req.name is not None:
                    chain.name = req.name
                if req.display_name is not None:
                    chain.display_name = req.display_name
                if req.description is not None:
                    chain.description = req.description
                if req.steps is not None:
                    chain.steps = req.steps

                await session.commit()
                await session.refresh(chain)
                return JSONResponse(content={"success": True, "chain": chain.to_dict()})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("チェーン更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.delete("/chains/{chain_id}")
    async def delete_skill_chain(
        chain_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルチェーンを削除"""
        try:
            from sqlalchemy import select, delete as sa_delete
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillChain

            uid = _parse_uuid(chain_id)

            async with await get_db_session() as session:
                stmt = select(SkillChain).where(SkillChain.id == uid)
                result = await session.execute(stmt)
                chain = result.scalar_one_or_none()

                if chain is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"チェーンが見つかりません: {chain_id}",
                    )

                await session.execute(sa_delete(SkillChain).where(SkillChain.id == uid))
                await session.commit()
                return JSONResponse(
                    content={
                        "success": True,
                        "message": "チェーンを削除しました",
                    }
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("チェーン削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @skill_ext_router.post("/chains/{chain_id}/execute")
    async def execute_skill_chain(
        chain_id: str,
        req: ExecuteChainRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """スキルチェーンを実行"""
        try:
            from sqlalchemy import select
            from ..memory.database import get_db_session
            from ..models.ecc_models import SkillChain
            from ..skills.registry import get_skill_registry

            uid = _parse_uuid(chain_id)

            async with await get_db_session() as session:
                stmt = select(SkillChain).where(SkillChain.id == uid)
                result = await session.execute(stmt)
                chain = result.scalar_one_or_none()

                if chain is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"チェーンが見つかりません: {chain_id}",
                    )

            # チェーンのステップを順番に実行
            registry = get_skill_registry()
            steps = chain.steps or []
            results = []
            current_input = req.input
            current_params = dict(req.parameters)

            for i, step in enumerate(steps):
                skill_name = step.get("skill_name", "")
                skill = registry.get_by_alias(skill_name) or registry.get(skill_name)
                if skill is None:
                    error_action = step.get("on_error", "abort")
                    if error_action == "abort":
                        raise HTTPException(
                            status_code=400,
                            detail=f"ステップ {i + 1} のスキル '{skill_name}' が見つかりません",
                        )
                    results.append(
                        {
                            "step": i + 1,
                            "skill": skill_name,
                            "status": "skipped",
                            "reason": "スキルが見つかりません",
                        }
                    )
                    continue

                # 入力マッピング適用
                input_mapping = step.get("input_mapping", {})
                step_params = dict(current_params)
                for key, source in input_mapping.items():
                    if isinstance(source, str) and source.startswith("$prev."):
                        # 前のステップの出力を参照
                        if results:
                            prev_output = results[-1].get("output", "")
                            step_params[key] = prev_output

                try:
                    rendered = skill.render_prompt(current_input, **step_params)
                    results.append(
                        {
                            "step": i + 1,
                            "skill": skill_name,
                            "status": "completed",
                            "output": rendered,
                        }
                    )
                    current_input = rendered
                except Exception as step_error:
                    error_action = step.get("on_error", "abort")
                    if error_action == "abort":
                        raise HTTPException(
                            status_code=500,
                            detail=f"ステップ {i + 1} 実行エラー: {step_error}",
                        )
                    results.append(
                        {
                            "step": i + 1,
                            "skill": skill_name,
                            "status": "failed",
                            "error": str(step_error),
                        }
                    )

            return JSONResponse(
                content={
                    "success": True,
                    "chain_id": chain_id,
                    "results": results,
                    "final_output": current_input,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("チェーン実行エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 6. MCP管理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    mcp_router = APIRouter(
        prefix="/api/mcp",
        tags=["mcp"],
    )

    def _get_mcp_plugin():
        """app_instance から MCPPlugin を取得する。"""
        plugin = getattr(app_instance, "mcp_plugin", None)
        if plugin is None:
            raise HTTPException(
                status_code=503,
                detail="MCPプラグインが利用できません",
            )
        return plugin

    @mcp_router.get("/servers")
    async def list_mcp_servers(
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバー一覧をステータス付きで取得"""
        try:
            plugin = _get_mcp_plugin()
            server_info = plugin.client.get_server_info()
            servers = []
            for name, info in server_info.items():
                is_connected = name in plugin.client.sessions
                servers.append(
                    {
                        "name": name,
                        "status": "connected" if is_connected else "disconnected",
                        "info": info,
                    }
                )
            return JSONResponse(
                content={
                    "success": True,
                    "servers": servers,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバー一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.get("/servers/{name}/tools")
    async def list_mcp_server_tools(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """指定サーバーのツール一覧を取得"""
        try:
            plugin = _get_mcp_plugin()
            if name not in plugin.client.sessions:
                raise HTTPException(
                    status_code=404,
                    detail=f"MCPサーバー '{name}' が見つかりません",
                )
            tools = await plugin.client.list_tools(server_name=name)
            return JSONResponse(
                content={
                    "success": True,
                    "server": name,
                    "tools": tools.get(name, []),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPツール一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.post("/servers/{name}/toggle")
    async def toggle_mcp_server(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバーの有効/無効を切り替え"""
        try:
            plugin = _get_mcp_plugin()
            if name in plugin.client.sessions:
                # 接続中 → 切断
                await plugin.client.remove_server(name)
                return JSONResponse(
                    content={
                        "success": True,
                        "server": name,
                        "status": "disconnected",
                        "message": f"サーバー '{name}' を切断しました",
                    }
                )
            else:
                # 切断中 → 再接続を試みる
                server_info = plugin.client.servers.get(name)
                if server_info is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"MCPサーバー '{name}' の設定が見つかりません",
                    )
                return JSONResponse(
                    content={
                        "success": False,
                        "server": name,
                        "message": "サーバーの再接続にはリスタートを使用してください",
                    }
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバートグルエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.post("/servers/{name}/restart")
    async def restart_mcp_server(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバーを再起動"""
        try:
            plugin = _get_mcp_plugin()
            # 既存接続を切断
            if name in plugin.client.sessions:
                await plugin.client.remove_server(name)

            # サーバー設定を取得して再接続
            config = getattr(app_instance, "config", None) or {}
            mcp_config = config.get("mcp", {}).get("servers", {})
            server_config = mcp_config.get(name)

            if server_config is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"MCPサーバー '{name}' の設定が見つかりません",
                )

            import platform as pf

            if isinstance(server_config, dict) and (
                "windows" in server_config or "linux" in server_config
            ):
                platform_name = "windows" if pf.system() == "Windows" else "linux"
                if platform_name in server_config:
                    actual_config = dict(server_config[platform_name])
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"プラットフォーム '{platform_name}' の設定がありません",
                    )
            else:
                actual_config = dict(server_config)

            success = await plugin.client.add_server(
                name=name,
                command=actual_config.get("command"),
                args=actual_config.get("args", []),
                env=actual_config.get("env"),
            )

            if success:
                return JSONResponse(
                    content={
                        "success": True,
                        "server": name,
                        "status": "connected",
                        "message": f"サーバー '{name}' を再起動しました",
                    }
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"サーバー '{name}' の再起動に失敗しました",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバー再起動エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.get("/status")
    async def get_mcp_status(
        request: Request,
        _=Depends(require_auth),
    ):
        """MCP全体のヘルスステータスを取得"""
        try:
            plugin = _get_mcp_plugin()
            is_available = plugin.is_available()
            is_initialized = plugin.is_initialized()
            server_info = plugin.client.get_server_info()
            connected_count = len(plugin.client.sessions)
            total_count = len(server_info)

            return JSONResponse(
                content={
                    "success": True,
                    "status": {
                        "available": is_available,
                        "initialized": is_initialized,
                        "total_servers": total_count,
                        "connected_servers": connected_count,
                        "health": (
                            "healthy"
                            if connected_count == total_count and total_count > 0
                            else "degraded" if connected_count > 0 else "unavailable"
                        ),
                    },
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPステータス取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 7. 品質検証
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    quality_router = APIRouter(
        prefix="/api/quality",
        tags=["quality"],
    )

    @quality_router.post("/verify")
    async def verify_response(
        req: VerifyRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """レスポンスの品質を検証"""
        try:
            from ..services.quality_verification_service import (
                QualityVerificationService,
            )

            service = QualityVerificationService()
            report = await service.verify_response(
                user_input=req.user_input,
                response=req.response,
                context=req.context,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "report": report.to_dict(),
                }
            )
        except Exception as e:
            logger.error("品質検証エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @quality_router.get("/config")
    async def get_quality_config(
        request: Request,
        _=Depends(require_auth),
    ):
        """品質検証の設定を取得"""
        try:
            from ..services.quality_verification_service import (
                QualityVerificationService,
            )

            service = QualityVerificationService()
            return JSONResponse(
                content={
                    "success": True,
                    "config": {"enabled": service.enabled},
                }
            )
        except Exception as e:
            logger.error("品質検証設定取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @quality_router.put("/config")
    async def update_quality_config(
        req: UpdateQualityConfigRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """品質検証の設定を更新"""
        try:
            from ..services.quality_verification_service import (
                QualityVerificationService,
            )

            service = QualityVerificationService()
            service.enabled = req.enabled
            return JSONResponse(
                content={
                    "success": True,
                    "config": {"enabled": service.enabled},
                }
            )
        except Exception as e:
            logger.error("品質検証設定更新エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    memory_router = APIRouter(
        prefix="/api/memories",
        tags=["dreaming-memories"],
    )

    @memory_router.get("")
    async def list_dreaming_memories(
        request: Request,
        active_only: bool = Query(False),
        _=Depends(require_auth),
    ):
        """Dreamingメモリ一覧を取得"""
        try:
            from ..services.dreaming_memory_service import list_memories

            user_id = await _get_user_id(request)
            memories = await list_memories(user_id, active_only=active_only)
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
            from ..services.dreaming_memory_service import create_memory

            body = await request.json()
            content = body.get("content", "").strip()
            if not content:
                raise HTTPException(status_code=400, detail="content は必須です")
            user_id = await _get_user_id(request)
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
            from ..services.dreaming_memory_service import update_memory

            body = await request.json()
            user_id = await _get_user_id(request)
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
            from ..services.dreaming_memory_service import delete_all_memories

            user_id = await _get_user_id(request)
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
            from ..services.dreaming_memory_service import delete_memory

            user_id = await _get_user_id(request)
            ok = await delete_memory(memory_id, user_id=user_id)
            if not ok:
                raise HTTPException(status_code=404, detail="メモリが見つかりません")
            return JSONResponse(content={"success": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("メモリ削除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @memory_router.post("/{memory_id}/toggle")
    async def toggle_dreaming_memory(
        memory_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """メモリの有効/無効を切り替え"""
        try:
            from ..services.dreaming_memory_service import toggle_memory

            user_id = await _get_user_id(request)
            mem = await toggle_memory(memory_id, user_id=user_id)
            if mem is None:
                raise HTTPException(status_code=404, detail="メモリが見つかりません")
            return JSONResponse(content={"success": True, "memory": mem})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("メモリトグルエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    context_router = APIRouter(
        prefix="/api/context",
        tags=["context"],
    )

    @context_router.post("/build-preview")
    async def build_context_preview(
        payload: ContextBuildPreviewRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """LLMに渡す統合コンテキストをプレビューする。"""
        try:
            from ..services.context_builder import ContextBuilder

            user_id = payload.user_id or await _get_user_id(request)
            bundle = await ContextBuilder().build_context(
                user_id=user_id,
                message=payload.message,
                project_id=payload.project_id,
                task_id=payload.task_id,
                session_id=payload.session_id,
                max_chars=payload.max_chars,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "context": bundle.render_for_prompt(),
                    "debug": bundle.debug,
                }
            )
        except Exception as e:
            logger.error("コンテキストプレビュー生成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    async def _get_user_id(request: Request) -> str:
        """リクエストからuser_idを取得。認証済みの場合はDBのユーザーID、それ以外はdefault_user。"""
        try:
            user_info = await app_instance._get_user_info_from_request(request)
            if user_info and user_info.get("id"):
                return str(user_info["id"])
        except Exception as e:
            logger.debug("ユーザーID取得失敗（default_userにフォールバック）: %s", e)
        return "default_user"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 10. ワールドブック
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
            from ..services.worldbook_service import list_worldbooks

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
            from ..services.worldbook_service import create_worldbook, WorldBookError

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
            from ..services.worldbook_service import (
                get_worldbook,
                WorldBookNotFoundError,
            )

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
            from ..services.worldbook_service import (
                update_worldbook,
                WorldBookNotFoundError,
                WorldBookError,
            )

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
            from ..services.worldbook_service import (
                delete_worldbook,
                WorldBookNotFoundError,
            )

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
            from ..services.worldbook_service import (
                create_entry,
                WorldBookNotFoundError,
                WorldBookError,
            )

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
            from ..services.worldbook_service import (
                update_entry,
                EntryNotFoundError,
                WorldBookError,
            )

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
            from ..services.worldbook_service import (
                delete_entry,
                EntryNotFoundError,
            )

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
            from ..services.worldbook_service import (
                link_character,
                WorldBookNotFoundError,
                WorldBookError,
            )

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
            from ..services.worldbook_service import (
                unlink_character,
                WorldBookError,
            )

            await unlink_character(worldbook_id, character_id)
            return JSONResponse(
                content={"success": True, "message": "紐づけを解除しました"}
            )
        except WorldBookError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        except Exception as e:
            logger.error("キャラクターリンク解除エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    root_router = APIRouter()
    root_router.include_router(characters_router)
    root_router.include_router(usage_router)
    root_router.include_router(skill_ext_router)
    root_router.include_router(mcp_router)
    root_router.include_router(quality_router)
    root_router.include_router(memory_router)
    root_router.include_router(context_router)
    root_router.include_router(worldbook_router)

    return root_router
