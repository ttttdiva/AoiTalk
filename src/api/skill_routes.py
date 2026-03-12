"""
Skills API Routes

スキルの一覧取得・詳細・作成・更新・削除を提供する REST API。
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CreateSkillRequest(BaseModel):
    """スキル作成リクエスト"""
    name: str
    description: str
    prompt_template: str
    trigger_mode: str = "both"
    aliases: List[str] = []
    bound_tools: List[str] = []
    examples: List[str] = []
    tags: List[str] = []
    parameters: dict = {}


class UpdateSkillRequest(BaseModel):
    """スキル更新リクエスト"""
    description: Optional[str] = None
    prompt_template: Optional[str] = None
    trigger_mode: Optional[str] = None
    aliases: Optional[List[str]] = None
    bound_tools: Optional[List[str]] = None
    examples: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    parameters: Optional[dict] = None


def create_skill_router(require_auth) -> APIRouter:
    """スキルAPI ルーターを作成

    Args:
        require_auth: 認証依存関数

    Returns:
        APIRouter
    """
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(request: Request, _=Depends(require_auth)):
        """全スキル一覧を取得"""
        try:
            from ..skills.registry import get_skill_registry
            registry = get_skill_registry()
            skills = [s.to_dict() for s in registry.get_all()]
            return JSONResponse(content={"success": True, "skills": skills})
        except Exception as e:
            logger.error(f"スキル一覧取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{name}")
    async def get_skill(name: str, request: Request, _=Depends(require_auth)):
        """スキル詳細を取得"""
        try:
            from ..skills.registry import get_skill_registry
            registry = get_skill_registry()
            skill = registry.get_by_alias(name) or registry.get(name)
            if not skill:
                raise HTTPException(status_code=404, detail=f"スキル '{name}' が見つかりません")
            return JSONResponse(content={"success": True, "skill": skill.to_dict()})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("")
    async def create_skill(req: CreateSkillRequest, request: Request, _=Depends(require_auth)):
        """新しいスキルを作成"""
        try:
            from ..skills.models import SkillDefinition, SkillTriggerMode
            from ..skills.registry import get_skill_registry, register_skill
            from ..skills.loader import save_skill_to_yaml

            registry = get_skill_registry()
            if req.name in registry:
                raise HTTPException(status_code=409, detail=f"スキル '{req.name}' は既に存在します")

            try:
                trigger_mode = SkillTriggerMode(req.trigger_mode)
            except ValueError:
                trigger_mode = SkillTriggerMode.BOTH

            skill = SkillDefinition(
                name=req.name,
                description=req.description,
                prompt_template=req.prompt_template,
                trigger_mode=trigger_mode,
                aliases=req.aliases,
                bound_tools=req.bound_tools,
                examples=req.examples,
                tags=req.tags,
                parameters=req.parameters,
            )

            if not save_skill_to_yaml(skill):
                raise HTTPException(status_code=500, detail="YAML保存に失敗しました")

            register_skill(skill)
            return JSONResponse(content={"success": True, "skill": skill.to_dict()}, status_code=201)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル作成エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{name}")
    async def update_skill(name: str, req: UpdateSkillRequest, request: Request, _=Depends(require_auth)):
        """スキルを更新"""
        try:
            from ..skills.models import SkillTriggerMode
            from ..skills.registry import get_skill_registry, register_skill
            from ..skills.loader import save_skill_to_yaml

            registry = get_skill_registry()
            skill = registry.get(name)
            if not skill:
                raise HTTPException(status_code=404, detail=f"スキル '{name}' が見つかりません")

            # 提供されたフィールドのみ更新
            if req.description is not None:
                skill.description = req.description
            if req.prompt_template is not None:
                skill.prompt_template = req.prompt_template
            if req.trigger_mode is not None:
                try:
                    skill.trigger_mode = SkillTriggerMode(req.trigger_mode)
                except ValueError:
                    pass
            if req.aliases is not None:
                skill.aliases = req.aliases
            if req.bound_tools is not None:
                skill.bound_tools = req.bound_tools
            if req.examples is not None:
                skill.examples = req.examples
            if req.tags is not None:
                skill.tags = req.tags
            if req.parameters is not None:
                skill.parameters = req.parameters

            if not save_skill_to_yaml(skill):
                raise HTTPException(status_code=500, detail="YAML保存に失敗しました")

            # レジストリ再登録（エイリアス更新のため）
            registry.unregister(name)
            register_skill(skill)
            return JSONResponse(content={"success": True, "skill": skill.to_dict()})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル更新エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/{name}")
    async def delete_skill(name: str, request: Request, _=Depends(require_auth)):
        """スキルを削除"""
        try:
            from ..skills.registry import get_skill_registry
            from ..skills.loader import delete_skill_yaml

            registry = get_skill_registry()
            if name not in registry:
                raise HTTPException(status_code=404, detail=f"スキル '{name}' が見つかりません")

            registry.unregister(name)
            delete_skill_yaml(name)
            return JSONResponse(content={"success": True, "message": f"スキル '{name}' を削除しました"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル削除エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{name}/test")
    async def test_skill(name: str, request: Request, _=Depends(require_auth)):
        """スキルのプロンプトテンプレートをテストレンダリング"""
        try:
            from ..skills.registry import get_skill_registry

            registry = get_skill_registry()
            skill = registry.get_by_alias(name) or registry.get(name)
            if not skill:
                raise HTTPException(status_code=404, detail=f"スキル '{name}' が見つかりません")

            body = await request.json()
            input_text = body.get("input", "テスト入力")
            params = body.get("parameters", {})

            rendered = skill.render_prompt(input_text, **params)
            return JSONResponse(content={"success": True, "rendered": rendered})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキルテストエラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
