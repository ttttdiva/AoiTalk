"""ルームの作成・一覧・取得・削除・完了。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

from ...memory.database import get_db_session
from ...memory.models import ConversationSession
from ...models.ecc_models import (
    Scenario,
    ScenarioParticipant,
    ScenarioPlaySession,
    ScenarioPlayLog,
)
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _generate_room_code,
    _hydrate_room_dict,
    _load_room_with_children,
    _normalize_room_status_filter,
    logger,
)


async def create_room(
    scenario_id: str,
    host_user_id: str,
    room_title: str = "",
    max_players: int = 4,
    gm_mode: str = "ai",
    is_public: bool = False,
    perspective: Optional[str] = None,
) -> Dict[str, Any]:
    """シナリオからプレイルーム（マルチプレイヤーセッション）を作成する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    host_uid = parse_uuid_strict(host_user_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = (
            select(Scenario)
            .options(selectinload(Scenario.scenes))
            .where(Scenario.id == scenario_uid)
        )
        result = await session.execute(stmt)
        scenario = result.scalar_one_or_none()
        if scenario is None:
            raise TRPGPlayError(
                f"シナリオが見つかりません: {scenario_id}",
                status_code=404,
            )
        if getattr(scenario, "scenario_kind", "writing") != "trpg":
            raise TRPGPlayError(
                "TRPGルームを作成できるのはTRPGシナリオだけです",
                status_code=400,
            )

        # 最初のシーンを決定
        first_scene_id = None
        if scenario.scenes:
            first_scene_id = sorted(scenario.scenes, key=lambda s: s.sort_order)[0].id

        # ルームコード（衝突回避）
        for _ in range(10):
            code = _generate_room_code()
            existing = await session.execute(
                select(ScenarioPlaySession).where(ScenarioPlaySession.room_code == code)
            )
            if existing.scalar_one_or_none() is None:
                break
        else:
            raise TRPGPlayError("ルームコードの生成に失敗しました")

        # 会話セッションも作成（既存のメッセージ基盤と連動させる）
        conv_session = ConversationSession(
            id=uuid.uuid4(),
            user_id=str(host_uid),
            character_name=f"trpg_room_{scenario.title}",
            title=f"[TRPG] {scenario.title}",
            is_group_chat=True,
            group_character_names=[],
        )
        session.add(conv_session)

        play_session = ScenarioPlaySession(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
            conversation_session_id=conv_session.id,
            current_scene_id=first_scene_id,
            perspective=perspective or scenario.perspective or "third_person",
            player_state={"party_inventory": [], "flags": {}},
            status="in_progress",
            room_code=code,
            room_title=room_title or scenario.title,
            host_user_id=host_uid,
            max_players=max_players,
            gm_mode=gm_mode,
            is_multiplayer=True,
            is_public=is_public,
            turn_order=[],
            shared_state={
                "weather": "",
                "time_of_day": "",
                "round": 0,
                "bgm_auto_enabled": True,
                "bgm": None,
            },
        )
        session.add(play_session)
        await session.flush()

        # システムログ: ルーム作成
        system_log = ScenarioPlayLog(
            id=uuid.uuid4(),
            play_session_id=play_session.id,
            participant_id=None,
            log_type="system",
            content=f"ルーム「{play_session.room_title}」が作成されました。",
            log_metadata={"event": "room_created"},
        )
        session.add(system_log)

        await session.commit()

        reloaded = await _load_room_with_children(session, play_session.id)
        data = await _hydrate_room_dict(session, reloaded)
        logger.info(
            "TRPGルーム作成: code=%s, scenario=%s, host=%s",
            code,
            scenario.title,
            host_uid,
        )
        return data


async def list_rooms(
    user_id: Optional[str] = None,
    include_public: bool = True,
    status: Optional[str] = "in_progress",
) -> List[Dict[str, Any]]:
    """参加可能なルーム一覧を取得する。"""
    status_filter = _normalize_room_status_filter(status)
    async with await get_db_session() as session:
        stmt = select(ScenarioPlaySession).where(
            ScenarioPlaySession.is_multiplayer.is_(True)
        )
        if status_filter:
            stmt = stmt.where(ScenarioPlaySession.status == status_filter)

        # 自分がホスト or 参加者のルーム + 公開ルーム
        if user_id:
            user_uid = parse_uuid(user_id)
            participant_stmt = select(ScenarioParticipant.play_session_id).where(
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.is_active_participant.is_(True),
            )
            conditions = [
                ScenarioPlaySession.host_user_id == user_uid,
                ScenarioPlaySession.id.in_(participant_stmt),
            ]
            if include_public:
                conditions.append(ScenarioPlaySession.is_public.is_(True))
            from sqlalchemy import or_

            stmt = stmt.where(or_(*conditions))
        elif include_public:
            stmt = stmt.where(ScenarioPlaySession.is_public.is_(True))

        stmt = stmt.options(selectinload(ScenarioPlaySession.participants)).order_by(
            desc(ScenarioPlaySession.updated_at)
        )

        result = await session.execute(stmt)
        rooms = result.scalars().all()

        # 軽量版 dict を返す
        out: List[Dict[str, Any]] = []
        for room in rooms:
            scenario = await session.get(Scenario, room.scenario_id)
            item = {
                "id": str(room.id),
                "room_code": room.room_code,
                "room_title": room.room_title or (scenario.title if scenario else ""),
                "status": room.status,
                "max_players": room.max_players,
                "player_count": len(
                    [
                        p
                        for p in room.participants
                        if p.is_active_participant and p.role == "player"
                    ]
                ),
                "is_public": bool(room.is_public),
                "gm_mode": room.gm_mode,
                "host_user_id": str(room.host_user_id) if room.host_user_id else None,
                "scenario": scenario.to_dict() if scenario else None,
                "updated_at": (
                    room.updated_at.isoformat() if room.updated_at else None
                ),
            }
            out.append(item)
        return out


async def get_room(room_id_or_code: str, log_limit: int = 200) -> Dict[str, Any]:
    """ルームIDまたは入室コードからルーム詳細を取得する。"""
    async with await get_db_session() as session:
        play_session = None
        uid = parse_uuid(room_id_or_code)
        if uid:
            play_session = await session.get(ScenarioPlaySession, uid)
        if play_session is None:
            # room_code で検索
            result = await session.execute(
                select(ScenarioPlaySession).where(
                    ScenarioPlaySession.room_code == room_id_or_code.upper()
                )
            )
            play_session = result.scalar_one_or_none()
        if play_session is None:
            raise RoomNotFoundError(room_id_or_code)

        reloaded = await _load_room_with_children(session, play_session.id)
        return await _hydrate_room_dict(session, reloaded, log_limit=log_limit)


def _can_delete_room(
    play_session: ScenarioPlaySession,
    user_uid: uuid.UUID,
    is_admin: bool,
) -> bool:
    return bool(is_admin or play_session.host_user_id == user_uid)


async def delete_room(room_id: str, user_id: str, *, is_admin: bool = False) -> None:
    """ルームを解散する。ホストまたは管理者のみ実行可能。"""
    uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    user_uid = parse_uuid_strict(user_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        if not _can_delete_room(play_session, user_uid, is_admin):
            raise TRPGPlayError("ホストまたは管理者のみルームを解散できます", status_code=403)
        await session.delete(play_session)
        await session.commit()
        logger.info("TRPGルーム解散: %s", room_id)


async def complete_room(
    room_id: str,
    outcome: str = "completed",
    summary: str = "",
) -> Dict[str, Any]:
    """Mark a TRPG room as completed and leave an ending log."""
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    outcome_key = str(outcome or "completed").strip() or "completed"
    summary_text = str(summary or "").strip()

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)

        shared_state = dict(play_session.shared_state or {})
        shared_state["post_session"] = {
            **(shared_state.get("post_session") if isinstance(shared_state.get("post_session"), dict) else {}),
            "outcome": outcome_key,
            "summary": summary_text,
            "completed_at": datetime.utcnow().isoformat(),
        }
        play_session.shared_state = shared_state
        play_session.status = "completed"
        play_session.updated_at = datetime.utcnow()
        content = "セッションを終了しました。"
        if outcome_key:
            content += f" 結果: {outcome_key}"
        if summary_text:
            content += f" / {summary_text}"
        await _append_log_internal(
            session,
            room_uid,
            None,
            "system",
            content,
            {"event": "session_completed", "outcome": outcome_key, "summary": summary_text},
        )
        await session.commit()
        reloaded = await _load_room_with_children(session, room_uid)
        return await _hydrate_room_dict(session, reloaded)
