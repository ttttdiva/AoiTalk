"""
シナリオ（TRPG / インタラクティブストーリー）API ルート
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════
# Pydantic リクエストモデル
# ════════════════════════════════════════════════════


class CreateScenarioRequest(BaseModel):
    title: str
    scenario_kind: str = "writing"
    ruleset: str = ""
    description: str = ""
    genre: str = ""
    perspective: str = "first_person"
    setting: str = ""
    opening_text: str = ""
    gm_instructions: str = ""
    tags: List[str] = []
    cover_image_path: str = ""
    is_published: bool = False


class UpdateScenarioRequest(BaseModel):
    title: Optional[str] = None
    scenario_kind: Optional[str] = None
    ruleset: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    perspective: Optional[str] = None
    setting: Optional[str] = None
    opening_text: Optional[str] = None
    gm_instructions: Optional[str] = None
    tags: Optional[List[str]] = None
    cover_image_path: Optional[str] = None
    is_published: Optional[bool] = None
    voice_tone: Optional[str] = None
    voice_tense_rules: Optional[str] = None
    voice_vocabulary_register: Optional[str] = None
    voice_banned_expressions: Optional[List[str]] = None
    voice_example_passages: Optional[str] = None


class AddScenarioCharacterRequest(BaseModel):
    character_id: Optional[str] = None
    role: str = "npc"
    name: str
    description: str = ""
    personality_override: str = ""
    appearance_tags_override: str = ""
    sort_order: int = 0
    backstory: str = ""
    psychology: str = ""
    speech_patterns: str = ""
    relationships: Any = Field(default_factory=list)
    character_arc: str = ""
    importance: int = 0
    example_dialogues: str = ""
    trpg_ruleset: str = ""
    trpg_pc_state: Dict[str, Any] = Field(default_factory=dict)


class UpdateScenarioCharacterRequest(BaseModel):
    character_id: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    personality_override: Optional[str] = None
    appearance_tags_override: Optional[str] = None
    sort_order: Optional[int] = None
    backstory: Optional[str] = None
    psychology: Optional[str] = None
    speech_patterns: Optional[str] = None
    relationships: Optional[List[Dict[str, Any]]] = None
    character_arc: Optional[str] = None
    importance: Optional[int] = None
    example_dialogues: Optional[str] = None
    trpg_ruleset: Optional[str] = None
    trpg_pc_state: Optional[Dict[str, Any]] = None


class AddScenarioSceneRequest(BaseModel):
    title: str
    description: str = ""
    scene_type: str = "normal"
    gm_instructions: str = ""
    image_prompt: str = ""
    transitions: List[Dict[str, Any]] = []
    sort_order: int = 0


class UpdateScenarioSceneRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    scene_type: Optional[str] = None
    gm_instructions: Optional[str] = None
    image_prompt: Optional[str] = None
    transitions: Optional[List[Dict[str, Any]]] = None
    sort_order: Optional[int] = None
    episode_id: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None
    state_snapshot: Optional[Dict[str, Any]] = None


class StartPlaySessionRequest(BaseModel):
    user_id: str = "default_user"


class UpdatePlayStateRequest(BaseModel):
    current_scene_id: Optional[str] = None
    player_state: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    perspective: Optional[str] = None


# ── エピソード ──


class CreateEpisodeRequest(BaseModel):
    title: str
    synopsis_sentence: str = ""
    synopsis_paragraph: str = ""
    synopsis_full: str = ""
    beat_sheet: List[Any] = []
    status: str = "draft"
    sort_order: int = 0


class UpdateEpisodeRequest(BaseModel):
    title: Optional[str] = None
    synopsis_sentence: Optional[str] = None
    synopsis_paragraph: Optional[str] = None
    synopsis_full: Optional[str] = None
    beat_sheet: Optional[List[Any]] = None
    status: Optional[str] = None
    sort_order: Optional[int] = None


class ReorderEpisodesRequest(BaseModel):
    episode_ids: List[str]


# ── Canon ──


class CreateCanonEntryRequest(BaseModel):
    category: str
    fact: str
    source_scene_id: Optional[str] = None


class UpdateCanonEntryRequest(BaseModel):
    category: Optional[str] = None
    fact: Optional[str] = None
    source_scene_id: Optional[str] = None


# ── 執筆セッション ──


class StartWritingSessionRequest(BaseModel):
    target_episode_id: Optional[str] = None
    target_scene_id: Optional[str] = None
    writing_prompt: str = ""
    user_id: str = "default_user"


class UpdateWritingSessionRequest(BaseModel):
    writing_prompt: Optional[str] = None
    status: Optional[str] = None
    target_episode_id: Optional[str] = None
    target_scene_id: Optional[str] = None


# ── シーン本文 ──


class SaveSceneContentRequest(BaseModel):
    content: str
    create_version: bool = True


class UpsertTRPGDocumentRequest(BaseModel):
    id: Optional[str] = None
    ruleset: str = "generic"
    source_label: str = ""
    source_text: str = ""
    structure: Dict[str, Any] = Field(default_factory=dict)


# ════════════════════════════════════════════════════
# ルーター生成
# ════════════════════════════════════════════════════


def create_scenario_router(app_instance) -> APIRouter:
    """シナリオ API ルーターを生成する。"""
    router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

    from ..services.scenario_service import (
        ScenarioError,
        ScenarioNotFoundError,
        ScenarioCharacterNotFoundError,
        ScenarioSceneNotFoundError,
        PlaySessionNotFoundError,
        EpisodeNotFoundError,
        CanonEntryNotFoundError,
        WritingSessionNotFoundError,
        list_scenarios,
        create_scenario,
        get_scenario,
        update_scenario,
        delete_scenario,
        add_scenario_character,
        update_scenario_character,
        delete_scenario_character,
        add_scenario_scene,
        update_scenario_scene,
        delete_scenario_scene,
        start_play_session,
        get_play_session,
        update_play_state,
        get_play_session_by_conversation_id,
        list_episodes,
        create_episode,
        update_episode,
        delete_episode,
        reorder_episodes,
        list_canon_entries,
        create_canon_entry,
        update_canon_entry,
        delete_canon_entry,
        start_writing_session,
        get_writing_session,
        get_writing_session_by_conversation,
        update_writing_session,
        list_scenario_logs,
        get_scenario_log_context_by_conversation,
        save_scene_content,
        get_scene_content,
        list_trpg_documents,
        upsert_trpg_document,
        delete_trpg_document,
    )

    def _require_auth(request: Request) -> None:
        app_instance._enforce_cookie_auth(request)

    def _handle_error(e: Exception):
        if isinstance(e, ScenarioError):
            raise HTTPException(status_code=e.status_code, detail=e.message)
        logger.error("シナリオAPI内部エラー: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="内部エラーが発生しました")

    # ── シナリオ CRUD ──

    @router.get("")
    async def api_list_scenarios(
        request: Request,
        genre: str = None,
        published_only: bool = False,
    ):
        _require_auth(request)
        try:
            scenarios = await list_scenarios(genre=genre, published_only=published_only)
            return {"scenarios": scenarios}
        except Exception as e:
            _handle_error(e)

    @router.post("")
    async def api_create_scenario(request: Request, body: CreateScenarioRequest):
        _require_auth(request)
        try:
            scenario = await create_scenario(body.model_dump(exclude_none=True))
            return scenario
        except Exception as e:
            _handle_error(e)

    @router.get("/play/{session_id}")
    async def api_get_play_session(request: Request, session_id: str):
        """プレイセッションを取得する（/play/{id} は /{id} より前に定義する必要がある）。"""
        _require_auth(request)
        try:
            return await get_play_session(session_id)
        except Exception as e:
            _handle_error(e)

    @router.put("/play/{session_id}")
    async def api_update_play_state(
        request: Request,
        session_id: str,
        body: UpdatePlayStateRequest,
    ):
        _require_auth(request)
        try:
            return await update_play_state(
                session_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.get("/by-conversation/{conv_session_id}")
    async def api_get_play_session_by_conversation(
        request: Request, conv_session_id: str
    ):
        _require_auth(request)
        try:
            return await get_play_session_by_conversation_id(conv_session_id)
        except Exception as e:
            _handle_error(e)

    @router.get("/logs/by-conversation/{conv_session_id}")
    async def api_get_scenario_log_context_by_conversation(
        request: Request,
        conv_session_id: str,
    ):
        _require_auth(request)
        try:
            return await get_scenario_log_context_by_conversation(conv_session_id)
        except Exception as e:
            _handle_error(e)

    # ── エピソード（静的パスを先に） ──

    @router.put("/episodes/{episode_id}")
    async def api_update_episode(
        request: Request,
        episode_id: str,
        body: UpdateEpisodeRequest,
    ):
        _require_auth(request)
        try:
            return await update_episode(episode_id, body.model_dump(exclude_none=True))
        except Exception as e:
            _handle_error(e)

    @router.delete("/episodes/{episode_id}")
    async def api_delete_episode(request: Request, episode_id: str):
        _require_auth(request)
        try:
            await delete_episode(episode_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    # ── Canon（静的パスを先に） ──

    @router.put("/canon/{entry_id}")
    async def api_update_canon_entry(
        request: Request,
        entry_id: str,
        body: UpdateCanonEntryRequest,
    ):
        _require_auth(request)
        try:
            return await update_canon_entry(
                entry_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.delete("/canon/{entry_id}")
    async def api_delete_canon_entry(request: Request, entry_id: str):
        _require_auth(request)
        try:
            await delete_canon_entry(entry_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    # ── 執筆セッション（静的パスを先に） ──

    @router.get("/write/by-conversation/{conv_session_id}")
    async def api_get_writing_session_by_conversation(
        request: Request,
        conv_session_id: str,
    ):
        _require_auth(request)
        try:
            return await get_writing_session_by_conversation(conv_session_id)
        except Exception as e:
            _handle_error(e)

    @router.get("/write/{session_id}")
    async def api_get_writing_session(request: Request, session_id: str):
        _require_auth(request)
        try:
            return await get_writing_session(session_id)
        except Exception as e:
            _handle_error(e)

    @router.put("/write/{session_id}")
    async def api_update_writing_session(
        request: Request,
        session_id: str,
        body: UpdateWritingSessionRequest,
    ):
        _require_auth(request)
        try:
            return await update_writing_session(
                session_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    # ── シーン本文（静的パスを先に） ──

    @router.put("/scenes/{scene_id}/content")
    async def api_save_scene_content(
        request: Request,
        scene_id: str,
        body: SaveSceneContentRequest,
    ):
        _require_auth(request)
        try:
            return await save_scene_content(scene_id, body.content, body.create_version)
        except Exception as e:
            _handle_error(e)

    @router.get("/scenes/{scene_id}/content")
    async def api_get_scene_content(request: Request, scene_id: str):
        _require_auth(request)
        try:
            return await get_scene_content(scene_id)
        except Exception as e:
            _handle_error(e)

    # ── TRPGシナリオ本文（静的パスを先に） ──

    @router.get("/{scenario_id}/trpg-documents")
    async def api_list_trpg_documents(request: Request, scenario_id: str):
        _require_auth(request)
        try:
            documents = await list_trpg_documents(scenario_id)
            return {"documents": documents}
        except Exception as e:
            _handle_error(e)

    @router.put("/{scenario_id}/trpg-documents")
    async def api_upsert_trpg_document(
        request: Request,
        scenario_id: str,
        body: UpsertTRPGDocumentRequest,
    ):
        _require_auth(request)
        try:
            return await upsert_trpg_document(
                scenario_id,
                body.model_dump(exclude_none=True),
            )
        except Exception as e:
            _handle_error(e)

    @router.delete("/{scenario_id}/trpg-documents/{document_id}")
    async def api_delete_trpg_document(
        request: Request,
        scenario_id: str,
        document_id: str,
    ):
        _require_auth(request)
        try:
            await delete_trpg_document(scenario_id, document_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    @router.get("/{scenario_id}/logs")
    async def api_list_scenario_logs(request: Request, scenario_id: str):
        _require_auth(request)
        try:
            return await list_scenario_logs(scenario_id)
        except Exception as e:
            _handle_error(e)

    # ── シナリオ単体（動的パスは最後に） ──

    @router.get("/{scenario_id}")
    async def api_get_scenario(request: Request, scenario_id: str):
        _require_auth(request)
        try:
            return await get_scenario(scenario_id, include_children=True)
        except Exception as e:
            _handle_error(e)

    @router.put("/{scenario_id}")
    async def api_update_scenario(
        request: Request,
        scenario_id: str,
        body: UpdateScenarioRequest,
    ):
        _require_auth(request)
        try:
            return await update_scenario(
                scenario_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.delete("/{scenario_id}")
    async def api_delete_scenario(request: Request, scenario_id: str):
        _require_auth(request)
        try:
            await delete_scenario(scenario_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    # ── シナリオキャラクター ──

    @router.post("/{scenario_id}/characters")
    async def api_add_character(
        request: Request,
        scenario_id: str,
        body: AddScenarioCharacterRequest,
    ):
        _require_auth(request)
        try:
            return await add_scenario_character(
                scenario_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.put("/{scenario_id}/characters/{char_id}")
    async def api_update_character(
        request: Request,
        scenario_id: str,
        char_id: str,
        body: UpdateScenarioCharacterRequest,
    ):
        _require_auth(request)
        try:
            return await update_scenario_character(
                scenario_id, char_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.delete("/{scenario_id}/characters/{char_id}")
    async def api_delete_character(
        request: Request,
        scenario_id: str,
        char_id: str,
    ):
        _require_auth(request)
        try:
            await delete_scenario_character(scenario_id, char_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    # ── シナリオシーン ──

    @router.post("/{scenario_id}/scenes")
    async def api_add_scene(
        request: Request,
        scenario_id: str,
        body: AddScenarioSceneRequest,
    ):
        _require_auth(request)
        try:
            return await add_scenario_scene(
                scenario_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.put("/{scenario_id}/scenes/{scene_id}")
    async def api_update_scene(
        request: Request,
        scenario_id: str,
        scene_id: str,
        body: UpdateScenarioSceneRequest,
    ):
        _require_auth(request)
        try:
            return await update_scenario_scene(
                scenario_id, scene_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    @router.delete("/{scenario_id}/scenes/{scene_id}")
    async def api_delete_scene(
        request: Request,
        scenario_id: str,
        scene_id: str,
    ):
        _require_auth(request)
        try:
            await delete_scenario_scene(scenario_id, scene_id)
            return {"success": True}
        except Exception as e:
            _handle_error(e)

    # ── プレイセッション ──

    @router.post("/{scenario_id}/play")
    async def api_start_play(
        request: Request,
        scenario_id: str,
        body: StartPlaySessionRequest,
    ):
        _require_auth(request)
        try:
            return await start_play_session(scenario_id, body.user_id)
        except Exception as e:
            _handle_error(e)

    # ── エピソード（{scenario_id}配下） ──

    @router.get("/{scenario_id}/episodes")
    async def api_list_episodes(request: Request, scenario_id: str):
        _require_auth(request)
        try:
            episodes = await list_episodes(scenario_id)
            return {"episodes": episodes}
        except Exception as e:
            _handle_error(e)

    @router.post("/{scenario_id}/episodes")
    async def api_create_episode(
        request: Request,
        scenario_id: str,
        body: CreateEpisodeRequest,
    ):
        _require_auth(request)
        try:
            return await create_episode(scenario_id, body.model_dump(exclude_none=True))
        except Exception as e:
            _handle_error(e)

    @router.put("/{scenario_id}/episodes/reorder")
    async def api_reorder_episodes(
        request: Request,
        scenario_id: str,
        body: ReorderEpisodesRequest,
    ):
        _require_auth(request)
        try:
            episodes = await reorder_episodes(scenario_id, body.episode_ids)
            return {"episodes": episodes}
        except Exception as e:
            _handle_error(e)

    # ── Canon（{scenario_id}配下） ──

    @router.get("/{scenario_id}/canon")
    async def api_list_canon_entries(
        request: Request,
        scenario_id: str,
        category: str = None,
    ):
        _require_auth(request)
        try:
            entries = await list_canon_entries(scenario_id, category=category)
            return {"entries": entries}
        except Exception as e:
            _handle_error(e)

    @router.post("/{scenario_id}/canon")
    async def api_create_canon_entry(
        request: Request,
        scenario_id: str,
        body: CreateCanonEntryRequest,
    ):
        _require_auth(request)
        try:
            return await create_canon_entry(
                scenario_id, body.model_dump(exclude_none=True)
            )
        except Exception as e:
            _handle_error(e)

    # ── 執筆セッション（{scenario_id}配下） ──

    @router.post("/{scenario_id}/write")
    async def api_start_writing_session(
        request: Request,
        scenario_id: str,
        body: StartWritingSessionRequest,
    ):
        _require_auth(request)
        try:
            return await start_writing_session(
                scenario_id, body.model_dump(exclude_none=True), body.user_id
            )
        except Exception as e:
            _handle_error(e)

    return router
