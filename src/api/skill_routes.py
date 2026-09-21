"""
Skills API Routes

スキルの一覧取得・詳細・作成・更新・削除を提供する REST API。
"""
import logging
from typing import Any, Dict, Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


class CreateSkillProposalRequest(BaseModel):
    operation: str
    target_scope: str
    target_name: str
    project_id: Optional[str] = None
    reason_type: str = "manual"
    proposed_content: Dict[str, Any]
    receipt_id: Optional[str] = None
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    provenance: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None


class UpdateSkillProposalRequest(BaseModel):
    proposed_content: Dict[str, Any]
    provenance: Dict[str, Any] = Field(default_factory=dict)


class RejectSkillProposalRequest(BaseModel):
    reason: Optional[str] = None


def create_skill_router(require_auth, get_current_user=None) -> APIRouter:
    """スキルAPI ルーターを作成

    Args:
        require_auth: 認証依存関数

    Returns:
        APIRouter
    """
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    # ``WebChatServer`` passes its bound ``_get_user_info_from_request`` method.
    # That method intentionally returns no principal in auth-disabled mode,
    # while the rest of the local runtime uses ``default_user``.  Detect only
    # that explicit server state; never treat an authenticated lookup failure
    # as the local administrator principal.
    auth_owner = (
        getattr(get_current_user, "__self__", None)
        if get_current_user is not None
        else None
    )
    auth_explicitly_disabled = getattr(auth_owner, "auth_enabled", None) is False

    async def authorize_project(request: Request, project_id: Optional[str]) -> None:
        """Validate membership before reading any project workspace files."""
        if not project_id:
            return
        try:
            UUID(project_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        user = None
        if get_current_user is not None:
            user = await get_current_user(request)
        if not user:
            state_user = getattr(request.state, "user", None)
            user = state_user if isinstance(state_user, dict) else None
        user_id = (user or {}).get("user_id") or (user or {}).get("id")
        if not user_id:
            raise HTTPException(status_code=403, detail="プロジェクトへのアクセス権がありません")
        from ..services.project_context import ProjectContextResolver
        context = await ProjectContextResolver().get_project_context(project_id, user_id=str(user_id))
        if context is None:
            # Do not reveal whether the project exists to a non-member.
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")

    async def actor_id_for_request(request: Request) -> str:
        user = None
        if get_current_user is not None:
            user = await get_current_user(request)
        if not user:
            state_user = getattr(request.state, "user", None)
            user = state_user if isinstance(state_user, dict) else None
        actor_id = (user or {}).get("user_id") or (user or {}).get("id")
        if not actor_id and auth_explicitly_disabled:
            return "default_user"
        if not actor_id:
            raise HTTPException(status_code=401, detail="認証が必要です")
        return str(actor_id)

    def translate_skill_learning_error(exc: Exception) -> HTTPException:
        from ..services.skill_learning_service import (
            SkillLearningConflict,
            SkillLearningForbidden,
            SkillLearningNotFound,
            SkillLearningStale,
            SkillLearningValidationError,
        )

        if isinstance(exc, SkillLearningStale):
            return HTTPException(
                status_code=409,
                detail={"message": str(exc), "proposal": exc.proposal},
            )
        if isinstance(exc, SkillLearningValidationError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, SkillLearningForbidden):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, SkillLearningNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, SkillLearningConflict):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail="Skill learning operation failed")

    @router.get("")
    async def list_skills(request: Request, project_id: Optional[str] = None, _=Depends(require_auth)):
        """全スキル一覧を取得"""
        try:
            await authorize_project(request, project_id)
            from ..skills.registry import get_skill_registry
            from ..skills.loader import load_project_skills
            registry = get_skill_registry()
            if project_id:
                load_project_skills(project_id)
            skills = [s.to_dict() for s in registry.get_all(project_id)]
            return JSONResponse(content={"success": True, "skills": skills})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル一覧取得エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/proposals")
    async def list_skill_proposals(
        request: Request,
        status: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposals = await SkillLearningService().list_proposals(
                actor_id=actor_id,
                status=status,
                project_id=project_id,
                limit=limit,
            )
            return JSONResponse(content={"success": True, "proposals": proposals})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal list failed")
            raise translate_skill_learning_error(exc) from exc

    @router.post("/proposals")
    async def create_skill_proposal(
        req: CreateSkillProposalRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().create_proposal(
                actor_id=actor_id,
                operation=req.operation,
                target_scope=req.target_scope,
                target_name=req.target_name,
                project_id=req.project_id,
                reason_type=req.reason_type,
                proposed_content=req.proposed_content,
                receipt_id=req.receipt_id,
                evidence=req.evidence,
                provenance=req.provenance,
                idempotency_key=req.idempotency_key,
            )
            return JSONResponse(
                content={"success": True, "proposal": proposal},
                status_code=201,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal create failed")
            raise translate_skill_learning_error(exc) from exc

    @router.get("/proposals/{proposal_id}")
    async def get_skill_proposal(
        proposal_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().get_proposal(
                proposal_id,
                actor_id=actor_id,
            )
            return JSONResponse(content={"success": True, "proposal": proposal})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal get failed")
            raise translate_skill_learning_error(exc) from exc

    @router.put("/proposals/{proposal_id}")
    async def revise_skill_proposal(
        proposal_id: str,
        req: UpdateSkillProposalRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().revise_proposal(
                proposal_id,
                actor_id=actor_id,
                proposed_content=req.proposed_content,
                provenance=req.provenance,
            )
            return JSONResponse(content={"success": True, "proposal": proposal})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal revise failed")
            raise translate_skill_learning_error(exc) from exc

    @router.post("/proposals/{proposal_id}/apply")
    async def apply_skill_proposal(
        proposal_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().apply_proposal(
                proposal_id,
                actor_id=actor_id,
            )
            return JSONResponse(content={"success": True, "proposal": proposal})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal apply failed")
            raise translate_skill_learning_error(exc) from exc

    @router.post("/proposals/{proposal_id}/reject")
    async def reject_skill_proposal(
        proposal_id: str,
        req: RejectSkillProposalRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().reject_proposal(
                proposal_id,
                actor_id=actor_id,
                reason=req.reason,
            )
            return JSONResponse(content={"success": True, "proposal": proposal})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal reject failed")
            raise translate_skill_learning_error(exc) from exc

    @router.post("/proposals/{proposal_id}/rollback")
    async def rollback_skill_proposal(
        proposal_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            proposal = await SkillLearningService().rollback_proposal(
                proposal_id,
                actor_id=actor_id,
            )
            return JSONResponse(content={"success": True, "proposal": proposal})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill proposal rollback failed")
            raise translate_skill_learning_error(exc) from exc

    @router.get("/usage-receipts")
    async def list_skill_usage_receipts(
        request: Request,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        skill_name: Optional[str] = None,
        limit: int = 50,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            receipts = await SkillLearningService().list_receipts(
                actor_id=actor_id,
                session_id=session_id,
                project_id=project_id,
                skill_name=skill_name,
                limit=limit,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "receipts": receipts,
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill usage receipt list failed")
            raise translate_skill_learning_error(exc) from exc

    @router.get("/usage-receipts/{receipt_id}")
    async def get_skill_usage_receipt(
        receipt_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        from ..services.skill_learning_service import SkillLearningService

        actor_id = await actor_id_for_request(request)
        try:
            receipt = await SkillLearningService().get_receipt(
                receipt_id,
                actor_id=actor_id,
            )
            return JSONResponse(content={"success": True, "receipt": receipt})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Skill usage receipt get failed")
            raise translate_skill_learning_error(exc) from exc

    @router.get("/{name}")
    async def get_skill(name: str, request: Request, project_id: Optional[str] = None, _=Depends(require_auth)):
        """スキル詳細を取得"""
        try:
            await authorize_project(request, project_id)
            from ..skills.registry import get_skill_registry
            from ..skills.loader import load_project_skills
            registry = get_skill_registry()
            if project_id:
                load_project_skills(project_id)
            skill = registry.get_by_alias(name, project_id) or registry.get(name, project_id)
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

            if not delete_skill_yaml(name):
                raise HTTPException(status_code=500, detail="YAML削除に失敗しました")

            # Durable deletion succeeded before changing the live registry.
            registry.unregister(name)
            return JSONResponse(content={"success": True, "message": f"スキル '{name}' を削除しました"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"スキル削除エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{name}/test")
    async def test_skill(name: str, request: Request, project_id: Optional[str] = None, _=Depends(require_auth)):
        """スキルのプロンプトテンプレートをテストレンダリング"""
        try:
            await authorize_project(request, project_id)
            from ..skills.registry import get_skill_registry

            from ..skills.loader import load_project_skills
            registry = get_skill_registry()
            if project_id:
                load_project_skills(project_id)
            skill = registry.get_by_alias(name, project_id) or registry.get(name, project_id)
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
