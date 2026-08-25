"""Scenario Studio の正本 API。

全パスは ``/api/story`` 配下に置き、GET は参照だけを行う。旧 scenario / Docs
API との同期はこの router からは行わない。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from ..memory.models import ConversationSession
from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryEpisodeRevision,
    StoryGenerationJob,
    StoryIllustration,
    StoryLink,
    StoryNote,
    StoryRulebook,
    StoryWork,
    StoryWorkCharacter,
    StoryWorkRulebook,
    StoryWritingSession,
)
from ..services.story_studio import (
    STORY_AI_REVISION_ORIGINS,
    StoryConflictError,
    StoryEpisodeService,
    StoryGraphError,
    StoryGraphService,
    StoryJobRunner,
    StoryJobExecutor,
    StoryNotFoundError,
    StoryRevisionAuthor,
    StoryRevisionOrigin,
    StoryRevisionService,
    StorySearchService,
    StoryModelResolver,
    StorySummaryService,
    StoryWorkService,
    build_story_context,
    resolve_story_route,
    story_user_choices,
)

logger = logging.getLogger(__name__)


class WorkCreateRequest(BaseModel):
    title: str
    synopsis: str | None = None
    plot: str | None = None
    style_guide: str | None = None
    kind: str = "novel"
    status: str = "planning"
    target_episode_chars: int = 6000
    planned_episode_count: int | None = None
    ui_state: dict[str, Any] = Field(default_factory=dict)
    model_override: dict[str, Any] = Field(default_factory=dict)
    image_settings: dict[str, Any] | None = None


class WorkPatchRequest(BaseModel):
    title: str | None = None
    synopsis: str | None = None
    plot: str | None = None
    style_guide: str | None = None
    status: str | None = None
    target_episode_chars: int | None = None
    planned_episode_count: int | None = None
    start_episode_id: UUID | None = None
    ui_state: dict[str, Any] | None = None
    model_override: dict[str, Any] | None = None
    image_settings: dict[str, Any] | None = None


class EpisodeCreateRequest(BaseModel):
    title: str
    plot: str | None = None
    body: str = ""
    summary: str | None = None
    premise_note: str | None = None
    status: str = "unwritten"
    target_chars: int | None = None
    sort_hint: float = 0.0
    after_episode_id: UUID | None = None
    choice_label: str | None = None


class EpisodePatchRequest(BaseModel):
    title: str | None = None
    plot: str | None = None
    summary: str | None = None
    premise_note: str | None = None
    status: str | None = None
    target_chars: int | None = None
    map_x: float | None = None
    map_y: float | None = None
    sort_hint: float | None = None


class BodyUpdateRequest(BaseModel):
    body: str
    expected_etag: str
    commit: bool = True
    message: str | None = None
    # §6.2 の origin / created_by 語彙。未指定は従来どおり manual / user。
    # 語彙外は pydantic が 422 を返す。
    origin: StoryRevisionOrigin | None = None
    created_by: StoryRevisionAuthor | None = None


class SplitRequest(BaseModel):
    offset: int = Field(ge=0)
    new_title: str
    expected_etag: str


class StructureRequest(BaseModel):
    ops: list[dict[str, Any]] = Field(default_factory=list)


class CheckpointRequest(BaseModel):
    message: str = Field(min_length=1)


class RestoreRequest(BaseModel):
    rev_no: int = Field(ge=1)


class RestoreArchivedRequest(BaseModel):
    restore_token: dict[str, Any] | None = None


class CharacterRequest(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    description: str | None = None
    notes: str | None = None
    ai_mode: str = "keyword"
    keywords: list[str] = Field(default_factory=list)


class CharacterPatchRequest(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    summary: str | None = None
    description: str | None = None
    notes: str | None = None
    ai_mode: str | None = None
    keywords: list[str] | None = None
    image_path: str | None = None


class WorkCharacterEntry(BaseModel):
    character_id: UUID
    role_note: str | None = None
    position: float = 0.0


class RulebookRequest(BaseModel):
    name: str
    content: str | None = None


class RulebookPatchRequest(BaseModel):
    name: str | None = None
    content: str | None = None


class WorkRulebookEntry(BaseModel):
    rulebook_id: UUID
    enabled: bool = True
    position: float = 0.0


class NoteRequest(BaseModel):
    title: str
    content: str | None = None
    ai_mode: str = "keyword"
    keywords: list[str] = Field(default_factory=list)
    position: float = 0.0


class NotePatchRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    ai_mode: str | None = None
    keywords: list[str] | None = None
    position: float | None = None


class JobRequest(BaseModel):
    episode_id: UUID | None = None
    episode_ids: list[UUID] = Field(default_factory=list)
    episode_count: int | None = Field(default=None, ge=1, le=500)
    instruction: str | None = None
    model: dict[str, Any] | None = None
    mode: str | None = None


class ComposeApplyRequest(BaseModel):
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)


class WriteRequest(BaseModel):
    episode_id: UUID | None = None
    conversation_session_id: UUID | None = None


class WritingSessionResponse(BaseModel):
    id: UUID
    work_id: UUID
    episode_id: UUID | None = None
    conversation_session_id: UUID | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WritingSessionPatchRequest(BaseModel):
    episode_id: UUID | None = None


class StoryResponseBase(BaseModel):
    """Story API の JSON DTO 共通基底。

    サービス層は辞書を返すが、公開 API の形状はここで固定する。将来の
    集計項目を後方互換に追加できるよう、読み取り DTO に限って余剰キーを
    許可する。
    """

    model_config = ConfigDict(extra="allow")


class StoryWorkResponse(StoryResponseBase):
    id: UUID
    user_id: UUID
    title: str
    synopsis: str | None = None
    plot: str | None = None
    style_guide: str | None = None
    kind: str
    status: str
    target_episode_chars: int
    planned_episode_count: int | None = None
    start_episode_id: UUID | None = None
    ui_state: dict[str, Any] = Field(default_factory=dict)
    model_override: dict[str, Any] = Field(default_factory=dict)
    image_settings: dict[str, Any] = Field(default_factory=dict)
    resolved_model: str | None = None
    model_layer: str | None = None
    episode_count: int | None = None
    char_count: int | None = None
    # §4.4 左レールのバッジ用。overview だけが埋め、一覧・単体取得では null。
    total_chars: int | None = None
    notes_count: int | None = None
    characters_count: int | None = None
    rulebooks_count: int | None = None
    branch_count: int | None = None
    route_episode_count: int | None = None
    route_chars: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None


class StoryEpisodeResponse(StoryResponseBase):
    id: UUID
    work_id: UUID
    title: str
    plot: str | None = None
    summary: str | None = None
    summary_locked: bool = False
    premise_note: str | None = None
    status: str
    target_chars: int | None = None
    char_count: int = 0
    body: str | None = None
    body_etag: str | None = None
    map_x: float | None = None
    map_y: float | None = None
    sort_hint: float = 0.0
    current_rev_no: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None


class StoryLinkResponse(StoryResponseBase):
    id: UUID
    work_id: UUID
    from_episode_id: UUID
    to_episode_id: UUID
    choice_label: str | None = None
    position: float = 0.0
    is_primary: bool = False
    created_at: str | None = None


class StoryGraphResponse(StoryResponseBase):
    episodes: list[StoryEpisodeResponse] = Field(default_factory=list)
    links: list[StoryLinkResponse] = Field(default_factory=list)
    start_episode_id: UUID | None = None


class StoryStructureResponse(StoryGraphResponse):
    results: list[dict[str, Any]] = Field(default_factory=list)


class StoryCharacterResponse(StoryResponseBase):
    id: UUID
    user_id: UUID
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    description: str | None = None
    notes: str | None = None
    ai_mode: str
    keywords: list[str] = Field(default_factory=list)
    image_path: str | None = None
    role_note: str | None = None
    position: float | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None


class StoryWorkCharacterResponse(StoryResponseBase):
    work_id: UUID
    character_id: UUID
    role_note: str | None = None
    position: float = 0.0


class StoryRulebookResponse(StoryResponseBase):
    id: UUID
    user_id: UUID
    name: str
    content: str | None = None
    enabled: bool | None = None
    position: float | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None


class StoryWorkRulebookResponse(StoryResponseBase):
    work_id: UUID
    rulebook_id: UUID
    enabled: bool = True
    position: float = 0.0


class StoryNoteResponse(StoryResponseBase):
    id: UUID
    work_id: UUID
    title: str
    content: str | None = None
    ai_mode: str
    keywords: list[str] = Field(default_factory=list)
    position: float = 0.0
    created_at: str | None = None
    updated_at: str | None = None


class StoryRevisionResponse(StoryResponseBase):
    id: UUID
    episode_id: UUID
    rev_no: int
    title: str | None = None
    plot: str | None = None
    body: str | None = None
    message: str | None = None
    origin: str
    body_sha256: str
    char_count: int = 0
    created_by: str
    created_at: str | None = None


class StoryRevisionListResponse(StoryResponseBase):
    items: list[StoryRevisionResponse] = Field(default_factory=list)
    limit: int
    offset: int


class StoryBodyUpdateResponse(StoryResponseBase):
    id: UUID
    body_etag: str | None = None
    char_count: int = 0
    current_rev_no: int = 0
    revision: StoryRevisionResponse | None = None
    # AI 適用時に積んだ直前状態（§6.2 pre_ai）。ジョブ結果と同じキー名で返す。
    pre_revision: StoryRevisionResponse | None = None


class StoryRestoreResponse(StoryResponseBase):
    episode: StoryEpisodeResponse
    pre_restore: StoryRevisionResponse
    restore: StoryRevisionResponse


class StoryContextResponse(StoryResponseBase):
    prompt: str
    injected: list[dict[str, str]] = Field(default_factory=list)
    model: dict[str, str] = Field(default_factory=dict)
    resolved_model: str | None = None
    model_layer: str | None = None
    estimated_chars: int = 0


class StoryJobResponse(StoryResponseBase):
    id: UUID
    work_id: UUID
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class StoryIllustrationResponse(StoryResponseBase):
    id: UUID
    work_id: UUID
    episode_id: UUID
    body_etag: str
    rev_no: int | None = None
    anchor_kind: str | None = None
    anchor_quote: str
    offset_hint: int | None = None
    ordering: int
    scene_description: str | None = None
    visual_prompt: str | None = None
    status: str
    generated_media_id: UUID | None = None
    error_message: str | None = None
    resolved_index: int | None = None
    stale: bool | None = None
    image_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class StoryIllustrationListResponse(StoryResponseBase):
    active: list[StoryIllustrationResponse] = Field(default_factory=list)
    stale: list[StoryIllustrationResponse] = Field(default_factory=list)


class StoryOverviewResponse(StoryResponseBase):
    work: StoryWorkResponse
    graph: StoryGraphResponse
    current_route: list[UUID] = Field(default_factory=list)


class StoryComposeApplyResponse(StoryResponseBase):
    episodes: list[StoryEpisodeResponse] = Field(default_factory=list)
    graph: StoryGraphResponse | StoryStructureResponse


class StoryDeleteResponse(StoryResponseBase):
    id: UUID
    archived_at: str | None = None
    deleted: bool | None = None
    restore_token: dict[str, Any] | None = None


class StorySearchHitResponse(StoryResponseBase):
    episode_id: UUID
    title: str
    snippet: str
    field: str | None = None
    match_start: int | None = None
    match_end: int | None = None


class StorySearchResponse(StoryResponseBase):
    query: str
    results: list[StorySearchHitResponse] = Field(default_factory=list)


class StoryRestoreArchivedResponse(StoryResponseBase):
    id: UUID
    archived_at: str | None = None


class StorySplitResponse(StoryResponseBase):
    source: dict[str, Any] = Field(default_factory=dict)
    created: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)


def _config_get(config: Any, path: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        try:
            value = config.get(path, default)
            if value != default:
                return value
        except TypeError:
            pass
    current = config.config if hasattr(config, "config") else config
    if not isinstance(current, Mapping):
        return default
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return default
        current = current.get(part)
    return default if current is None else current


def _llm_spec(client: Any) -> dict[str, Any]:
    if client is None:
        return {}
    aliases = {
        "provider": ("provider", "provider_label"),
        "model": ("model", "model_name"),
        "base_url": ("base_url",),
        "api_key_ref": ("api_key_ref", "credential_profile"),
        "reasoning_effort": ("reasoning_effort", "effort"),
    }
    result: dict[str, Any] = {}
    for target, names in aliases.items():
        for name in names:
            if hasattr(client, name):
                value = getattr(client, name)
                if value not in (None, ""):
                    result[target] = value
                    break
    return result


def create_story_router(
    app_instance: Any | None = None,
    *,
    get_db_manager: Any | None = None,
    get_user_from_request: Any | None = None,
    require_auth_dependency: Any | None = None,
    config: Any | None = None,
    get_llm_client: Any | None = None,
) -> APIRouter:
    """WebChatServer と単体テストの双方から作れる router factory。"""

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
    router = APIRouter(prefix="/api/story", tags=["story"])

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

    async def open_session():
        session = await get_db_manager().get_session()
        return session

    def fail(exc: Exception) -> None:
        if isinstance(exc, StoryNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, StoryConflictError):
            # rollback 済みでも 409 を返せるよう、例外が控えた値だけを使う。
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "current_etag": exc.current_etag,
                    "updated_at": exc.updated_at,
                    "updated_by": exc.updated_by,
                },
            ) from exc
        if isinstance(exc, StoryGraphError):
            raise HTTPException(status_code=400, detail={"message": str(exc), "op_index": exc.op_index}) from exc
        raise exc

    async def owner_work(session: Any, work_id: UUID, user_id: UUID):
        try:
            return await StoryWorkService(session).get(work_id, user_id)
        except Exception as exc:
            fail(exc)

    async def owner_episode(session: Any, episode_id: UUID, user_id: UUID):
        try:
            return await StoryEpisodeService(session).get(episode_id, user_id)
        except Exception as exc:
            fail(exc)

    async def owner_archived_episode(session: Any, episode_id: UUID, user_id: UUID):
        try:
            return await StoryEpisodeService(session).get_archived(episode_id, user_id)
        except Exception as exc:
            fail(exc)

    def model_for(work: Any, runtime: Mapping[str, Any] | None = None) -> dict[str, str]:
        writing = _config_get(config, "model_routing.classes.writing", {}) or {}
        client = get_llm_client() if callable(get_llm_client) else None
        return StoryModelResolver.resolve(
            runtime,
            work.model_override or {},
            writing,
            _llm_spec(client),
        )

    def model_metadata_for(work: Any, runtime: Mapping[str, Any] | None = None) -> dict[str, Any]:
        resolved = model_for(work, runtime)
        provider = str(resolved.get("provider") or "").strip()
        model = str(resolved.get("model") or "").strip()
        return {
            "resolved_model": "/".join(item for item in (provider, model) if item) or None,
            "model_layer": resolved.get("layer") or None,
        }

    def work_response(work: Any, *, episode_count: int | None = None, char_count: int | None = None) -> dict[str, Any]:
        return work.to_dict(
            episode_count=episode_count,
            char_count=char_count,
            **model_metadata_for(work),
        )

    async def run_job_background(job_id: UUID) -> None:
        session = await open_session()
        try:
            client = get_llm_client() if callable(get_llm_client) else None
            await StoryJobExecutor(session, client, config=config).run(job_id)
        except Exception:
            logger.exception("Story background job 起動に失敗しました: %s", job_id)
            try:
                await session.rollback()
                job = await session.scalar(
                    select(StoryGenerationJob)
                    .where(StoryGenerationJob.id == job_id)
                    .with_for_update()
                )
                if job is not None and job.status in {"queued", "running"}:
                    await StoryJobRunner(session).transition(
                        job,
                        "error",
                        error="interrupted: background job startup failed",
                    )
                    await session.commit()
            except Exception:
                logger.exception("Story background job rollback に失敗しました: %s", job_id)
        finally:
            await session.close()

    async def run_summary_background(episode_id: UUID, work_id: UUID) -> None:
        """§8.4 の自動要約。失敗しても本文保存は取り消さない（ログのみ）。"""

        session = await open_session()
        service: StorySummaryService | None = None
        try:
            work = await session.get(StoryWork, work_id)
            client = get_llm_client() if callable(get_llm_client) else None
            service = StorySummaryService(session, client, config=config)
            await service.generate(episode_id, model=model_for(work) if work is not None else None)
        except Exception:
            logger.warning("Story 要約の自動生成に失敗しました: %s", episode_id, exc_info=True)
            try:
                await session.rollback()
            except Exception:
                logger.warning("Story 要約の rollback に失敗しました: %s", episode_id, exc_info=True)
        finally:
            if service is not None:
                await service.aclose()
            await session.close()

    @router.get("/works", response_model=list[StoryWorkResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_works(request: Request):
        user_id = await current_user_id(request)
        session = await open_session()
        try:
            return await StoryWorkService(session).list(user_id, response_metadata=model_metadata_for)
        finally:
            await session.close()

    @router.post("/works", response_model=StoryWorkResponse, dependencies=[Depends(require_auth_dependency)])
    async def create_work(payload: WorkCreateRequest, request: Request):
        user_id = await current_user_id(request)
        session = await open_session()
        try:
            work = await StoryWorkService(session).create(user_id, payload.model_dump())
            await session.commit()
            return work_response(work, episode_count=0, char_count=0)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.get("/works/{work_id}", response_model=StoryWorkResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_work(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            return work_response(work)
        finally:
            await session.close()

    @router.patch("/works/{work_id}", response_model=StoryWorkResponse, dependencies=[Depends(require_auth_dependency)])
    async def patch_work(work_id: UUID, payload: WorkPatchRequest, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            data = payload.model_dump(exclude_unset=True)
            if "model_override" in data:
                from ..services.story_studio import _clean_model_spec

                data["model_override"] = _clean_model_spec(data["model_override"])
            if data.get("start_episode_id") is not None:
                # §7.1 単一開始点。structure op の set_start と同じ所有チェックを課し、
                # 他作品のエピソードを開始点にできないようにする。
                start_episode = await session.get(StoryEpisode, data["start_episode_id"])
                if (
                    start_episode is None
                    or start_episode.work_id != work.id
                    or start_episode.archived_at is not None
                ):
                    raise StoryGraphError("開始エピソードが見つかりません")
            for key, value in data.items():
                if key == "ui_state" and isinstance(value, dict):
                    # UI updates are partial: route selection must not discard
                    # the map viewport or other persisted workspace state.
                    work.ui_state = {**(work.ui_state or {}), **value}
                elif key == "image_settings" and isinstance(value, dict):
                    from ..services.story_illustration_service import normalize_image_settings

                    work.image_settings = normalize_image_settings(
                        {**(work.image_settings or {}), **value}
                    )
                else:
                    setattr(work, key, value)
            await session.commit()
            return work_response(work)
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.delete("/works/{work_id}", response_model=StoryDeleteResponse, dependencies=[Depends(require_auth_dependency)])
    async def archive_work(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            from datetime import datetime

            work.archived_at = datetime.utcnow()
            await session.commit()
            return {"id": str(work.id), "archived_at": work.archived_at.isoformat()}
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get("/works/{work_id}/overview", response_model=StoryOverviewResponse, dependencies=[Depends(require_auth_dependency)])
    async def work_overview(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            graph = await StoryGraphService(session).graph(work)
            route = resolve_story_route(work.start_episode_id, graph["links"], story_user_choices(work.ui_state))
            route_ids = set(route)
            route_episodes = [item for item in graph["episodes"] if item["id"] in route_ids]
            total_chars = sum(int(item.get("char_count") or 0) for item in graph["episodes"])
            # §4.4 の左レールバッジ。GET なので集計は読み取りだけで完結させる。
            outgoing: dict[str, int] = {}
            for link in graph["links"]:
                key = str(link.get("from_episode_id"))
                outgoing[key] = outgoing.get(key, 0) + 1
            notes_count = await session.scalar(
                select(func.count()).select_from(StoryNote).where(StoryNote.work_id == work.id)
            )
            characters_count = await session.scalar(
                select(func.count()).select_from(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id)
            )
            rulebooks_count = await session.scalar(
                select(func.count())
                .select_from(StoryWorkRulebook)
                .where(StoryWorkRulebook.work_id == work.id, StoryWorkRulebook.enabled.is_(True))
            )
            work_dto = work_response(
                work,
                episode_count=len(graph["episodes"]),
                char_count=total_chars,
            )
            work_dto.update(
                {
                    "total_chars": total_chars,
                    "notes_count": int(notes_count or 0),
                    "characters_count": int(characters_count or 0),
                    "rulebooks_count": int(rulebooks_count or 0),
                    "branch_count": sum(1 for value in outgoing.values() if value >= 2),
                    "route_episode_count": len(route),
                    "route_chars": sum(int(item.get("char_count") or 0) for item in route_episodes),
                }
            )
            return {
                "work": work_dto,
                "graph": graph,
                "current_route": route,
            }
        finally:
            await session.close()

    @router.get(
        "/works/{work_id}/export",
        response_class=Response,
        responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}}},
        dependencies=[Depends(require_auth_dependency)],
    )
    async def export_work(
        work_id: UUID,
        request: Request,
        scope: Literal["route", "all"] = Query("route"),
        format: Literal["txt"] = Query("txt"),
    ):
        if format != "txt" or scope not in {"route", "all"}:
            raise HTTPException(status_code=400, detail="format=txt と scope=route|all のみ対応しています")
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            episodes = await StoryEpisodeService(session).list(work)
            links = list((await session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
            if scope == "route":
                ids = set(resolve_story_route(work.start_episode_id, links, story_user_choices(work.ui_state)))
                episodes = [episode for episode in episodes if str(episode.id) in ids]
            episodes.sort(key=lambda item: (item.sort_hint, item.created_at))
            body = "\n\n".join(f"# {item.title}\n\n{item.body or ''}" for item in episodes)
            return Response(content=body, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="story-{work.id}.txt"'})
        finally:
            await session.close()

    @router.get("/works/{work_id}/search", response_model=StorySearchResponse, dependencies=[Depends(require_auth_dependency)])
    async def search_work(work_id: UUID, request: Request, q: str = Query("", min_length=0)):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            results = await StorySearchService(session).search(work, q)
            return {"query": q, "results": results}
        finally:
            await session.close()

    @router.get("/works/{work_id}/episodes", response_model=list[StoryEpisodeResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_episodes(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            return [episode.to_dict(include_body=False) for episode in await StoryEpisodeService(session).list(work)]
        finally:
            await session.close()

    @router.post("/works/{work_id}/episodes", response_model=StoryEpisodeResponse, dependencies=[Depends(require_auth_dependency)])
    async def create_episode(work_id: UUID, payload: EpisodeCreateRequest, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            episode = await StoryEpisodeService(session).create(work, payload.model_dump(), after_episode_id=payload.after_episode_id)
            await session.commit()
            return episode.to_dict()
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get("/episodes/{episode_id}", response_model=StoryEpisodeResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_episode(episode_id: UUID, request: Request):
        session = await open_session()
        try:
            return (await owner_episode(session, episode_id, await current_user_id(request))).to_dict()
        finally:
            await session.close()

    @router.patch("/episodes/{episode_id}", response_model=StoryEpisodeResponse, dependencies=[Depends(require_auth_dependency)])
    async def patch_episode(episode_id: UUID, payload: EpisodePatchRequest, request: Request):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            episode = await StoryEpisodeService(session).update_meta(episode, payload.model_dump(exclude_unset=True))
            await session.commit()
            return episode.to_dict()
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.put("/episodes/{episode_id}/body", response_model=StoryBodyUpdateResponse, dependencies=[Depends(require_auth_dependency)])
    async def put_episode_body(episode_id: UUID, payload: BodyUpdateRequest, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            revision_service = StoryRevisionService(session)
            pre_revision = None
            if payload.commit and payload.origin in STORY_AI_REVISION_ORIGINS:
                # §6.2: AI 適用の直前は、未保存差分がある場合だけ pre_ai を積む。
                pre_revision = await revision_service.ensure_pre_ai(episode)
            revision = await revision_service.update_body(
                episode,
                payload.body,
                expected_etag=payload.expected_etag,
                commit=payload.commit,
                message=payload.message,
                origin=payload.origin or "manual",
                created_by=payload.created_by or "user",
            )
            summary_target = (
                revision is not None
                and not bool(episode.summary_locked)
                and bool(payload.body.strip())
            )
            work_id = episode.work_id
            await session.commit()
            if summary_target:
                # §8.4 要約は非同期。リビジョンが積まれた保存だけを対象にして、
                # 2 秒ごとのオートセーブで LLM を叩き続けないようにする。
                background_tasks.add_task(run_summary_background, episode_id, work_id)
            from ..services.story_illustration_service import StoryIllustrationService

            illustration_service = StoryIllustrationService(session, config=config)
            await illustration_service.resolve_episode_anchors(episode)
            await session.commit()
            return {
                "id": str(episode.id),
                "body_etag": episode.body_etag,
                "char_count": episode.char_count,
                "current_rev_no": episode.current_rev_no,
                "revision": revision.to_dict() if revision else None,
                "pre_revision": pre_revision.to_dict() if pre_revision else None,
            }
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.delete("/episodes/{episode_id}", response_model=StoryDeleteResponse, dependencies=[Depends(require_auth_dependency)])
    async def delete_episode(episode_id: UUID, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            episode = await owner_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            deleted = await StoryEpisodeService(session).delete(work, episode)
            await session.commit()
            return {
                "id": str(episode.id),
                "archived_at": episode.archived_at.isoformat(),
                "restore_token": deleted.get("restore_token"),
            }
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post("/episodes/{episode_id}/split", response_model=StorySplitResponse, dependencies=[Depends(require_auth_dependency)])
    async def split_episode(episode_id: UUID, payload: SplitRequest, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            episode = await owner_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            result = await StoryEpisodeService(session).split(work, episode, offset=payload.offset, new_title=payload.new_title, expected_etag=payload.expected_etag)
            await session.commit()
            return result
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get("/works/{work_id}/graph", response_model=StoryGraphResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_graph(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            return await StoryGraphService(session).graph(work)
        finally:
            await session.close()

    @router.post("/works/{work_id}/structure", response_model=StoryStructureResponse, dependencies=[Depends(require_auth_dependency)])
    async def post_structure(work_id: UUID, payload: StructureRequest, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            result = await StoryGraphService(session).apply(work, payload.ops)
            await session.commit()
            return result
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get("/episodes/{episode_id}/revisions", response_model=StoryRevisionListResponse, dependencies=[Depends(require_auth_dependency)])
    async def list_revisions(episode_id: UUID, request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            rows = list((await session.scalars(select(StoryEpisodeRevision).where(StoryEpisodeRevision.episode_id == episode.id).order_by(StoryEpisodeRevision.rev_no.desc()).offset(offset).limit(limit))).all())
            return {"items": [row.to_dict() for row in rows], "limit": limit, "offset": offset}
        finally:
            await session.close()

    @router.get("/episodes/{episode_id}/revisions/{rev_no}", response_model=StoryRevisionResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_revision(episode_id: UUID, rev_no: int, request: Request):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            row = await session.scalar(select(StoryEpisodeRevision).where(StoryEpisodeRevision.episode_id == episode.id, StoryEpisodeRevision.rev_no == rev_no))
            if row is None:
                raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
            return row.to_dict(include_body=True)
        finally:
            await session.close()

    @router.post("/episodes/{episode_id}/checkpoint", response_model=StoryRevisionResponse, dependencies=[Depends(require_auth_dependency)])
    async def checkpoint(episode_id: UUID, payload: CheckpointRequest, request: Request):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            row = await StoryRevisionService(session).checkpoint(episode, message=payload.message)
            await session.commit()
            return row.to_dict(include_body=True)
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post("/episodes/{episode_id}/restore", response_model=StoryRestoreResponse, dependencies=[Depends(require_auth_dependency)])
    async def restore(episode_id: UUID, payload: RestoreRequest, request: Request):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            row = await session.scalar(select(StoryEpisodeRevision).where(StoryEpisodeRevision.episode_id == episode.id, StoryEpisodeRevision.rev_no == payload.rev_no))
            if row is None:
                raise HTTPException(status_code=404, detail="リビジョンが見つかりません")
            before, restored = await StoryRevisionService(session).restore(episode, row)
            await session.commit()
            return {"episode": episode.to_dict(), "pre_restore": before.to_dict(), "restore": restored.to_dict()}
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post(
        "/episodes/{episode_id}/restore-archived",
        response_model=StoryRestoreArchivedResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def restore_archived_episode(episode_id: UUID, payload: RestoreArchivedRequest, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            episode = await owner_archived_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            try:
                await StoryEpisodeService(session).restore_archived(
                    work,
                    episode,
                    restore_token=payload.restore_token,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except StoryGraphError as exc:
                raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
            await session.commit()
            return {"id": episode.id, "archived_at": None}
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get("/characters", response_model=list[StoryCharacterResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_characters(request: Request):
        session = await open_session()
        try:
            from ..services.story_character_fields import normalize_character_dict

            user_id = await current_user_id(request)
            rows = list((await session.scalars(select(StoryCharacter).where(StoryCharacter.user_id == user_id, StoryCharacter.archived_at.is_(None)).order_by(StoryCharacter.name))).all())
            return [normalize_character_dict(row.to_dict()) for row in rows]
        finally:
            await session.close()

    @router.post("/characters", response_model=StoryCharacterResponse, dependencies=[Depends(require_auth_dependency)])
    async def create_character(payload: CharacterRequest, request: Request):
        session = await open_session()
        try:
            from ..services.story_character_fields import (
                apply_character_field_normalization,
                normalize_character_dict,
            )

            aliases, keywords = apply_character_field_normalization(
                payload.name,
                payload.aliases,
                payload.keywords,
            )
            row = StoryCharacter(
                user_id=await current_user_id(request),
                name=payload.name,
                aliases=aliases,
                summary=payload.summary,
                description=payload.description,
                notes=payload.notes,
                ai_mode=payload.ai_mode,
                keywords=keywords,
            )
            session.add(row)
            await session.commit()
            return normalize_character_dict(row.to_dict())
        finally:
            await session.close()

    @router.get("/characters/{character_id}", response_model=StoryCharacterResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_character(character_id: UUID, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryCharacter).where(StoryCharacter.id == character_id, StoryCharacter.user_id == await current_user_id(request), StoryCharacter.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="登場人物が見つかりません")
            from ..services.story_character_fields import normalize_character_dict

            return normalize_character_dict(row.to_dict())
        finally:
            await session.close()

    @router.patch("/characters/{character_id}", response_model=StoryCharacterResponse, dependencies=[Depends(require_auth_dependency)])
    async def patch_character(character_id: UUID, payload: CharacterPatchRequest, request: Request):
        session = await open_session()
        try:
            from ..services.story_character_fields import (
                apply_character_field_normalization,
                normalize_character_dict,
            )

            row = await session.scalar(select(StoryCharacter).where(StoryCharacter.id == character_id, StoryCharacter.user_id == await current_user_id(request), StoryCharacter.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="登場人物が見つかりません")
            patch = payload.model_dump(exclude_unset=True)
            next_name = patch.get("name", row.name)
            if "aliases" in patch or "keywords" in patch:
                aliases, keywords = apply_character_field_normalization(
                    next_name,
                    patch.get("aliases", row.aliases),
                    patch.get("keywords", row.keywords),
                )
                patch["aliases"] = aliases
                patch["keywords"] = keywords
            for key, value in patch.items():
                setattr(row, key, value)
            await session.commit()
            return normalize_character_dict(row.to_dict())
        finally:
            await session.close()

    @router.delete("/characters/{character_id}", response_model=StoryDeleteResponse, dependencies=[Depends(require_auth_dependency)])
    async def delete_character(character_id: UUID, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryCharacter).where(StoryCharacter.id == character_id, StoryCharacter.user_id == await current_user_id(request), StoryCharacter.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="登場人物が見つかりません")
            from datetime import datetime

            row.archived_at = datetime.utcnow()
            await session.commit()
            return {"id": str(row.id), "archived_at": row.archived_at.isoformat()}
        finally:
            await session.close()

    @router.get("/works/{work_id}/characters", response_model=list[StoryCharacterResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_work_characters(work_id: UUID, request: Request):
        session = await open_session()
        try:
            from ..services.story_character_fields import normalize_character_dict

            work = await owner_work(session, work_id, await current_user_id(request))
            joins = list((await session.scalars(select(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id).order_by(StoryWorkCharacter.position))).all())
            chars = {row.id: row for row in (await session.scalars(select(StoryCharacter).where(StoryCharacter.id.in_([join.character_id for join in joins])))).all()}
            return [
                normalize_character_dict({**chars[join.character_id].to_dict(), **join.to_dict()})
                for join in joins
                if join.character_id in chars
            ]
        finally:
            await session.close()

    @router.put("/works/{work_id}/characters", response_model=list[StoryCharacterResponse], dependencies=[Depends(require_auth_dependency)])
    async def replace_work_characters(work_id: UUID, payload: list[WorkCharacterEntry], request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            rows = list((await session.scalars(select(StoryCharacter).where(StoryCharacter.id.in_([item.character_id for item in payload]), StoryCharacter.user_id == work.user_id, StoryCharacter.archived_at.is_(None)))).all())
            valid = {row.id for row in rows}
            if valid != {item.character_id for item in payload}:
                raise HTTPException(status_code=400, detail="作品外または存在しない登場人物です")
            await session.execute(delete(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id))
            session.add_all([StoryWorkCharacter(work_id=work.id, **item.model_dump()) for item in payload])
            await session.commit()
            return await list_work_characters(work_id, request)
        finally:
            await session.close()

    @router.get("/rulebooks", response_model=list[StoryRulebookResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_rulebooks(request: Request):
        session = await open_session()
        try:
            rows = list((await session.scalars(select(StoryRulebook).where(StoryRulebook.user_id == await current_user_id(request), StoryRulebook.archived_at.is_(None)).order_by(StoryRulebook.name))).all())
            return [row.to_dict() for row in rows]
        finally:
            await session.close()

    @router.post("/rulebooks", response_model=StoryRulebookResponse, dependencies=[Depends(require_auth_dependency)])
    async def create_rulebook(payload: RulebookRequest, request: Request):
        session = await open_session()
        try:
            row = StoryRulebook(user_id=await current_user_id(request), **payload.model_dump())
            session.add(row)
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    @router.get("/rulebooks/{rulebook_id}", response_model=StoryRulebookResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_rulebook(rulebook_id: UUID, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryRulebook).where(StoryRulebook.id == rulebook_id, StoryRulebook.user_id == await current_user_id(request), StoryRulebook.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="ルールブックが見つかりません")
            return row.to_dict()
        finally:
            await session.close()

    @router.patch("/rulebooks/{rulebook_id}", response_model=StoryRulebookResponse, dependencies=[Depends(require_auth_dependency)])
    async def patch_rulebook(rulebook_id: UUID, payload: RulebookPatchRequest, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryRulebook).where(StoryRulebook.id == rulebook_id, StoryRulebook.user_id == await current_user_id(request), StoryRulebook.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="ルールブックが見つかりません")
            for key, value in payload.model_dump(exclude_unset=True).items():
                setattr(row, key, value)
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    @router.delete("/rulebooks/{rulebook_id}", response_model=StoryDeleteResponse, dependencies=[Depends(require_auth_dependency)])
    async def delete_rulebook(rulebook_id: UUID, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryRulebook).where(StoryRulebook.id == rulebook_id, StoryRulebook.user_id == await current_user_id(request), StoryRulebook.archived_at.is_(None)))
            if row is None:
                raise HTTPException(status_code=404, detail="ルールブックが見つかりません")
            from datetime import datetime

            row.archived_at = datetime.utcnow()
            await session.commit()
            return {"id": str(row.id), "archived_at": row.archived_at.isoformat()}
        finally:
            await session.close()

    @router.get("/works/{work_id}/rulebooks", response_model=list[StoryRulebookResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_work_rulebooks(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            joins = list((await session.scalars(select(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id).order_by(StoryWorkRulebook.position))).all())
            books = {row.id: row for row in (await session.scalars(select(StoryRulebook).where(StoryRulebook.id.in_([join.rulebook_id for join in joins])))).all()}
            return [{**books[join.rulebook_id].to_dict(), **join.to_dict()} for join in joins if join.rulebook_id in books]
        finally:
            await session.close()

    @router.put("/works/{work_id}/rulebooks", response_model=list[StoryWorkRulebookResponse], dependencies=[Depends(require_auth_dependency)])
    async def replace_work_rulebooks(work_id: UUID, payload: list[WorkRulebookEntry], request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            valid = {row.id for row in (await session.scalars(select(StoryRulebook).where(StoryRulebook.id.in_([item.rulebook_id for item in payload]), StoryRulebook.user_id == work.user_id, StoryRulebook.archived_at.is_(None)))).all()}
            if valid != {item.rulebook_id for item in payload}:
                raise HTTPException(status_code=400, detail="作品外または存在しないルールブックです")
            await session.execute(delete(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id))
            session.add_all([StoryWorkRulebook(work_id=work.id, **item.model_dump()) for item in payload])
            await session.commit()
            joins = list((await session.scalars(select(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id).order_by(StoryWorkRulebook.position))).all())
            return [join.to_dict() for join in joins]
        finally:
            await session.close()

    @router.get("/works/{work_id}/notes", response_model=list[StoryNoteResponse], dependencies=[Depends(require_auth_dependency)])
    async def list_notes(work_id: UUID, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            return [row.to_dict() for row in (await session.scalars(select(StoryNote).where(StoryNote.work_id == work.id).order_by(StoryNote.position, StoryNote.created_at))).all()]
        finally:
            await session.close()

    @router.post("/works/{work_id}/notes", response_model=StoryNoteResponse, dependencies=[Depends(require_auth_dependency)])
    async def create_note(work_id: UUID, payload: NoteRequest, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            row = StoryNote(work_id=work.id, **payload.model_dump())
            session.add(row)
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    @router.patch("/notes/{note_id}", response_model=StoryNoteResponse, dependencies=[Depends(require_auth_dependency)])
    async def patch_note(note_id: UUID, payload: NotePatchRequest, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryNote).join(StoryWork).where(StoryNote.id == note_id, StoryWork.user_id == await current_user_id(request)))
            if row is None:
                raise HTTPException(status_code=404, detail="設定資料が見つかりません")
            for key, value in payload.model_dump(exclude_unset=True).items():
                setattr(row, key, value)
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    @router.delete("/notes/{note_id}", response_model=StoryDeleteResponse, dependencies=[Depends(require_auth_dependency)])
    async def delete_note(note_id: UUID, request: Request):
        session = await open_session()
        try:
            row = await session.scalar(select(StoryNote).join(StoryWork).where(StoryNote.id == note_id, StoryWork.user_id == await current_user_id(request)))
            if row is None:
                raise HTTPException(status_code=404, detail="設定資料が見つかりません")
            await session.delete(row)
            await session.commit()
            return {"id": str(note_id), "deleted": True}
        finally:
            await session.close()

    async def preview_context(session: Any, episode: Any, work: Any, runtime: Mapping[str, Any] | None = None):
        episodes = await StoryEpisodeService(session).list(work)
        links = list((await session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
        route_ids = resolve_story_route(work.start_episode_id, links, story_user_choices(work.ui_state))
        route_map = {str(item.id): item for item in episodes}
        route = [route_map[item] for item in route_ids if item in route_map]
        joins = list((await session.scalars(select(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id))).all())
        chars = list((await session.scalars(select(StoryCharacter).where(StoryCharacter.id.in_([join.character_id for join in joins])))).all()) if joins else []
        book_joins = list((await session.scalars(select(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id))).all())
        books = list((await session.scalars(select(StoryRulebook).where(StoryRulebook.id.in_([join.rulebook_id for join in book_joins])))).all()) if book_joins else []
        notes = list((await session.scalars(select(StoryNote).where(StoryNote.work_id == work.id))).all())
        model = model_for(work, runtime)
        return build_story_context(work, episode, route, characters=chars, work_characters=joins, rulebooks=books, work_rulebooks=book_joins, notes=notes, links=links, model=model)

    @router.get("/episodes/{episode_id}/context-preview", response_model=StoryContextResponse, dependencies=[Depends(require_auth_dependency)])
    async def context_preview(episode_id: UUID, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            episode = await owner_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            return (await preview_context(session, episode, work)).to_dict()
        finally:
            await session.close()

    @router.post("/works/{work_id}/compose", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def compose(work_id: UUID, payload: JobRequest, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            job = await StoryJobRunner(session).create(work, kind="compose", payload=payload.model_dump(mode="json"), model=model_for(work, payload.model))
            await session.commit()
            background_tasks.add_task(run_job_background, job.id)
            return job.to_dict()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post("/works/{work_id}/compose/apply", response_model=StoryComposeApplyResponse, dependencies=[Depends(require_auth_dependency)])
    async def compose_apply(work_id: UUID, payload: ComposeApplyRequest, request: Request):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            episode_service = StoryEpisodeService(session)
            created = []
            aliases: dict[str, str] = {}
            for index, item in enumerate(payload.episodes):
                created.append(await episode_service.create(work, item))
                created_id = str(created[-1].id)
                aliases[str(index)] = created_id
                for key in ("id", "episode_id", "key", "slug", "title"):
                    value = item.get(key)
                    if value not in (None, ""):
                        aliases[str(value)] = created_id
            graph = StoryGraphService(session)
            ops = []
            for link in payload.links:
                source = str(link.get("from", link.get("from_episode_id", "")))
                target = str(link.get("to", link.get("to_episode_id", "")))
                op: dict[str, Any] = {
                    "op": "add_link",
                    "from": aliases.get(source, source),
                    "to": aliases.get(target, target),
                    "choice_label": link.get("choice_label"),
                }
                if link.get("is_primary") is not None:
                    op["is_primary"] = bool(link["is_primary"])
                ops.append(op)
            result = await graph.apply(work, ops) if ops else await graph.graph(work)
            await session.commit()
            return {"episodes": [item.to_dict() for item in created], "graph": result}
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get(
        "/episodes/{episode_id}/illustrations",
        response_model=StoryIllustrationListResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def list_episode_illustrations(episode_id: UUID, request: Request):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            work = await owner_work(session, episode.work_id, await current_user_id(request))
            from ..services.story_illustration_service import StoryIllustrationService

            service = StoryIllustrationService(session, config=config)
            return await service.list_for_episode(episode)
        finally:
            await session.close()

    @router.post(
        "/episodes/{episode_id}/illustrations/generate",
        response_model=StoryIllustrationListResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def generate_episode_illustrations(
        episode_id: UUID,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            episode = await owner_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            from ..services.story_illustration_service import (
                StoryIllustrationService,
                is_image_settings_enabled,
                run_episode_illustrations_background,
            )

            if not is_image_settings_enabled(work.image_settings):
                raise HTTPException(status_code=409, detail="挿絵が無効な作品です")

            service = StoryIllustrationService(session, config=config)
            client = get_llm_client() if callable(get_llm_client) else None
            background_tasks.add_task(
                run_episode_illustrations_background,
                episode_id=episode.id,
                work_id=work.id,
                model=model_for(work),
                config=config,
                llm_client=client,
            )
            return await service.list_for_episode(episode)
        except HTTPException:
            raise
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post(
        "/illustrations/{illustration_id}/regenerate",
        response_model=StoryIllustrationResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def regenerate_illustration(illustration_id: UUID, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            row = await session.get(StoryIllustration, illustration_id)
            if row is None:
                raise HTTPException(status_code=404, detail="挿絵が見つかりません")
            work = await owner_work(session, row.work_id, user_id)
            from ..services.story_illustration_service import StoryIllustrationService

            service = StoryIllustrationService(session, config=config)
            item = await service.regenerate(work, illustration_id)
            if item is None:
                raise HTTPException(status_code=404, detail="挿絵が見つかりません")
            await session.commit()
            return item
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.delete(
        "/illustrations/{illustration_id}",
        response_model=StoryDeleteResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def delete_illustration(illustration_id: UUID, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            row = await session.get(StoryIllustration, illustration_id)
            if row is None:
                raise HTTPException(status_code=404, detail="挿絵が見つかりません")
            work = await owner_work(session, row.work_id, user_id)
            from ..services.story_illustration_service import StoryIllustrationService

            service = StoryIllustrationService(session, config=config)
            deleted = await service.delete(work, illustration_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="挿絵が見つかりません")
            await session.commit()
            return {"id": str(illustration_id), "archived_at": None}
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post("/episodes/{episode_id}/generate", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def generate(episode_id: UUID, payload: JobRequest, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            work = await owner_work(session, episode.work_id, await current_user_id(request))
            job = await StoryJobRunner(session).create(work, kind="generate", payload={**payload.model_dump(mode="json"), "episode_id": str(episode.id)}, model=model_for(work, payload.model))
            await session.commit()
            background_tasks.add_task(run_job_background, job.id)
            return job.to_dict()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post("/episodes/{episode_id}/revise", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def revise(episode_id: UUID, payload: JobRequest, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            episode = await owner_episode(session, episode_id, await current_user_id(request))
            work = await owner_work(session, episode.work_id, await current_user_id(request))
            job = await StoryJobRunner(session).create(work, kind="revise", payload={**payload.model_dump(mode="json"), "episode_id": str(episode.id)}, model=model_for(work, payload.model))
            await session.commit()
            background_tasks.add_task(run_job_background, job.id)
            return job.to_dict()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.post(
        "/episodes/{episode_id}/summary/regenerate",
        response_model=StoryEpisodeResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def regenerate_summary(episode_id: UUID, request: Request):
        """§8.4 の明示再生成。summary_locked を false に戻して要約を作り直す。"""

        session = await open_session()
        service: StorySummaryService | None = None
        try:
            user_id = await current_user_id(request)
            episode = await owner_episode(session, episode_id, user_id)
            work = await owner_work(session, episode.work_id, user_id)
            client = get_llm_client() if callable(get_llm_client) else None
            service = StorySummaryService(session, client, config=config)
            updated = await service.generate(episode.id, model=model_for(work), force=True)
            if updated is None:
                raise HTTPException(status_code=502, detail="要約を生成できませんでした")
            return updated.to_dict()
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            if service is not None:
                await service.aclose()
            await session.close()

    @router.post("/works/{work_id}/batch-generate", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def batch_generate(work_id: UUID, payload: JobRequest, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            work = await owner_work(session, work_id, await current_user_id(request))
            ids = payload.episode_ids
            job = await StoryJobRunner(session).create(work, kind="batch", payload={**payload.model_dump(mode="json"), "episode_ids": [str(item) for item in ids], "progress": {"total": len(ids), "completed": 0, "items": [{"episode_id": str(item), "state": "pending"} for item in ids]}}, model=model_for(work, payload.model))
            await session.commit()
            background_tasks.add_task(run_job_background, job.id)
            return job.to_dict()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.get("/jobs/{job_id}", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def get_job(job_id: UUID, request: Request):
        session = await open_session()
        try:
            job = await session.scalar(select(StoryGenerationJob).join(StoryGenerationJob.work).where(StoryGenerationJob.id == job_id, StoryGenerationJob.work.has(user_id=await current_user_id(request))))
            if job is None:
                raise HTTPException(status_code=404, detail="ジョブが見つかりません")
            return job.to_dict()
        finally:
            await session.close()

    @router.post("/jobs/{job_id}/cancel", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def cancel_job(job_id: UUID, request: Request):
        session = await open_session()
        try:
            job = await session.scalar(select(StoryGenerationJob).join(StoryGenerationJob.work).where(StoryGenerationJob.id == job_id, StoryGenerationJob.work.has(user_id=await current_user_id(request))).with_for_update())
            if job is None:
                raise HTTPException(status_code=404, detail="ジョブが見つかりません")
            await StoryJobRunner(session).cancel(job)
            await session.commit()
            return job.to_dict()
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.post("/jobs/{job_id}/resume", response_model=StoryJobResponse, dependencies=[Depends(require_auth_dependency)])
    async def resume_job(job_id: UUID, request: Request, background_tasks: BackgroundTasks):
        session = await open_session()
        try:
            job = await session.scalar(
                select(StoryGenerationJob)
                .join(StoryGenerationJob.work)
                .where(
                    StoryGenerationJob.id == job_id,
                    StoryGenerationJob.work.has(user_id=await current_user_id(request)),
                )
                .with_for_update()
            )
            if job is None:
                raise HTTPException(status_code=404, detail="ジョブが見つかりません")
            await StoryJobRunner(session).resume(job)
            await session.commit()
            background_tasks.add_task(run_job_background, job.id)
            return job.to_dict()
        except Exception as exc:
            await session.rollback()
            fail(exc)
        finally:
            await session.close()

    @router.get(
        "/writing-sessions/by-conversation/{conversation_session_id}",
        response_model=WritingSessionResponse | None,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def get_writing_session_by_conversation(conversation_session_id: UUID, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            row = await session.scalar(
                select(StoryWritingSession)
                .join(StoryWritingSession.work)
                .where(
                    StoryWritingSession.conversation_session_id == conversation_session_id,
                    StoryWritingSession.work.has(user_id=user_id),
                )
                .order_by(StoryWritingSession.updated_at.desc())
                .limit(1)
            )
            return row.to_dict() if row is not None else None
        finally:
            await session.close()

    @router.post(
        "/works/{work_id}/write",
        response_model=WritingSessionResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def start_writing(work_id: UUID, payload: WriteRequest, request: Request):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            work = await owner_work(session, work_id, user_id)
            if payload.episode_id is not None:
                episode = await owner_episode(session, payload.episode_id, work.user_id)
                if episode.work_id != work.id:
                    raise HTTPException(status_code=400, detail="作品外のエピソードです")
            if payload.conversation_session_id is not None:
                conversation = await session.scalar(
                    select(ConversationSession).where(
                        ConversationSession.id == payload.conversation_session_id,
                        ConversationSession.user_id == user_id,
                        ConversationSession.deleted_at.is_(None),
                    )
                )
                if conversation is None:
                    raise HTTPException(status_code=400, detail="会話セッションが見つかりません")
            row = StoryWritingSession(work_id=work.id, episode_id=payload.episode_id, conversation_session_id=payload.conversation_session_id)
            session.add(row)
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    @router.patch(
        "/writing-sessions/{writing_session_id}",
        response_model=WritingSessionResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def update_writing_session(
        writing_session_id: UUID,
        payload: WritingSessionPatchRequest,
        request: Request,
    ):
        session = await open_session()
        try:
            user_id = await current_user_id(request)
            row = await session.scalar(
                select(StoryWritingSession)
                .join(StoryWritingSession.work)
                .where(
                    StoryWritingSession.id == writing_session_id,
                    StoryWritingSession.work.has(user_id=user_id),
                )
            )
            if row is None:
                raise HTTPException(status_code=404, detail="執筆セッションが見つかりません")
            if payload.episode_id is not None:
                episode = await owner_episode(session, payload.episode_id, user_id)
                if episode.work_id != row.work_id:
                    raise HTTPException(status_code=400, detail="作品外のエピソードです")
            row.episode_id = payload.episode_id
            await session.commit()
            return row.to_dict()
        finally:
            await session.close()

    return router


__all__ = ["create_story_router"]
