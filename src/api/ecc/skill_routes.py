"""スキル拡張（カテゴリ・プリセット・チェーン）ルート。"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from ...memory.database import get_db_session
from ...models.ecc_models import SkillCategory, SkillChain, SkillPreset
from ...skills.loader import save_skill_to_yaml
from ...skills.models import SkillDefinition, SkillTriggerMode
from ...skills.registry import get_skill_registry, register_skill
from ..ecc_helpers import parse_uuid as _parse_uuid
from .schemas import (
    CreateCategoryRequest,
    CreateChainRequest,
    ExecuteChainRequest,
    UpdateChainRequest,
)

logger = logging.getLogger(__name__)


def build_skill_router(require_auth: Callable[..., Any]) -> APIRouter:
    """スキル拡張の APIRouter を構築する。"""

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

    return skill_ext_router
