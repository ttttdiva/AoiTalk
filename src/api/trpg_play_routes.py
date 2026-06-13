"""TRPG マルチプレイヤー API ルート

ココフォリア風のルーム／参加者／ログ／ダイスを提供する。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════
# Pydantic リクエストモデル
# ════════════════════════════════════════════════════


class CreateRoomRequest(BaseModel):
    scenario_id: str
    room_title: str = ""
    max_players: int = Field(default=4, ge=1, le=12)
    gm_mode: str = "ai"  # "ai" | "human"
    is_public: bool = False
    perspective: Optional[str] = None


class JoinRoomRequest(BaseModel):
    display_name: str
    character_id: Optional[str] = None
    scenario_character_id: Optional[str] = None
    role: str = "player"  # player | gm | npc | observer
    pc_state: Optional[Dict[str, Any]] = None
    avatar_url: str = ""
    as_npc: bool = False
    save_character_sheet: bool = True
    invite_code: Optional[str] = None


class UpdateParticipantRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    color: Optional[str] = None
    role: Optional[str] = None
    pc_state: Optional[Dict[str, Any]] = None
    seat_index: Optional[int] = None
    is_connected: Optional[bool] = None


class AppendLogRequest(BaseModel):
    log_type: str
    content: str = ""
    participant_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class RollDiceRequest(BaseModel):
    expression: str = "1d100"
    participant_id: Optional[str] = None
    target: Optional[int] = None
    difficulty: str = "regular"
    note: str = ""


class CoCResourceRequest(BaseModel):
    participant_id: str
    resource: str
    operation: str = "damage"
    amount: int = Field(default=0, ge=0)
    reason: str = ""


class CoCSkillCheckRequest(BaseModel):
    participant_id: str
    skill: str
    difficulty: str = "regular"
    note: str = ""
    mark_experience: bool = True


class CoCResistanceRequest(BaseModel):
    participant_id: str
    active_value: int
    passive_value: int
    note: str = ""


class CoCDevelopmentRequest(BaseModel):
    participant_id: str
    skill: str


class CompleteRoomRequest(BaseModel):
    outcome: str = "completed"
    summary: str = ""


class CoCPostSessionRequest(BaseModel):
    participant_ids: List[str] = Field(default_factory=list)
    sanity_recovery_expression: str = ""
    outcome: str = ""
    close_room: bool = False


class CoCAttackRequest(BaseModel):
    attacker_id: str
    defender_id: Optional[str] = None
    weapon: str = "こぶし"
    defense_type: str = "回避"
    note: str = ""


class CoCSpellCostRequest(BaseModel):
    participant_id: str
    spell_name: str
    mp_cost: int = Field(default=0, ge=0)
    san_cost: int = Field(default=0, ge=0)
    hp_cost: int = Field(default=0, ge=0)
    pow_cost: int = Field(default=0, ge=0)


class CoCInsanityRequest(BaseModel):
    participant_id: str
    kind: str = "temporary"
    reason: str = ""


class ChangeSceneRequest(BaseModel):
    next_scene_id: str
    announcement: str = ""


class UpdateSharedStateRequest(BaseModel):
    updates: Dict[str, Any]


class UIModuleActionRequest(BaseModel):
    participant_id: Optional[str] = None
    action_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class PlayerActionRequest(BaseModel):
    participant_id: str
    action_text: str
    action_kind: str = "action"  # action | speech | ooc


class GMAdvanceRequest(BaseModel):
    user_request: str = ""


class CurrentSceneImageRequest(BaseModel):
    participant_id: str
    user_prompt: str = ""


class NPCStrategyScheduleRequest(BaseModel):
    phase: str = "作戦タイム"
    delay_seconds: int = Field(default=30, ge=0, le=300)
    focus: str = ""


class NPCStrategyProcessRequest(BaseModel):
    schedule_id: Optional[str] = None
    force: bool = False


class SuggestQuickNPCNameRequest(BaseModel):
    theme: str = ""
    name: str = ""


class BGMVideoPlaybackRequest(BaseModel):
    track: str


class CreateDisclosureRequest(BaseModel):
    creator_participant_id: Optional[str] = None
    disclosure_type: str = "handout"
    visibility: str = "public"
    target_participant_ids: List[str] = Field(default_factory=list)
    title: str
    content: str = ""
    image_url: str = ""
    image_path: str = ""
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    is_pinned: bool = False


class SendPrivateMessageRequest(BaseModel):
    sender_participant_id: str
    target_participant_ids: List[str] = Field(default_factory=list)
    content: str
    message_type: str = "private"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    request_gm_reply: bool = True


class UpsertReferenceDocumentRequest(BaseModel):
    id: Optional[str] = None
    title: str
    source_label: str = ""
    source_text: str = ""
    document_type: str = "rulebook"
    supplement_kind: str = "general"
    structure: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    is_active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ════════════════════════════════════════════════════
# ルーター生成
# ════════════════════════════════════════════════════


def create_trpg_play_router(app_instance) -> APIRouter:
    """TRPG プレイ API ルーターを生成する。"""
    router = APIRouter(prefix="/api/trpg", tags=["trpg-play"])

    from .trpg_ws import TRPGRoomBroadcaster

    broadcaster = TRPGRoomBroadcaster.get()

    async def _broadcast(
        room_id: str, event_type: str, payload: Dict[str, Any]
    ) -> None:
        try:
            await broadcaster.broadcast(
                room_id,
                {"type": event_type, **payload},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("TRPG broadcast failed: %s", e)

    async def _broadcast_result_logs(room_id: str, result: Dict[str, Any]) -> None:
        logs = result.get("logs")
        if isinstance(logs, list) and logs:
            for log in logs:
                await _broadcast(room_id, "log_append", {"log": log})
            return
        if result.get("log"):
            await _broadcast(room_id, "log_append", {"log": result["log"]})

    async def _broadcast_npc_strategy_result(room_id: str, result: Dict[str, Any]) -> None:
        await _broadcast_result_logs(room_id, result)
        participants = result.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                await _broadcast(
                    room_id,
                    "participant_update",
                    {"participant": participant, "event": "ai_npc_strategy"},
                )
        if result.get("shared_state"):
            await _broadcast(
                room_id,
                "shared_state",
                {"shared_state": result["shared_state"]},
            )

    async def _broadcast_ai_npc_reactions(room_id: str, trigger: str) -> Dict[str, Any]:
        try:
            result = await process_ai_npc_reactions(room_id, trigger=trigger)
        except Exception as e:  # noqa: BLE001
            logger.warning("AI NPC reaction skipped: %s", e)
            return {"logs": [], "participants": []}
        await _broadcast_npc_strategy_result(room_id, result)
        return result

    async def _delayed_npc_strategy(room_id: str, strategy: Dict[str, Any]) -> None:
        try:
            delay_seconds = int(strategy.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            delay_seconds = 0
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        try:
            result = await process_due_ai_npc_strategy(
                room_id,
                schedule_id=strategy.get("id"),
            )
            await _broadcast_npc_strategy_result(room_id, result)
        except Exception as e:  # noqa: BLE001
            logger.warning("AI NPC strategy task failed: %s", e)

    def _queue_npc_strategy_from_result(room_id: str, result: Dict[str, Any]) -> None:
        markers = result.get("markers")
        strategy = markers.get("npc_strategy") if isinstance(markers, dict) else None
        if isinstance(strategy, dict) and strategy.get("id"):
            asyncio.create_task(_delayed_npc_strategy(room_id, strategy))

    async def _broadcast_coc_result(room_id: str, result: Dict[str, Any]) -> None:
        await _broadcast_result_logs(room_id, result)
        if result.get("participant"):
            await _broadcast(
                room_id,
                "participant_update",
                {"participant": result["participant"], "event": "coc_update"},
            )
        if result.get("defender"):
            await _broadcast(
                room_id,
                "participant_update",
                {"participant": result["defender"], "event": "coc_update"},
            )

    from ..services.trpg_play_service import (
        TRPGPlayError,
        RoomNotFoundError,
        ParticipantNotFoundError,
        RoomFullError,
        create_room,
        list_rooms,
        get_room,
        delete_room,
        complete_room,
        join_room,
        list_player_character_sheets,
        suggest_quick_npc_name,
        leave_room,
        update_participant,
        append_log,
        list_logs,
        roll_dice_in_room,
        coc_apply_resource,
        coc_skill_check,
        coc_resistance_check,
        coc_development_check,
        coc_post_session_summary,
        coc_apply_post_session,
        coc_attack_action,
        coc_spell_cost_action,
        coc_insanity_action,
        advance_turn,
        change_scene,
        update_shared_state,
        apply_ui_module_action,
        require_room_view_access,
        require_room_participation_access,
        require_room_gm_access,
        require_participant_write_access,
        list_disclosures,
        create_disclosure,
        list_private_messages,
        send_private_message,
        GM_TARGET_ID,
    )
    from ..services.trpg_gm_service import (
        generate_current_scene_image,
        generate_gm_narration,
        generate_private_gm_reply,
        submit_player_action,
        start_session_with_opening,
    )
    from ..services.trpg_ai_npc_service import (
        process_ai_npc_reactions,
        process_due_ai_npc_strategy,
        schedule_ai_npc_strategy,
    )
    from ..services.trpg_rulebook_service import (
        TRPGRulebookError,
        list_ruleset_profiles,
        list_reference_documents,
        upsert_reference_document,
        delete_reference_document,
        list_rulebook_documents,
        upsert_rulebook_document,
        delete_rulebook_document,
    )
    from ..services.trpg_rule_reference_service import (
        get_rule_reference_stats,
        search_rule_references,
    )

    def _http(exc: TRPGPlayError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    def _http_rulebook(exc: TRPGRulebookError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    def _require_auth(request: Request) -> None:
        """認証を強制する。未認証なら 401 を返す。"""
        if app_instance is not None:
            app_instance._enforce_cookie_auth(request)

    async def _current_user_info(request: Request) -> Optional[Dict[str, Any]]:
        """リクエストから認証済みユーザー情報を取得する。"""
        if app_instance is not None and hasattr(app_instance, "_get_user_info_from_request"):
            user = await app_instance._get_user_info_from_request(request)
            if user and isinstance(user, dict):
                return user
        # フォールバック: request.state.user (テスト用)
        user = getattr(request.state, "user", None)
        if user and isinstance(user, dict):
            return user
        return None

    async def _current_user_id(request: Request) -> Optional[str]:
        """リクエストから認証済みユーザーIDを取得する。"""
        user = await _current_user_info(request)
        if user:
            return user.get("user_id") or user.get("id")
        return None

    async def _require_user_id(request: Request) -> str:
        _require_auth(request)
        user_id = await _current_user_id(request)
        if not user_id:
            raise HTTPException(401, "認証が必要です")
        return user_id

    # ── ルールシステム / 参照資料 ──

    @router.get("/rulesets")
    async def api_list_rulesets(include_disabled: bool = False):
        try:
            profiles = await list_ruleset_profiles(include_disabled=include_disabled)
            return {"rulesets": profiles, "count": len(profiles)}
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.get("/rulesets/{ruleset_key}/reference-documents")
    async def api_list_reference_documents(
        ruleset_key: str,
        active_only: bool = True,
        document_type: Optional[str] = None,
    ):
        try:
            documents = await list_reference_documents(
                ruleset_key,
                active_only=active_only,
                document_type=document_type,
            )
            return {"documents": documents, "count": len(documents)}
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.put("/rulesets/{ruleset_key}/reference-documents")
    async def api_upsert_reference_document(
        ruleset_key: str,
        req: UpsertReferenceDocumentRequest,
        request: Request,
    ):
        _require_auth(request)
        try:
            return await upsert_reference_document(ruleset_key, req.dict())
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.delete("/rulesets/{ruleset_key}/reference-documents/{document_id}")
    async def api_delete_reference_document(
        ruleset_key: str,
        document_id: str,
        request: Request,
    ):
        _require_auth(request)
        try:
            await delete_reference_document(ruleset_key, document_id)
            return {"ok": True}
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.get("/rulesets/{ruleset_key}/rulebooks")
    async def api_list_rulebooks(ruleset_key: str, active_only: bool = True):
        try:
            documents = await list_rulebook_documents(ruleset_key, active_only=active_only)
            return {"rulebooks": documents, "count": len(documents)}
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.put("/rulesets/{ruleset_key}/rulebooks")
    async def api_upsert_rulebook(
        ruleset_key: str,
        req: UpsertReferenceDocumentRequest,
        request: Request,
    ):
        _require_auth(request)
        try:
            return await upsert_rulebook_document(ruleset_key, req.dict())
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.delete("/rulesets/{ruleset_key}/rulebooks/{document_id}")
    async def api_delete_rulebook(
        ruleset_key: str,
        document_id: str,
        request: Request,
    ):
        _require_auth(request)
        try:
            await delete_rulebook_document(ruleset_key, document_id)
            return {"ok": True}
        except TRPGRulebookError as e:
            raise _http_rulebook(e)

    @router.get("/rulesets/{ruleset_key}/references")
    async def api_search_rule_references(
        ruleset_key: str,
        query: str = "",
        kind: str = "all",
        mechanic_key: Optional[str] = None,
        rule_domain: Optional[str] = None,
        creature_type: Optional[str] = None,
        limit: int = 20,
    ):
        normalized_kind = str(kind or "all").strip().lower()
        tome_domains = ["mythos_tomes", "occult_tomes"]
        include_tomes = normalized_kind in {"tomes", "tome", "books", "book"}
        include_rules = normalized_kind in {"all", "rules", "rule"} or include_tomes
        include_creatures = normalized_kind in {"all", "creatures", "creature"}
        if not include_rules and not include_creatures:
            include_rules = True
            include_creatures = True
        rule_domains = [rule_domain] if rule_domain and include_rules else None
        excluded_rule_domains = None
        if include_tomes:
            rule_domains = tome_domains
        elif normalized_kind in {"rules", "rule"}:
            excluded_rule_domains = tome_domains
        bundle = await search_rule_references(
            ruleset_key=ruleset_key,
            query=query,
            mechanic_keys=[mechanic_key] if mechanic_key and include_rules else None,
            rule_domains=rule_domains,
            excluded_rule_domains=excluded_rule_domains,
            creature_types=[creature_type] if creature_type and include_creatures else None,
            include_creatures=include_creatures,
            limit=max(1, min(int(limit or 20), 80)),
        )
        if not include_rules:
            bundle["rules"] = []
            bundle["mechanic_links"] = []
        return {
            **bundle,
            "count": len(bundle.get("rules") or []) + len(bundle.get("creatures") or []),
        }

    @router.get("/rulesets/{ruleset_key}/reference-stats")
    async def api_rule_reference_stats(ruleset_key: str):
        return await get_rule_reference_stats(ruleset_key)

    # ── ルーム ──

    @router.get("/rooms")
    async def api_list_rooms(
        request: Request,
        include_public: bool = True,
        status: Optional[str] = "in_progress",
    ):
        user_id = await _current_user_id(request)
        try:
            rooms = await list_rooms(
                user_id=user_id,
                include_public=include_public,
                status=status,
            )
            return {"rooms": rooms, "count": len(rooms)}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms")
    async def api_create_room(req: CreateRoomRequest, request: Request):
        user_id = await _current_user_id(request)
        if not user_id:
            raise HTTPException(401, "認証が必要です")
        try:
            room = await create_room(
                scenario_id=req.scenario_id,
                host_user_id=user_id,
                room_title=req.room_title,
                max_players=req.max_players,
                gm_mode=req.gm_mode,
                is_public=req.is_public,
                perspective=req.perspective,
            )
            return room
        except TRPGPlayError as e:
            raise _http(e)

    @router.get("/rooms/{room_id_or_code}")
    async def api_get_room(
        room_id_or_code: str,
        request: Request,
        log_limit: int = 200,
        invite_code: Optional[str] = None,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_view_access(
                room_id_or_code,
                user_id,
                invite_code=invite_code,
            )
            return await get_room(room_id_or_code, log_limit=log_limit)
        except TRPGPlayError as e:
            raise _http(e)

    @router.delete("/rooms/{room_id}")
    async def api_delete_room(room_id: str, request: Request):
        user = await _current_user_info(request)
        user_id = (user or {}).get("user_id") or (user or {}).get("id")
        if not user_id:
            raise HTTPException(401, "認証が必要です")
        is_admin = (user or {}).get("role") == "admin"
        try:
            await delete_room(room_id, user_id, is_admin=is_admin)
            return {"ok": True}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/complete")
    async def api_complete_room(room_id: str, req: CompleteRoomRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id, user_id)
            room = await complete_room(room_id, outcome=req.outcome, summary=req.summary)
            await _broadcast(room_id, "state_sync", {"room": room})
            return room
        except TRPGPlayError as e:
            raise _http(e)

    # ── 参加者 ──

    @router.get("/rooms/{room_id_or_code}/player-sheets")
    async def api_list_player_sheets(room_id_or_code: str, request: Request):
        user_id = await _current_user_id(request)
        if not user_id:
            raise HTTPException(401, "認証が必要です")
        try:
            sheets = await list_player_character_sheets(room_id_or_code, user_id)
            return {"sheets": sheets}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id_or_code}/join")
    async def api_join_room(
        room_id_or_code: str,
        req: JoinRoomRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)

        # NPCとして追加する場合は、リクエスト者がホストであることを確認する
        actual_user_id = user_id
        if req.as_npc:
            room_data = await get_room(room_id_or_code)
            if room_data.get("host_user_id") != user_id:
                raise HTTPException(403, "ホストのみNPCを追加できます")
            actual_user_id = None
        else:
            try:
                await require_room_participation_access(
                    room_id_or_code,
                    user_id,
                    invite_code=req.invite_code,
                )
            except TRPGPlayError as e:
                raise _http(e)

        try:
            participant = await join_room(
                room_id_or_code=room_id_or_code,
                user_id=actual_user_id,
                display_name=req.display_name,
                character_id=req.character_id,
                scenario_character_id=req.scenario_character_id,
                role=req.role,
                pc_state=req.pc_state,
                avatar_url=req.avatar_url,
                save_character_sheet=req.save_character_sheet,
            )
            await _broadcast(
                participant["play_session_id"],
                "participant_update",
                {"participant": participant, "event": "join"},
            )
            return participant
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id_or_code}/npc/suggest-name")
    async def api_suggest_quick_npc_name(
        room_id_or_code: str,
        req: SuggestQuickNPCNameRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id_or_code, user_id)
            return await suggest_quick_npc_name(
                room_id_or_code,
                theme=req.theme,
                name=req.name,
            )
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/leave/{participant_id}")
    async def api_leave_room(
        room_id: str,
        participant_id: str,
        request: Request,
        disconnect_only: bool = False,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(participant_id, user_id)
            await leave_room(room_id, participant_id, disconnect_only=disconnect_only)
            room = await get_room(room_id)
            await _broadcast(room_id, "state_sync", {"room": room})
            return {"ok": True}
        except TRPGPlayError as e:
            raise _http(e)

    @router.put("/participants/{participant_id}")
    async def api_update_participant(
        participant_id: str,
        req: UpdateParticipantRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(participant_id, user_id)
            payload = {k: v for k, v in req.dict().items() if v is not None}
            return await update_participant(participant_id, payload)
        except TRPGPlayError as e:
            raise _http(e)

    # ── ログ ──

    @router.get("/rooms/{room_id}/logs")
    async def api_list_logs(
        room_id: str,
        request: Request,
        limit: int = 200,
        before_id: Optional[str] = None,
        invite_code: Optional[str] = None,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_view_access(room_id, user_id, invite_code=invite_code)
            logs = await list_logs(room_id, limit=limit, before_id=before_id)
            return {"logs": logs, "count": len(logs)}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/logs")
    async def api_append_log(room_id: str, req: AppendLogRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            if req.participant_id:
                await require_participant_write_access(req.participant_id, user_id)
            else:
                await require_room_gm_access(room_id, user_id)
            log = await append_log(
                room_id=room_id,
                log_type=req.log_type,
                content=req.content,
                participant_id=req.participant_id,
                metadata=req.metadata,
            )
            await _broadcast(room_id, "log_append", {"log": log})
            await _broadcast_ai_npc_reactions(room_id, "manual_log")
            return log
        except TRPGPlayError as e:
            raise _http(e)

    # ── 開示情報 / 個別チャット ──

    @router.get("/rooms/{room_id}/disclosures")
    async def api_list_disclosures(
        room_id: str,
        request: Request,
        viewer_participant_id: Optional[str] = None,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_view_access(room_id, user_id)
            disclosures = await list_disclosures(
                room_id,
                viewer_participant_id=viewer_participant_id,
                user_id=user_id,
            )
            return {"disclosures": disclosures, "count": len(disclosures)}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/disclosures")
    async def api_create_disclosure(
        room_id: str,
        req: CreateDisclosureRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)
        try:
            if req.creator_participant_id:
                await require_participant_write_access(req.creator_participant_id, user_id)
            if req.visibility in {"private", "gm"}:
                await require_room_gm_access(room_id, user_id)
            disclosure = await create_disclosure(room_id, req.dict(), user_id=user_id)
            await _broadcast(room_id, "disclosure_refresh", {})
            return disclosure
        except TRPGPlayError as e:
            raise _http(e)

    @router.get("/rooms/{room_id}/private-messages")
    async def api_list_private_messages(
        room_id: str,
        request: Request,
        viewer_participant_id: Optional[str] = None,
        limit: int = 200,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_view_access(room_id, user_id)
            messages = await list_private_messages(
                room_id,
                viewer_participant_id=viewer_participant_id,
                user_id=user_id,
                limit=limit,
            )
            return {"messages": messages, "count": len(messages)}
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/private-messages")
    async def api_send_private_message(
        room_id: str,
        req: SendPrivateMessageRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(
                req.sender_participant_id,
                user_id,
                allow_gm=True,
            )
            message = await send_private_message(room_id, req.dict(), user_id=user_id)
            gm_reply = None
            if req.request_gm_reply and GM_TARGET_ID in req.target_participant_ids:
                gm_reply = await generate_private_gm_reply(
                    room_id,
                    req.sender_participant_id,
                    req.content,
                )
            await _broadcast(room_id, "private_refresh", {})
            return {"message": message, "gm_reply": gm_reply}
        except TRPGPlayError as e:
            raise _http(e)

    # ── ダイス ──

    @router.post("/rooms/{room_id}/dice")
    async def api_roll_dice(room_id: str, req: RollDiceRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            if req.participant_id:
                await require_participant_write_access(req.participant_id, user_id)
            else:
                await require_room_gm_access(room_id, user_id)
            log = await roll_dice_in_room(
                room_id=room_id,
                participant_id=req.participant_id,
                expression=req.expression,
                target=req.target,
                difficulty=req.difficulty,
                note=req.note,
            )
            await _broadcast(room_id, "log_append", {"log": log})
            await _broadcast_ai_npc_reactions(room_id, "dice")
            return log
        except TRPGPlayError as e:
            raise _http(e)

    # ── CoC 専用処理 ──

    @router.post("/rooms/{room_id}/coc/resource")
    async def api_coc_resource(room_id: str, req: CoCResourceRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_apply_resource(
                room_id=room_id,
                participant_id=req.participant_id,
                resource=req.resource,
                operation=req.operation,
                amount=req.amount,
                reason=req.reason,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/skill-check")
    async def api_coc_skill_check(room_id: str, req: CoCSkillCheckRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_skill_check(
                room_id=room_id,
                participant_id=req.participant_id,
                skill=req.skill,
                difficulty=req.difficulty,
                note=req.note,
                mark_experience=req.mark_experience,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/resistance")
    async def api_coc_resistance(room_id: str, req: CoCResistanceRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_resistance_check(
                room_id=room_id,
                participant_id=req.participant_id,
                active_value=req.active_value,
                passive_value=req.passive_value,
                note=req.note,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/development")
    async def api_coc_development(room_id: str, req: CoCDevelopmentRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_development_check(
                room_id=room_id,
                participant_id=req.participant_id,
                skill=req.skill,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.get("/rooms/{room_id}/coc/post-session")
    async def api_coc_post_session_summary(
        room_id: str,
        request: Request,
        participant_id: Optional[str] = None,
    ):
        user_id = await _require_user_id(request)
        try:
            await require_room_view_access(room_id, user_id)
            participant_ids = [participant_id] if participant_id else None
            return await coc_post_session_summary(room_id, participant_ids=participant_ids)
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/post-session")
    async def api_coc_post_session(room_id: str, req: CoCPostSessionRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            if req.close_room or not req.participant_ids:
                await require_room_gm_access(room_id, user_id)
            else:
                for participant_id in req.participant_ids:
                    await require_participant_write_access(participant_id, user_id)
            res = await coc_apply_post_session(
                room_id=room_id,
                participant_ids=req.participant_ids or None,
                sanity_recovery_expression=req.sanity_recovery_expression,
                outcome=req.outcome,
                close_room=req.close_room,
            )
            if res.get("room"):
                await _broadcast(room_id, "state_sync", {"room": res["room"]})
            else:
                await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/attack")
    async def api_coc_attack(room_id: str, req: CoCAttackRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.attacker_id, user_id)
            if req.defender_id:
                await require_participant_write_access(req.defender_id, user_id)
            res = await coc_attack_action(
                room_id=room_id,
                attacker_id=req.attacker_id,
                defender_id=req.defender_id,
                weapon=req.weapon,
                defense_type=req.defense_type,
                note=req.note,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/spell-cost")
    async def api_coc_spell_cost(room_id: str, req: CoCSpellCostRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_spell_cost_action(
                room_id=room_id,
                participant_id=req.participant_id,
                spell_name=req.spell_name,
                mp_cost=req.mp_cost,
                san_cost=req.san_cost,
                hp_cost=req.hp_cost,
                pow_cost=req.pow_cost,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/coc/insanity")
    async def api_coc_insanity(room_id: str, req: CoCInsanityRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await coc_insanity_action(
                room_id=room_id,
                participant_id=req.participant_id,
                kind=req.kind,
                reason=req.reason,
            )
            await _broadcast_coc_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    # ── ターン / シーン / 状態 ──

    @router.post("/rooms/{room_id}/turn/advance")
    async def api_advance_turn(room_id: str, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id, user_id)
            res = await advance_turn(room_id)
            await _broadcast(room_id, "turn_change", res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/scene")
    async def api_change_scene(room_id: str, req: ChangeSceneRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id, user_id)
            res = await change_scene(
                room_id=room_id,
                next_scene_id=req.next_scene_id,
                announcement=req.announcement,
            )
            await _broadcast(room_id, "scene_change", res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.put("/rooms/{room_id}/shared_state")
    async def api_update_shared_state(room_id: str, req: UpdateSharedStateRequest, request: Request):
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id, user_id)
            res = await update_shared_state(room_id, req.updates)
            await _broadcast(room_id, "shared_state", {"shared_state": res})
            return res
        except TRPGPlayError as e:
            raise _http(e)

    # ── BGM / 動画音声 ──

    @router.post("/rooms/{room_id}/ui-modules/{module_id}/actions")
    async def api_apply_ui_module_action(
        room_id: str,
        module_id: str,
        req: UIModuleActionRequest,
        request: Request,
    ):
        user_id = await _require_user_id(request)
        try:
            if req.participant_id:
                await require_participant_write_access(room_id, req.participant_id, user_id)
            else:
                await require_room_gm_access(room_id, user_id)
            res = await apply_ui_module_action(
                room_id=room_id,
                module_id=module_id,
                action_type=req.action_type,
                payload=req.payload,
                participant_id=req.participant_id,
            )
            if res.get("log"):
                await _broadcast(room_id, "log_append", {"log": res["log"]})
            if res.get("shared_state"):
                await _broadcast(
                    room_id,
                    "shared_state",
                    {"shared_state": res["shared_state"]},
                )
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/bgm/video")
    async def api_play_video_bgm(room_id: str, req: BGMVideoPlaybackRequest, request: Request):
        """YouTube/NicoNico URL を既存の動画音声再生機能で再生する。"""
        user_id = await _require_user_id(request)
        track = req.track.strip()
        if not track:
            raise HTTPException(400, "track が空です")
        try:
            await require_room_gm_access(room_id, user_id)
            if "youtube.com" in track or "youtu.be" in track:
                from ..tools.entertainment.video_streaming.video_streaming_tools import (
                    play_youtube_audio,
                )

                result = play_youtube_audio(track)
            elif "nicovideo.jp" in track or "nico.ms" in track:
                from ..tools.entertainment.video_streaming.video_streaming_tools import (
                    play_niconico_audio,
                )

                result = play_niconico_audio(track)
            else:
                raise HTTPException(400, "対応している動画URLではありません")
            await _broadcast(
                room_id,
                "bgm_video",
                {"track": track, "message": result},
            )
            return {"ok": True, "message": result}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("video BGM playback failed: %s", e)
            raise HTTPException(500, f"動画音声の再生に失敗しました: {e}")

    # ── AI GM ──

    @router.post("/rooms/{room_id}/npc/strategy/schedule")
    async def api_schedule_npc_strategy(
        room_id: str,
        req: NPCStrategyScheduleRequest,
        request: Request,
    ):
        """AI NPC作戦フェーズを予約する。"""
        user_id = await _require_user_id(request)
        try:
            await require_room_participation_access(room_id, user_id)
            result = await schedule_ai_npc_strategy(
                room_id,
                phase=req.phase,
                delay_seconds=req.delay_seconds,
                focus=req.focus,
                trigger="manual",
            )
            await _broadcast(room_id, "shared_state", {"shared_state": result["shared_state"]})
            if isinstance(result.get("strategy"), dict):
                asyncio.create_task(_delayed_npc_strategy(room_id, result["strategy"]))
            return result
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/npc/strategy/process")
    async def api_process_npc_strategy(
        room_id: str,
        req: NPCStrategyProcessRequest,
        request: Request,
    ):
        """期限到来済みのAI NPC作戦フェーズを処理する。"""
        user_id = await _require_user_id(request)
        try:
            await require_room_participation_access(room_id, user_id)
            result = await process_due_ai_npc_strategy(
                room_id,
                schedule_id=req.schedule_id,
                force=req.force,
            )
            await _broadcast_npc_strategy_result(room_id, result)
            return result
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/start")
    async def api_start_session(room_id: str, request: Request):
        """セッション開始（オープニング生成）"""
        user_id = await _require_user_id(request)
        try:
            await require_room_gm_access(room_id, user_id)
            res = await start_session_with_opening(room_id)
            await _broadcast_result_logs(room_id, res)
            res["npc_reactions"] = await _broadcast_ai_npc_reactions(room_id, "session_start")
            if res.get("shared_state"):
                await _broadcast(room_id, "shared_state", {"shared_state": res["shared_state"]})
            _queue_npc_strategy_from_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/gm/advance")
    async def api_gm_advance(room_id: str, req: GMAdvanceRequest, request: Request):
        """AI GMに次の描写を作らせる"""
        user_id = await _require_user_id(request)
        try:
            await require_room_participation_access(room_id, user_id)
            await _broadcast(room_id, "gm_thinking", {})
            res = await generate_gm_narration(room_id, user_request=req.user_request)
            await _broadcast_result_logs(room_id, res)
            if res.get("markers"):
                await _broadcast(room_id, "gm_markers", {"markers": res["markers"]})
            if res.get("room"):
                await _broadcast(room_id, "state_sync", {"room": res["room"]})
            res["npc_reactions"] = await _broadcast_ai_npc_reactions(room_id, "gm_advance")
            if res.get("shared_state"):
                await _broadcast(room_id, "shared_state", {"shared_state": res["shared_state"]})
            _queue_npc_strategy_from_result(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/images/current")
    async def api_generate_current_scene_image(
        room_id: str,
        req: CurrentSceneImageRequest,
        request: Request,
    ):
        """参加者の要求で現在状況の画像を生成する"""
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            await _broadcast(room_id, "image_generating", {"participant_id": req.participant_id})
            res = await generate_current_scene_image(
                room_id,
                req.participant_id,
                user_prompt=req.user_prompt,
            )
            await _broadcast_result_logs(room_id, res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    @router.post("/rooms/{room_id}/actions")
    async def api_submit_action(room_id: str, req: PlayerActionRequest, request: Request):
        """プレイヤーの行動宣言 → AI GM ナレーション生成"""
        user_id = await _require_user_id(request)
        try:
            await require_participant_write_access(req.participant_id, user_id)
            res = await submit_player_action(
                room_id=room_id,
                participant_id=req.participant_id,
                action_text=req.action_text,
                action_kind=req.action_kind,
                generate_gm_reply=False,
            )
            if res.get("action_log"):
                await _broadcast(room_id, "log_append", {"log": res["action_log"]})
            res["npc_reactions"] = await _broadcast_ai_npc_reactions(room_id, "player_action")
            if req.action_kind != "ooc":
                gm_res = await generate_gm_narration(
                    room_id,
                    user_request=(
                        "直前のPL発言とNPC応答を受けて、GMとして状況説明、必要な進行判断、"
                        "次にPLが取れる行動を提示してください。"
                    ),
                )
                await _broadcast_result_logs(room_id, gm_res)
                if gm_res.get("markers"):
                    await _broadcast(room_id, "gm_markers", {"markers": gm_res["markers"]})
                if gm_res.get("room"):
                    await _broadcast(room_id, "state_sync", {"room": gm_res["room"]})
                if gm_res.get("shared_state"):
                    await _broadcast(room_id, "shared_state", {"shared_state": gm_res["shared_state"]})
                _queue_npc_strategy_from_result(room_id, gm_res)
                res.update(gm_res)
            return res
        except TRPGPlayError as e:
            raise _http(e)

    return router
