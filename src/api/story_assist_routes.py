"""Story Studio の AI 編集支援 API（story_routes とは分離）。"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..services.story_assist_service import StoryAssistFieldKind, StoryAssistService
from ..services.story_studio import StoryNotFoundError

logger = logging.getLogger(__name__)


class AssistSelectionRequest(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class AssistProposeRequest(BaseModel):
    field_kind: StoryAssistFieldKind
    current_text: str = ""
    instruction: str = Field(min_length=1)
    work_id: UUID | None = None
    episode_id: UUID | None = None
    character_id: UUID | None = None
    rulebook_id: UUID | None = None
    note_id: UUID | None = None
    selection: AssistSelectionRequest | None = None
    include_private_notes: bool = False
    model: dict[str, Any] | None = None


class AssistProposeResponse(BaseModel):
    proposal: str


def create_story_assist_router(
    app_instance: Any | None = None,
    *,
    get_db_manager: Any | None = None,
    get_user_from_request: Any | None = None,
    require_auth_dependency: Any | None = None,
    config: Any | None = None,
    get_llm_client: Any | None = None,
) -> APIRouter:
    if app_instance is not None:
        get_db_manager = get_db_manager or (lambda: app_instance._db_manager)
        get_user_from_request = get_user_from_request or app_instance._get_user_info_from_request
        config = config if config is not None else getattr(app_instance, "config", None)
        get_llm_client = get_llm_client or (lambda: getattr(app_instance, "_llm_client", None))
        if require_auth_dependency is None:
            from .router_helpers import cookie_auth_dependency

            require_auth_dependency = cookie_auth_dependency(app_instance._enforce_cookie_auth)
    if get_db_manager is None or get_user_from_request is None:
        raise TypeError("get_db_manager と get_user_from_request が必要です")
    if require_auth_dependency is None:

        async def require_auth_dependency(_: Request) -> None:  # type: ignore[no-redef]
            return None

    router = APIRouter(prefix="/api/story", tags=["story-assist"])

    async def current_user_id(request: Request) -> UUID:
        result = get_user_from_request(request)
        if inspect.isawaitable(result):
            result = await result
        raw = result.get("id") if isinstance(result, Mapping) else result
        raw = raw or request.headers.get("x-forwarded-user-id")
        if not raw:
            raise HTTPException(status_code=401, detail="ユーザーを特定できません")
        try:
            return UUID(str(raw))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="ユーザーIDが不正です") from exc

    @router.post(
        "/assist/propose",
        response_model=AssistProposeResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def propose_assist(payload: AssistProposeRequest, request: Request) -> AssistProposeResponse:
        user_id = await current_user_id(request)
        session = await get_db_manager().get_session()
        try:
            service = StoryAssistService(
                session,
                get_llm_client() if callable(get_llm_client) else None,
                config=config,
            )
            proposal = await service.propose(
                user_id=user_id,
                work_id=payload.work_id,
                field_kind=payload.field_kind,
                current_text=payload.current_text,
                instruction=payload.instruction,
                model=payload.model,
                episode_id=payload.episode_id,
                character_id=payload.character_id,
                rulebook_id=payload.rulebook_id,
                note_id=payload.note_id,
                selection_start=payload.selection.start if payload.selection else None,
                selection_end=payload.selection.end if payload.selection else None,
                include_private_notes=payload.include_private_notes,
            )
            await session.commit()
            return AssistProposeResponse(proposal=proposal)
        except StoryNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            await session.rollback()
            logger.exception("Story assist proposal failed")
            raise HTTPException(status_code=500, detail="AI編集支援の提案を取得できませんでした") from exc
        finally:
            await session.close()

    return router
