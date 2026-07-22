"""TRPG プレイサービス共有基盤

例外・定数・DB解決/ロード・共通ユーティリティなど、複数の関心モジュールから
参照される土台をまとめる。
"""

from __future__ import annotations

import logging
import random
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...memory.database import get_db_session
from ...models.ecc_models import (
    Scenario,
    ScenarioScene,
    ScenarioParticipant,
    ScenarioPlaySession,
    ScenarioPlayLog,
)
from ..trpg_rules import (
    COC6_RULESET_TAG,
    COC7_RULESET_TAG,
    create_pc_state_for_ruleset,
    is_coc_scenario,
    is_coc7_scenario,
)
from ..trpg_coc import normalize_coc_state
from ...utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
# 例外
# ────────────────────────────────────────────


class TRPGPlayError(Exception):
    """TRPGプレイ操作のドメインエラー"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class RoomNotFoundError(TRPGPlayError):
    def __init__(self, identifier: str):
        super().__init__(
            f"プレイルームが見つかりません: {identifier}",
            status_code=404,
        )


class ParticipantNotFoundError(TRPGPlayError):
    def __init__(self, identifier: str):
        super().__init__(
            f"参加者が見つかりません: {identifier}",
            status_code=404,
        )


class RoomFullError(TRPGPlayError):
    def __init__(self, room_id: str, max_players: int):
        super().__init__(
            f"ルームが満席です (最大{max_players}名): {room_id}",
            status_code=409,
        )


# ────────────────────────────────────────────
# ユーティリティ / 定数
# ────────────────────────────────────────────


_DEFAULT_PARTICIPANT_COLORS = [
    "#60a5fa",  # blue
    "#f472b6",  # pink
    "#34d399",  # green
    "#fbbf24",  # amber
    "#a78bfa",  # purple
    "#fb7185",  # rose
    "#22d3ee",  # cyan
    "#facc15",  # yellow
]
GM_TARGET_ID = "gm"
DISCLOSURE_VISIBILITIES = {"public", "private", "gm"}
DISCLOSURE_TYPES = {"handout", "item", "clue", "image", "note"}
ALL_ROOM_STATUSES = {"", "all", "any", "*"}


def _normalize_room_status_filter(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    normalized = str(status).strip().lower()
    if normalized in ALL_ROOM_STATUSES:
        return None
    return normalized


def _normalize_target_ids(
    values: Optional[List[Any]],
    allow_gm: bool = True,
) -> List[str]:
    normalized: List[str] = []
    for value in values or []:
        raw = str(value or "").strip()
        if not raw:
            continue
        if allow_gm and raw.lower() == GM_TARGET_ID:
            raw = GM_TARGET_ID
        else:
            uid = parse_uuid(raw)
            if uid is None:
                continue
            raw = str(uid)
        if raw not in normalized:
            normalized.append(raw)
    return normalized


def _participant_is_gm(participant: Optional[ScenarioParticipant]) -> bool:
    return bool(participant and (participant.role or "").lower() == "gm")


def _participant_id_str(participant: Optional[ScenarioParticipant]) -> str:
    return str(participant.id) if participant and participant.id else ""


async def _resolve_play_session(session, room_id_or_code: str) -> ScenarioPlaySession:
    play_session = None
    uid = parse_uuid(room_id_or_code)
    if uid:
        play_session = await session.get(ScenarioPlaySession, uid)
    if play_session is None:
        result = await session.execute(
            select(ScenarioPlaySession).where(
                ScenarioPlaySession.room_code == room_id_or_code.upper()
            )
        )
        play_session = result.scalar_one_or_none()
    if play_session is None:
        raise RoomNotFoundError(room_id_or_code)
    return play_session


def _generate_room_code() -> str:
    """6文字の入室コードを生成する。"""
    # 紛らわしい文字 (0/O, 1/I/L) は除外
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(alphabet, k=6))


def _default_pc_state(display_name: str, ruleset: str = "") -> Dict[str, Any]:
    return create_pc_state_for_ruleset(display_name, ruleset or "generic")


def _pc_state_for_ruleset(
    display_name: str,
    ruleset: str,
    pc_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        current = normalize_coc_state(pc_state, display_name, ruleset)
        if isinstance(pc_state, dict):
            for key in ("npc_profile", "is_quick_npc"):
                if key in pc_state:
                    current[key] = pc_state[key]
        return current
    if pc_state:
        current = dict(pc_state)
        current.setdefault("ruleset", ruleset or "generic")
        current.setdefault("sheet_format", "generic_pc_v1")
        return current
    return create_pc_state_for_ruleset(display_name, ruleset or "generic")


def _ruleset_for_scenario(scenario: Optional[Scenario]) -> str:
    if scenario is None or getattr(scenario, "scenario_kind", "writing") != "trpg":
        return ""
    ruleset = str(getattr(scenario, "ruleset", "") or "").strip().lower()
    if ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        return ruleset
    if is_coc7_scenario(scenario.tags, scenario.genre, ruleset):
        return COC7_RULESET_TAG
    if is_coc_scenario(scenario.tags, scenario.genre, ruleset):
        return COC6_RULESET_TAG
    return ruleset or "generic"


def _next_seat_and_color(
    existing: List[ScenarioParticipant],
) -> Dict[str, Any]:
    used_seats = {p.seat_index for p in existing if p.is_active_participant}
    seat = 0
    while seat in used_seats:
        seat += 1
    color = _DEFAULT_PARTICIPANT_COLORS[seat % len(_DEFAULT_PARTICIPANT_COLORS)]
    return {"seat_index": seat, "color": color}


async def _load_room_with_children(
    session, play_session_id: uuid.UUID, log_limit: int = 100
) -> ScenarioPlaySession:
    stmt = (
        select(ScenarioPlaySession)
        .options(
            selectinload(ScenarioPlaySession.participants),
            selectinload(ScenarioPlaySession.logs),
        )
        .where(ScenarioPlaySession.id == play_session_id)
    )
    result = await session.execute(stmt)
    play_session = result.scalar_one_or_none()
    if play_session is None:
        raise RoomNotFoundError(str(play_session_id))
    return play_session


async def _hydrate_room_dict(
    session, play_session: ScenarioPlaySession, log_limit: int = 100
) -> Dict[str, Any]:
    data = play_session.to_dict(include_children=True)
    # シナリオ情報を付与
    stmt = (
        select(Scenario)
        .options(selectinload(Scenario.characters))
        .where(Scenario.id == play_session.scenario_id)
    )
    result = await session.execute(stmt)
    scenario = result.scalar_one_or_none()
    if scenario:
        data["scenario"] = scenario.to_dict()
        # キャラクター一覧も含める
        data["scenario"]["characters"] = [c.to_dict() for c in (scenario.characters or [])]
    if play_session.current_scene_id:
        scene = await session.get(ScenarioScene, play_session.current_scene_id)
        if scene:
            data["current_scene"] = scene.to_dict()
    # ログは最新から log_limit 件に制限
    if log_limit and len(data.get("logs", [])) > log_limit:
        data["logs"] = data["logs"][-log_limit:]
    return data


async def _append_log_internal(
    session,
    play_session_id: uuid.UUID,
    participant_id: Optional[uuid.UUID],
    log_type: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> ScenarioPlayLog:
    log = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=play_session_id,
        participant_id=participant_id,
        log_type=log_type,
        content=content,
        log_metadata=metadata or {},
    )
    session.add(log)
    await session.flush()
    return log
