"""TRPG マルチプレイヤープレイサービス

ココフォリア風のルーム／参加者／ログ管理と、AI GM 連携の入口を提供する。
既存の scenario_service.py はシングルプレイ用のAPIを維持する。
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, desc, func
from sqlalchemy.orm import selectinload

from ..memory.database import get_db_session
from ..memory.models import ConversationSession, User
from ..models.ecc_models import (
    Character,
    Scenario,
    ScenarioCharacter,
    ScenarioScene,
    TRPGPlayerCharacterSheet,
    ScenarioPlaySession,
    ScenarioParticipant,
    ScenarioPlayLog,
    TRPGPrivateMessage,
    TRPGRoomDisclosure,
    TRPGRulesetProfile,
)
from .trpg_rules import (
    COC6_RULESET_TAG,
    COC7_RULESET_TAG,
    create_pc_state_for_ruleset,
    evaluate_roll_for_ruleset,
    is_coc_scenario,
    is_coc7_scenario,
    resolve_roll_target_from_state,
)
from .trpg_coc import (
    apply_coc_san_loss,
    extract_coc_pc_state_from_relationships,
    is_coc_sheet,
    is_coc_san_label,
    normalize_coc_state,
    parse_coc_san_loss,
)
from .trpg_coc_system import (
    apply_coc_damage,
    apply_coc_checked_skill_development,
    apply_coc_sanity_loss,
    apply_coc_sanity_recovery,
    apply_coc_skill_development,
    apply_coc_spell_cost,
    checked_coc_development_skills,
    coc6_resistance_target,
    evaluate_coc6_resistance,
    find_coc_weapon,
    heal_coc_hp,
    mark_coc_skill_experience,
    recover_coc_mp,
    rebuild_coc_state_runtime,
    resolve_coc_attack,
    roll_coc_dice_expression,
    roll_coc_insanity_effect,
    spend_coc_mp,
)
from .trpg_rulebook_service import profile_model_to_runtime_dict
from .trpg_rule_reference_service import format_rule_reference_context, get_mechanic_rule_context

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
# ユーティリティ
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


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _parse_uuid_strict(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise TRPGPlayError(f"無効なUUID形式です: {value}")


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
            uid = _parse_uuid(raw)
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


def _viewer_can_see_disclosure(
    disclosure: TRPGRoomDisclosure,
    viewer_participant: Optional[ScenarioParticipant] = None,
    is_host: bool = False,
) -> bool:
    if is_host:
        return True
    visibility = str(getattr(disclosure, "visibility", "") or "public").lower()
    if visibility == "public":
        return True
    viewer_id = _participant_id_str(viewer_participant)
    if viewer_id and str(getattr(disclosure, "creator_participant_id", "") or "") == viewer_id:
        return True
    targets = {str(item) for item in (disclosure.target_participant_ids or [])}
    if viewer_id and viewer_id in targets:
        return True
    if GM_TARGET_ID in targets and _participant_is_gm(viewer_participant):
        return True
    if visibility == "gm" and _participant_is_gm(viewer_participant):
        return True
    return False


def _viewer_can_see_private_message(
    message: TRPGPrivateMessage,
    viewer_participant: Optional[ScenarioParticipant] = None,
    is_host: bool = False,
) -> bool:
    if is_host:
        return True
    viewer_id = _participant_id_str(viewer_participant)
    if viewer_id and str(getattr(message, "sender_participant_id", "") or "") == viewer_id:
        return True
    targets = {str(item) for item in (message.target_participant_ids or [])}
    if viewer_id and viewer_id in targets:
        return True
    if GM_TARGET_ID in targets and _participant_is_gm(viewer_participant):
        return True
    return False


async def _resolve_play_session(session, room_id_or_code: str) -> ScenarioPlaySession:
    play_session = None
    uid = _parse_uuid(room_id_or_code)
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


async def _resolve_viewer_context(
    session,
    play_session: ScenarioPlaySession,
    viewer_participant_id: Optional[str],
    user_id: Optional[str],
) -> tuple[Optional[ScenarioParticipant], bool]:
    user_uid = _parse_uuid(user_id) if user_id else None
    is_host = bool(user_uid and play_session.host_user_id == user_uid)
    participant: Optional[ScenarioParticipant] = None

    if viewer_participant_id:
        pid = _parse_uuid_strict(viewer_participant_id)
        participant = await session.get(ScenarioParticipant, pid)
        if participant is None or participant.play_session_id != play_session.id:
            raise ParticipantNotFoundError(viewer_participant_id)
        if participant.user_id and participant.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者の秘匿情報は参照できません", status_code=403)
        if participant.user_id and user_uid is None:
            raise TRPGPlayError("認証が必要です", status_code=401)
        return participant, is_host

    if user_uid:
        result = await session.execute(
            select(ScenarioParticipant)
            .where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.is_active_participant.is_(True),
            )
            .order_by(ScenarioParticipant.seat_index)
        )
        participant = result.scalars().first()
    return participant, is_host


async def require_room_view_access(
    room_id_or_code: str,
    user_id: Optional[str],
    invite_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure the authenticated user can view a room snapshot/log stream.

    A private room is visible to its host, active participants, or a user who
    presents the exact room invite code. Public rooms are visible to any
    authenticated user.
    """
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    user_uid = _parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        play_session = await _resolve_play_session(session, room_id_or_code)
        invite = str(invite_code or "").strip().upper()
        code = str(play_session.room_code or "").strip().upper()
        invited = bool(invite and code and invite == code)
        is_host = bool(play_session.host_user_id == user_uid)

        participant_result = await session.execute(
            select(ScenarioParticipant).where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.is_active_participant.is_(True),
            )
        )
        participant = participant_result.scalars().first()
        if invited or is_host or participant or bool(play_session.is_public):
            return {
                "room_id": str(play_session.id),
                "room_code": play_session.room_code,
                "is_public": bool(play_session.is_public),
                "is_host": is_host,
                "is_participant": bool(participant),
                "participant_id": str(participant.id) if participant else None,
                "invited": invited,
            }
        raise TRPGPlayError("このルームを閲覧できません", status_code=403)


async def require_room_participation_access(
    room_id_or_code: str,
    user_id: Optional[str],
    invite_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure a user can interact with a room as host or active participant."""
    access = await require_room_view_access(room_id_or_code, user_id, invite_code)
    if (
        access.get("is_host")
        or access.get("is_participant")
        or access.get("invited")
        or access.get("is_public")
    ):
        return access
    raise TRPGPlayError("このルームでは操作できません", status_code=403)


async def require_room_gm_access(
    room_id_or_code: str,
    user_id: Optional[str],
) -> Dict[str, Any]:
    """Ensure a user is the room host or an active GM participant."""
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    user_uid = _parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        play_session = await _resolve_play_session(session, room_id_or_code)
        if play_session.host_user_id == user_uid:
            return {"room_id": str(play_session.id), "is_host": True, "is_gm": True}
        gm_result = await session.execute(
            select(ScenarioParticipant).where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.role == "gm",
                ScenarioParticipant.is_active_participant.is_(True),
            )
        )
        gm = gm_result.scalars().first()
        if gm:
            return {
                "room_id": str(play_session.id),
                "is_host": False,
                "is_gm": True,
                "participant_id": str(gm.id),
            }
        raise TRPGPlayError("GMまたはホストのみ操作できます", status_code=403)


async def require_participant_write_access(
    participant_id: str,
    user_id: Optional[str],
    allow_gm: bool = True,
) -> Dict[str, Any]:
    """Ensure a user can write as or update a participant."""
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    participant_uid = _parse_uuid_strict(participant_id)
    user_uid = _parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        participant = await session.get(ScenarioParticipant, participant_uid)
        if participant is None:
            raise ParticipantNotFoundError(participant_id)
        play_session = await session.get(ScenarioPlaySession, participant.play_session_id)
        if play_session is None:
            raise RoomNotFoundError(str(participant.play_session_id))

        if participant.user_id == user_uid:
            return {
                "room_id": str(play_session.id),
                "participant_id": str(participant.id),
                "is_owner": True,
                "is_host": False,
                "is_gm": False,
            }
        if allow_gm and play_session.host_user_id == user_uid:
            return {
                "room_id": str(play_session.id),
                "participant_id": str(participant.id),
                "is_owner": False,
                "is_host": True,
                "is_gm": True,
            }
        if allow_gm:
            gm_result = await session.execute(
                select(ScenarioParticipant).where(
                    ScenarioParticipant.play_session_id == play_session.id,
                    ScenarioParticipant.user_id == user_uid,
                    ScenarioParticipant.role == "gm",
                    ScenarioParticipant.is_active_participant.is_(True),
                )
            )
            gm = gm_result.scalars().first()
            if gm:
                return {
                    "room_id": str(play_session.id),
                    "participant_id": str(participant.id),
                    "is_owner": False,
                    "is_host": False,
                    "is_gm": True,
                }
        raise TRPGPlayError("この参加者として操作できません", status_code=403)


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


_QUICK_NPC_FAMILY_NAMES = [
    "浅見",
    "有坂",
    "一ノ瀬",
    "榎本",
    "奥村",
    "神谷",
    "桐生",
    "黒瀬",
    "久我",
    "佐伯",
    "篠原",
    "高槻",
    "成瀬",
    "鳴海",
    "日下部",
    "藤堂",
    "水瀬",
    "八代",
]
_QUICK_NPC_GIVEN_NAMES = [
    "葵",
    "伊織",
    "奏",
    "景",
    "志乃",
    "千尋",
    "透",
    "直",
    "昴",
    "遥",
    "真琴",
    "湊",
    "悠",
    "怜",
    "蓮",
]


def _clean_suggested_npc_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json|text)?|```$", "", text).strip()
    text = text.splitlines()[0].strip() if text else ""
    text = re.sub(r"^(?:名前|NPC名|提案名)\s*[:：]\s*", "", text).strip()
    text = text.strip("「」『』\"'`、。., ")
    if " " in text:
        parts = [part for part in text.split(" ") if part]
        text = " ".join(parts[:2])
    return text[:32]


def _fallback_quick_npc_name(existing_names: set[str]) -> str:
    for _ in range(80):
        name = f"{random.choice(_QUICK_NPC_FAMILY_NAMES)} {random.choice(_QUICK_NPC_GIVEN_NAMES)}"
        if name not in existing_names:
            return name
    return f"即席NPC {len(existing_names) + 1}"


def _quick_npc_fallback_profile(name: str, theme: str, scenario_title: str) -> Dict[str, Any]:
    role = str(theme or "").strip() or "その場で登場する協力者"
    background = f"{scenario_title or 'このシナリオ'}の状況に偶然関わった人物。"
    return {
        "name": name,
        "role": role[:80],
        "occupation": "関係者",
        "age": "30代",
        "sex": "",
        "appearance": "落ち着いた服装で、周囲をよく観察している。",
        "background": background,
        "personality": "慎重で、必要なことだけを短く話す。",
        "motivation": "自分の安全を確保しつつ、状況の真相を知りたい。",
        "secret": "まだ誰にも話していない小さな手掛かりを持っている。",
        "speaking_style": "丁寧語。緊張すると言葉を選ぶ。",
        "items": ["スマートフォン", "メモ帳", "筆記具"],
        "skills": {
            "目星": 55,
            "聞き耳": 50,
            "図書館": 45,
            "心理学": 45,
            "説得": 40,
        },
        "stats": {
            "STR": 45,
            "CON": 50,
            "POW": 55,
            "DEX": 50,
            "APP": 50,
            "SIZ": 55,
            "INT": 60,
            "EDU": 60,
        },
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, dict) else {}


def _quick_npc_profile_to_pc_state(
    name: str,
    ruleset: str,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    skills = profile.get("skills") if isinstance(profile.get("skills"), dict) else {}
    stats = profile.get("stats") if isinstance(profile.get("stats"), dict) else {}
    items = profile.get("items") if isinstance(profile.get("items"), list) else []
    personal = {
        "name": name,
        "role": str(profile.get("role") or ""),
        "background": str(profile.get("background") or ""),
        "personality": str(profile.get("personality") or ""),
        "motivation": str(profile.get("motivation") or ""),
        "secret": str(profile.get("secret") or ""),
        "appearance": str(profile.get("appearance") or ""),
        "speaking_style": str(profile.get("speaking_style") or ""),
    }
    raw_state = {
        "name": name,
        "occupation": str(profile.get("occupation") or personal["role"] or "NPC"),
        "age": str(profile.get("age") or ""),
        "sex": str(profile.get("sex") or ""),
        "personal": personal,
        "characteristics": stats,
        "stats": stats,
        "skills": skills,
        "items": [str(item) for item in items if str(item).strip()],
        "notes": "\n".join(
            [
                f"役割: {personal['role']}",
                f"背景: {personal['background']}",
                f"性格: {personal['personality']}",
                f"目的: {personal['motivation']}",
                f"秘密: {personal['secret']}",
                f"口調: {personal['speaking_style']}",
            ]
        ).strip(),
        "npc_profile": personal,
        "is_quick_npc": True,
    }
    if ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        state = normalize_coc_state(raw_state, name, ruleset)
    else:
        state = create_pc_state_for_ruleset(name, ruleset or "generic")
        state.update(
            {
                "stats": stats,
                "skills": skills,
                "items": raw_state["items"],
                "notes": raw_state["notes"],
                "npc_profile": personal,
                "is_quick_npc": True,
            }
        )
    state["npc_profile"] = personal
    state["is_quick_npc"] = True
    return state


async def suggest_quick_npc_name(
    room_id_or_code: str,
    theme: str = "",
    name: str = "",
) -> Dict[str, Any]:
    """Suggest a one-off NPC profile for the room scenario."""
    async with await get_db_session() as session:
        play_session = await _resolve_play_session(session, room_id_or_code)
        scenario = await session.get(Scenario, play_session.scenario_id)
        participant_result = await session.execute(
            select(ScenarioParticipant).where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.is_active_participant.is_(True),
            )
        )
        participants = participant_result.scalars().all()
        character_result = await session.execute(
            select(ScenarioCharacter).where(
                ScenarioCharacter.scenario_id == play_session.scenario_id
            )
        )
        characters = character_result.scalars().all()

    existing_names = {
        str(name).strip()
        for name in [
            *(p.display_name for p in participants),
            *(c.name for c in characters),
        ]
        if str(name or "").strip()
    }
    preferred_name = _clean_suggested_npc_name(name)
    fallback_name = (
        preferred_name
        if preferred_name and preferred_name not in existing_names
        else _fallback_quick_npc_name(existing_names)
    )
    scenario_title = getattr(scenario, "title", "") if scenario else ""
    scenario_desc = getattr(scenario, "description", "") if scenario else ""
    ruleset = _ruleset_for_scenario(scenario)
    prompt = (
        "TRPG卓にその場で追加するNPCを日本語で1人だけ生成し、JSONだけを返してください。\n"
        "条件:\n"
        "- 説明文やMarkdownは禁止。JSONオブジェクトだけを出力。\n"
        "- 現代日本の心理戦シナリオに馴染む、人名として自然な姓名。\n"
        "- 既存参加者や既存NPCと重複しない。\n"
        "- 背景、性格、目的、秘密、口調、所持品、主要技能、ステータスを作る。\n"
        "- statsはSTR/CON/POW/DEX/APP/SIZ/INT/EDUを30から80の整数で入れる。\n"
        "- skillsは技能名をキー、1から90の整数を値にする。主要技能を5から8個。\n"
        "- JSONキー: name, role, occupation, age, sex, appearance, background, personality, motivation, secret, speaking_style, items, skills, stats\n"
        f"シナリオ: {scenario_title}\n"
        f"ルールセット: {ruleset or 'generic'}\n"
        f"概要: {scenario_desc[:500]}\n"
        f"追加メモ: {str(theme or '').strip()[:180]}\n"
        f"指定名: {preferred_name}\n"
        f"既存名: {', '.join(sorted(existing_names))[:900]}\n"
    )
    try:
        from agents import Agent, Runner

        agent = Agent(
            name="trpg_quick_npc_profile_generator",
            instructions=(
                "あなたはTRPG卓の即席NPCを作る補助AIです。"
                "返答は必ずJSONオブジェクト1つだけにします。"
            ),
            model="gpt-4o-mini",
        )
        result = await Runner.run(agent, prompt)
        profile = _extract_json_object(result.final_output or "")
        generated_name = _clean_suggested_npc_name(str(profile.get("name") or ""))
        if preferred_name:
            generated_name = preferred_name
        if generated_name and generated_name not in existing_names:
            profile["name"] = generated_name
            pc_state = _quick_npc_profile_to_pc_state(generated_name, ruleset, profile)
            return {
                "name": generated_name,
                "source": "ai",
                "profile": profile,
                "pc_state": pc_state,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Quick NPC profile suggestion fell back: %s", exc)

    profile = _quick_npc_fallback_profile(fallback_name, theme, scenario_title)
    pc_state = _quick_npc_profile_to_pc_state(fallback_name, ruleset, profile)
    return {
        "name": fallback_name,
        "source": "fallback",
        "profile": profile,
        "pc_state": pc_state,
    }


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


def _sheet_owner_user_id(character: ScenarioCharacter) -> Optional[str]:
    relationships = character.relationships or []
    if not isinstance(relationships, list):
        return None
    for rel in relationships:
        if isinstance(rel, dict) and rel.get("type") == "owner_user":
            user_id = rel.get("user_id")
            return str(user_id) if user_id else None
    return None


def _legacy_sheet_to_player_sheet_dict(character: ScenarioCharacter) -> Dict[str, Any]:
    data = character.to_dict()
    data["sheet_source"] = "legacy_scenario_character"
    data["sheet_metadata"] = data.get("sheet_metadata") or {}
    return data


async def _load_player_character_sheet(
    session,
    sheet_id: uuid.UUID,
    scenario_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[TRPGPlayerCharacterSheet]:
    sheet = await session.get(TRPGPlayerCharacterSheet, sheet_id)
    if sheet is None or sheet.scenario_id != scenario_id or sheet.user_id != user_id:
        return None
    return sheet


async def _load_legacy_player_character_sheet(
    session,
    sheet_id: uuid.UUID,
    scenario_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[ScenarioCharacter]:
    selected = await session.get(ScenarioCharacter, sheet_id)
    if (
        selected is None
        or selected.scenario_id != scenario_id
        or _sheet_owner_user_id(selected) != str(user_id)
    ):
        return None
    return selected


async def _upsert_player_character_sheet(
    session,
    scenario_id: uuid.UUID,
    user_id: uuid.UUID,
    ruleset: str,
    name: str,
    pc_state: Dict[str, Any],
    avatar_url: str = "",
    existing_sheet: Optional[TRPGPlayerCharacterSheet] = None,
) -> TRPGPlayerCharacterSheet:
    sheet = existing_sheet
    if sheet is None:
        result = await session.execute(
            select(TRPGPlayerCharacterSheet)
            .where(
                TRPGPlayerCharacterSheet.scenario_id == scenario_id,
                TRPGPlayerCharacterSheet.user_id == user_id,
                TRPGPlayerCharacterSheet.ruleset == ruleset,
                TRPGPlayerCharacterSheet.name == name,
            )
            .order_by(TRPGPlayerCharacterSheet.updated_at.desc())
        )
        sheet = result.scalars().first()
    if sheet is None:
        sheet = TRPGPlayerCharacterSheet(
            id=uuid.uuid4(),
            scenario_id=scenario_id,
            user_id=user_id,
        )
        session.add(sheet)
    sheet.ruleset = ruleset
    sheet.name = name
    sheet.description = "プレイヤーキャラクター"
    sheet.trpg_pc_state = pc_state or {}
    metadata = dict(sheet.sheet_metadata or {})
    metadata["source"] = "join_room"
    metadata["avatar_url"] = avatar_url or ""
    sheet.sheet_metadata = metadata
    sheet.updated_at = datetime.utcnow()
    return sheet


async def list_player_character_sheets(
    room_id_or_code: str,
    user_id: str,
) -> List[Dict[str, Any]]:
    """Return scenario character sheets saved by this user for the room scenario."""
    async with await get_db_session() as session:
        play_session = None
        uid = _parse_uuid(room_id_or_code)
        if uid:
            result = await session.execute(
                select(ScenarioPlaySession).where(ScenarioPlaySession.id == uid)
            )
            play_session = result.scalar_one_or_none()
        if play_session is None:
            result = await session.execute(
                select(ScenarioPlaySession).where(
                    ScenarioPlaySession.room_code == room_id_or_code.upper()
                )
            )
            play_session = result.scalar_one_or_none()
        if play_session is None:
            raise RoomNotFoundError(room_id_or_code)

        result = await session.execute(
            select(TRPGPlayerCharacterSheet)
            .where(
                TRPGPlayerCharacterSheet.scenario_id == play_session.scenario_id,
                TRPGPlayerCharacterSheet.user_id == _parse_uuid_strict(user_id),
            )
            .order_by(TRPGPlayerCharacterSheet.updated_at.desc(), TRPGPlayerCharacterSheet.name)
        )
        sheets = [sheet.to_dict() for sheet in result.scalars().all()]

        legacy_result = await session.execute(
            select(ScenarioCharacter)
            .where(ScenarioCharacter.scenario_id == play_session.scenario_id)
            .order_by(ScenarioCharacter.sort_order, ScenarioCharacter.name)
        )
        for character in legacy_result.scalars().all():
            if _sheet_owner_user_id(character) == user_id:
                sheets.append(_legacy_sheet_to_player_sheet_dict(character))
        return sheets


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


# ────────────────────────────────────────────
# ルーム作成 / 一覧 / 取得
# ────────────────────────────────────────────


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
    scenario_uid = _parse_uuid_strict(scenario_id)
    host_uid = _parse_uuid_strict(host_user_id)

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
            user_uid = _parse_uuid(user_id)
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
        uid = _parse_uuid(room_id_or_code)
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
    uid = _parse_uuid_strict(room_id)
    user_uid = _parse_uuid_strict(user_id)

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
    room_uid = _parse_uuid_strict(room_id)
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


# ────────────────────────────────────────────
# 参加者管理
# ────────────────────────────────────────────


async def join_room(
    room_id_or_code: str,
    user_id: Optional[str],
    display_name: str,
    character_id: Optional[str] = None,
    role: str = "player",
    pc_state: Optional[Dict[str, Any]] = None,
    avatar_url: str = "",
    scenario_character_id: Optional[str] = None,
    save_character_sheet: bool = True,
) -> Dict[str, Any]:
    """ルームに参加する。"""
    async with await get_db_session() as session:
        # ルーム解決（participants を eager load して lazy load エラーを防ぐ）
        play_session = None
        uid = _parse_uuid(room_id_or_code)
        if uid:
            _r = await session.execute(
                select(ScenarioPlaySession)
                .options(selectinload(ScenarioPlaySession.participants))
                .where(ScenarioPlaySession.id == uid)
            )
            play_session = _r.scalar_one_or_none()
        if play_session is None:
            _r2 = await session.execute(
                select(ScenarioPlaySession)
                .options(selectinload(ScenarioPlaySession.participants))
                .where(
                    ScenarioPlaySession.room_code == room_id_or_code.upper()
                )
            )
            play_session = _r2.scalar_one_or_none()
        if play_session is None:
            raise RoomNotFoundError(room_id_or_code)

        scenario = await session.get(Scenario, play_session.scenario_id)
        ruleset = _ruleset_for_scenario(scenario)
        user_uid = _parse_uuid(user_id) if user_id else None
        char_uid = _parse_uuid(character_id) if character_id else None
        scenario_char_uid = _parse_uuid(scenario_character_id) if scenario_character_id else None
        selected_player_sheet: Optional[TRPGPlayerCharacterSheet] = None
        if scenario_char_uid and user_uid:
            selected_player_sheet = await _load_player_character_sheet(
                session,
                scenario_char_uid,
                play_session.scenario_id,
                user_uid,
            )
            legacy_sheet = None
            if selected_player_sheet is None:
                legacy_sheet = await _load_legacy_player_character_sheet(
                    session,
                    scenario_char_uid,
                    play_session.scenario_id,
                    user_uid,
                )
            if selected_player_sheet is None and legacy_sheet is None:
                raise TRPGPlayError(
                    "選択したキャラクターシートを使用できません",
                    status_code=403,
                )
            selected = selected_player_sheet or legacy_sheet
            display_name = selected.name
            if not pc_state:
                pc_state = selected.trpg_pc_state or None
            if (
                not avatar_url
                and selected_player_sheet is not None
                and isinstance(selected_player_sheet.sheet_metadata, dict)
            ):
                avatar_url = str(selected_player_sheet.sheet_metadata.get("avatar_url") or "")

        # 既に参加者として居るか確認（復帰サポート）
        existing_stmt = select(ScenarioParticipant).where(
            ScenarioParticipant.play_session_id == play_session.id
        )
        if user_uid:
            existing_stmt = existing_stmt.where(
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.participant_kind == "human",
            )
        elif char_uid:
            existing_stmt = existing_stmt.where(
                ScenarioParticipant.character_id == char_uid,
                ScenarioParticipant.participant_kind == "ai_character",
            )
        else:
            existing_stmt = existing_stmt.where(
                ScenarioParticipant.display_name == display_name,
                ScenarioParticipant.role == role,
            )
        existing_result = await session.execute(existing_stmt)
        existing = existing_result.scalars().first()

        if existing:
            # 再入室
            existing.is_active_participant = True
            existing.is_connected = True
            existing.last_seen_at = datetime.utcnow()
            if display_name:
                existing.display_name = display_name
            if avatar_url:
                existing.avatar_url = avatar_url
            if (
                user_uid
                and role == "player"
                and ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}
                and save_character_sheet
            ):
                await _upsert_player_character_sheet(
                    session,
                    play_session.scenario_id,
                    user_uid,
                    ruleset,
                    existing.display_name,
                    existing.pc_state or {},
                    existing.avatar_url or "",
                    selected_player_sheet,
                )
            await session.commit()
            await session.refresh(existing)
            await _append_log_internal(
                session,
                play_session.id,
                existing.id,
                "system",
                f"{existing.display_name} が再入室しました。",
                {"event": "rejoin"},
            )
            await session.commit()
            return existing.to_dict()

        # 新規参加
        # 定員チェック（player のみ）
        player_count = sum(
            1
            for p in (play_session.participants or [])
            if p.is_active_participant and p.role == "player"
        )
        if role == "player" and player_count >= (play_session.max_players or 4):
            raise RoomFullError(str(play_session.id), play_session.max_players)

        seat_color = _next_seat_and_color(play_session.participants or [])
        kind = "ai_character" if ((char_uid or role == "npc") and not user_uid) else "human"

        effective_pc_state = pc_state
        if ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG} and role == "npc" and not effective_pc_state:
            scenario_character = None
            if char_uid:
                character_result = await session.execute(
                    select(ScenarioCharacter).where(
                        ScenarioCharacter.scenario_id == play_session.scenario_id,
                        ScenarioCharacter.character_id == char_uid,
                    )
                )
                scenario_character = character_result.scalar_one_or_none()
            if scenario_character is None:
                character_result = await session.execute(
                    select(ScenarioCharacter).where(
                        ScenarioCharacter.scenario_id == play_session.scenario_id,
                        ScenarioCharacter.name == display_name,
                    )
                )
                scenario_character = character_result.scalar_one_or_none()
            if scenario_character is not None:
                effective_pc_state = scenario_character.trpg_pc_state or extract_coc_pc_state_from_relationships(
                    scenario_character.relationships or []
                )

        participant = ScenarioParticipant(
            id=uuid.uuid4(),
            play_session_id=play_session.id,
            user_id=user_uid,
            character_id=char_uid,
            display_name=display_name,
            role=role,
            participant_kind=kind,
            avatar_url=avatar_url,
            color=seat_color["color"],
            seat_index=seat_color["seat_index"],
            pc_state=_pc_state_for_ruleset(display_name, ruleset, effective_pc_state),
            is_active_participant=True,
            is_connected=True,
            joined_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
        session.add(participant)
        await session.flush()

        if (
            user_uid
            and role == "player"
            and ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}
            and save_character_sheet
        ):
            await _upsert_player_character_sheet(
                session,
                play_session.scenario_id,
                user_uid,
                ruleset,
                display_name,
                participant.pc_state or {},
                avatar_url,
                selected_player_sheet,
            )

        # ログ追加
        await _append_log_internal(
            session,
            play_session.id,
            participant.id,
            "system",
            f"{display_name} がルームに参加しました。",
            {"event": "join", "role": role, "kind": kind},
        )

        await session.commit()
        await session.refresh(participant)
        logger.info(
            "参加者追加: room=%s, display_name=%s, kind=%s",
            play_session.id,
            display_name,
            kind,
        )
        return participant.to_dict()


async def leave_room(
    room_id: str,
    participant_id: str,
    disconnect_only: bool = False,
) -> None:
    """参加者を退出または切断させる。"""
    room_uid = _parse_uuid_strict(room_id)
    pid = _parse_uuid_strict(participant_id)

    async with await get_db_session() as session:
        participant = await session.get(ScenarioParticipant, pid)
        if participant is None or participant.play_session_id != room_uid:
            raise ParticipantNotFoundError(participant_id)

        if disconnect_only:
            participant.is_connected = False
            participant.last_seen_at = datetime.utcnow()
            event = "disconnect"
            content = f"{participant.display_name} が接続を切りました。"
        else:
            participant.is_active_participant = False
            participant.is_connected = False
            participant.last_seen_at = datetime.utcnow()
            event = "leave"
            content = f"{participant.display_name} が退出しました。"

        await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "system",
            content,
            {"event": event},
        )
        await session.commit()


async def update_participant(
    participant_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """参加者情報（PC状態・表示名・色など）を更新する。"""
    pid = _parse_uuid_strict(participant_id)

    async with await get_db_session() as session:
        participant = await session.get(ScenarioParticipant, pid)
        if participant is None:
            raise ParticipantNotFoundError(participant_id)

        if "display_name" in updates and updates["display_name"]:
            participant.display_name = updates["display_name"]
        if "avatar_url" in updates:
            participant.avatar_url = updates["avatar_url"] or ""
        if "color" in updates and updates["color"]:
            participant.color = updates["color"]
        if "role" in updates and updates["role"]:
            participant.role = updates["role"]
        if "pc_state" in updates and isinstance(updates["pc_state"], dict):
            # マージ（上書きではなく追加更新）
            current = participant.pc_state or {}
            current.update(updates["pc_state"])
            if is_coc_sheet(current):
                participant.pc_state = normalize_coc_state(
                    current,
                    participant.display_name,
                    str(current.get("ruleset") or COC6_RULESET_TAG),
                )
            else:
                participant.pc_state = current
        if "seat_index" in updates:
            participant.seat_index = int(updates["seat_index"])
        if "is_connected" in updates:
            participant.is_connected = bool(updates["is_connected"])

        participant.last_seen_at = datetime.utcnow()
        if "avatar_url" in updates and participant.user_id and participant.role == "player":
            play_session = await session.get(ScenarioPlaySession, participant.play_session_id)
            scenario = await session.get(Scenario, play_session.scenario_id) if play_session else None
            ruleset = _ruleset_for_scenario(scenario)
            if play_session and ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
                await _upsert_player_character_sheet(
                    session,
                    play_session.scenario_id,
                    participant.user_id,
                    ruleset,
                    participant.display_name,
                    participant.pc_state or {},
                    participant.avatar_url or "",
                )
        await session.commit()
        await session.refresh(participant)
        return participant.to_dict()


# ────────────────────────────────────────────
# ログ追加
# ────────────────────────────────────────────


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


async def append_log(
    room_id: str,
    log_type: str,
    content: str,
    participant_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """任意のログをルームに追加する。"""
    room_uid = _parse_uuid_strict(room_id)
    pid = _parse_uuid(participant_id) if participant_id else None

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        log = await _append_log_internal(
            session, room_uid, pid, log_type, content, metadata
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(log)
        return log.to_dict()


async def list_logs(
    room_id: str,
    limit: int = 200,
    before_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """ログをページング取得する（新しい順）。"""
    room_uid = _parse_uuid_strict(room_id)
    async with await get_db_session() as session:
        stmt = (
            select(ScenarioPlayLog)
            .where(ScenarioPlayLog.play_session_id == room_uid)
            .order_by(desc(ScenarioPlayLog.created_at))
            .limit(limit)
        )
        if before_id:
            before_uid = _parse_uuid(before_id)
            if before_uid:
                anchor = await session.get(ScenarioPlayLog, before_uid)
                if anchor:
                    stmt = stmt.where(ScenarioPlayLog.created_at < anchor.created_at)
        result = await session.execute(stmt)
        logs = result.scalars().all()
        return [log.to_dict() for log in reversed(logs)]


# ────────────────────────────────────────────
# 開示情報 / 個別チャット
# ────────────────────────────────────────────


async def list_disclosures(
    room_id: str,
    viewer_participant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    room_uid = _parse_uuid_strict(room_id)
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        viewer, is_host = await _resolve_viewer_context(
            session, play_session, viewer_participant_id, user_id
        )
        result = await session.execute(
            select(TRPGRoomDisclosure)
            .where(TRPGRoomDisclosure.play_session_id == room_uid)
            .order_by(desc(TRPGRoomDisclosure.is_pinned), TRPGRoomDisclosure.created_at)
        )
        return [
            disclosure.to_dict()
            for disclosure in result.scalars().all()
            if _viewer_can_see_disclosure(disclosure, viewer, is_host)
        ]


async def create_disclosure(
    room_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    visibility = str(payload.get("visibility") or "public").strip().lower()
    if visibility not in DISCLOSURE_VISIBILITIES:
        raise TRPGPlayError("開示範囲が不正です", status_code=400)
    disclosure_type = str(payload.get("disclosure_type") or "handout").strip().lower()
    if disclosure_type not in DISCLOSURE_TYPES:
        disclosure_type = "note"
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    image_url = str(payload.get("image_url") or "").strip()
    image_path = str(payload.get("image_path") or "").strip()
    if not title:
        raise TRPGPlayError("開示情報のタイトルは必須です", status_code=400)
    if not content and not image_url and not image_path:
        raise TRPGPlayError("本文または画像URL/パスを入力してください", status_code=400)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        creator_id = _parse_uuid(payload.get("creator_participant_id"))
        if creator_id is None:
            raise TRPGPlayError("開示情報の作成者が必要です", status_code=400)
        creator = await session.get(ScenarioParticipant, creator_id)
        if creator is None or creator.play_session_id != room_uid:
            raise ParticipantNotFoundError(str(creator_id))
        user_uid = _parse_uuid(user_id) if user_id else None
        is_host = bool(user_uid and play_session.host_user_id == user_uid)
        if creator.user_id and creator.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者として開示できません", status_code=403)
        targets = _normalize_target_ids(payload.get("target_participant_ids"))
        if visibility == "private" and not targets:
            raise TRPGPlayError("個別開示には宛先が必要です", status_code=400)

        disclosure = TRPGRoomDisclosure(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            creator_participant_id=creator.id if creator else None,
            disclosure_type=disclosure_type,
            visibility=visibility,
            target_participant_ids=targets,
            title=title,
            content=content,
            image_url=image_url,
            image_path=image_path,
            tags=[
                str(tag).strip()
                for tag in (payload.get("tags") or [])
                if str(tag).strip()
            ],
            disclosure_metadata=payload.get("metadata") or {},
            is_pinned=bool(payload.get("is_pinned")),
        )
        session.add(disclosure)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(disclosure)
        return disclosure.to_dict()


async def list_private_messages(
    room_id: str,
    viewer_participant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    room_uid = _parse_uuid_strict(room_id)
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        viewer, is_host = await _resolve_viewer_context(
            session, play_session, viewer_participant_id, user_id
        )
        result = await session.execute(
            select(TRPGPrivateMessage)
            .where(TRPGPrivateMessage.play_session_id == room_uid)
            .order_by(desc(TRPGPrivateMessage.created_at))
            .limit(limit)
        )
        messages = [
            message
            for message in result.scalars().all()
            if _viewer_can_see_private_message(message, viewer, is_host)
        ]
        return [message.to_dict() for message in reversed(messages)]


async def send_private_message(
    room_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    content = str(payload.get("content") or "").strip()
    if not content:
        raise TRPGPlayError("メッセージ本文は必須です", status_code=400)
    targets = _normalize_target_ids(payload.get("target_participant_ids"))
    if not targets:
        raise TRPGPlayError("個別チャットには宛先が必要です", status_code=400)
    message_type = str(payload.get("message_type") or "private").strip().lower()
    if message_type not in {"private", "gm", "mention"}:
        message_type = "private"

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        sender_id = _parse_uuid(payload.get("sender_participant_id"))
        if sender_id is None:
            raise TRPGPlayError("送信者が必要です", status_code=400)
        sender = await session.get(ScenarioParticipant, sender_id)
        if sender is None or sender.play_session_id != room_uid:
            raise ParticipantNotFoundError(str(sender_id))
        user_uid = _parse_uuid(user_id) if user_id else None
        is_host = bool(user_uid and play_session.host_user_id == user_uid)
        if sender.user_id and sender.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者として送信できません", status_code=403)

        message = TRPGPrivateMessage(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            sender_participant_id=sender.id,
            sender_label=sender.display_name,
            target_participant_ids=targets,
            message_type="gm" if GM_TARGET_ID in targets else message_type,
            content=content,
            message_metadata=payload.get("metadata") or {},
        )
        session.add(message)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(message)
        return message.to_dict()


async def append_private_message_internal(
    room_id: str,
    sender_participant_id: Optional[str],
    sender_label: str,
    target_participant_ids: List[str],
    content: str,
    message_type: str = "private",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    sender_uid = _parse_uuid(sender_participant_id) if sender_participant_id else None
    targets = _normalize_target_ids(target_participant_ids)
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        message = TRPGPrivateMessage(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            sender_participant_id=sender_uid,
            sender_label=sender_label,
            target_participant_ids=targets,
            message_type=message_type,
            content=content,
            message_metadata=metadata or {},
        )
        session.add(message)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(message)
        return message.to_dict()


# ────────────────────────────────────────────
# ダイスロール
# ────────────────────────────────────────────


_DICE_PATTERN = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+)\s*([+-]\s*\d+)?\s*$")


def roll_dice_expression(
    expression: str,
) -> Dict[str, Any]:
    """ "2d6+3" のようなダイス式をロールする。"""
    match = _DICE_PATTERN.match(expression.strip())
    if not match:
        raise TRPGPlayError(f"ダイス式のパースに失敗しました: {expression}")

    count_s, faces_s, modifier_s = match.groups()
    count = int(count_s) if count_s else 1
    faces = int(faces_s)
    modifier = int(modifier_s.replace(" ", "")) if modifier_s else 0

    if count < 1 or count > 100 or faces < 1 or faces > 10000:
        raise TRPGPlayError(f"ダイス数・面数が不正です: {expression}")

    rolls = [random.randint(1, faces) for _ in range(count)]
    total = sum(rolls) + modifier
    return {
        "expression": expression,
        "count": count,
        "faces": faces,
        "modifier": modifier,
        "rolls": rolls,
        "total": total,
    }


def _roll_static_or_dice_expression(expression: str) -> Dict[str, Any]:
    expr = str(expression or "0").strip()
    if re.fullmatch(r"\d+", expr):
        value = int(expr)
        return {
            "expression": expr,
            "count": 0,
            "faces": 0,
            "modifier": 0,
            "rolls": [],
            "total": value,
        }
    return roll_dice_expression(expr)


async def roll_dice_in_room(
    room_id: str,
    participant_id: Optional[str],
    expression: str,
    target: Optional[int] = None,
    difficulty: str = "regular",
    note: str = "",
) -> Dict[str, Any]:
    """ルーム内でダイスを振り、判定結果をログに追加する。"""
    room_uid = _parse_uuid_strict(room_id)
    pid = _parse_uuid(participant_id) if participant_id else None

    # 表示用テキスト
    display_name = ""
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        scenario = await session.get(Scenario, play_session.scenario_id)
        ruleset = _ruleset_for_scenario(scenario)
        profile = await session.get(TRPGRulesetProfile, ruleset)
        runtime_profile = profile_model_to_runtime_dict(profile)

        participant = None
        san_check: Dict[str, Any] = {}
        if pid:
            participant = await session.get(ScenarioParticipant, pid)
            if participant:
                display_name = participant.display_name
                pc_state = participant.pc_state or {}
                if target is None and note and is_coc_sheet(pc_state) and is_coc_san_label(note):
                    target = resolve_roll_target_from_state(
                        ruleset,
                        pc_state,
                        "SAN",
                        runtime_profile,
                    )
                if target is None and note:
                    target = resolve_roll_target_from_state(
                        ruleset,
                        pc_state,
                        note,
                        runtime_profile,
                    )

        roll = roll_dice_expression(expression)
        success: Optional[bool] = None
        ruleset_result: Dict[str, Any] = {}
        if target is not None:
            evaluated = evaluate_roll_for_ruleset(
                ruleset,
                roll,
                target,
                difficulty,
                runtime_profile,
            )
            success = evaluated["success"]
            ruleset_result = evaluated["details"]

        if (
            participant is not None
            and success is not None
            and roll.get("count") == 1
            and roll.get("faces") == 100
            and is_coc_sheet(participant.pc_state or {})
            and note
            and is_coc_san_label(note)
        ):
            loss_pair = parse_coc_san_loss(note)
            if loss_pair:
                loss_expr = loss_pair["success"] if success else loss_pair["failure"]
                loss_roll = _roll_static_or_dice_expression(loss_expr)
                before_san = int((participant.pc_state or {}).get("sanity") or 0)
                participant.pc_state = apply_coc_san_loss(
                    participant.pc_state or {},
                    int(loss_roll["total"]),
                )
                after_san = int((participant.pc_state or {}).get("sanity") or 0)
                san_check = {
                    "loss_options": loss_pair,
                    "loss_expression": loss_expr,
                    "loss_roll": loss_roll,
                    "loss": int(loss_roll["total"]),
                    "before": before_san,
                    "after": after_san,
                }

        rolls_str = " + ".join(str(r) for r in roll["rolls"])
        mod_str = ""
        if roll["modifier"]:
            mod_str = f" {'+' if roll['modifier'] > 0 else '-'} {abs(roll['modifier'])}"
        content_parts = [
            f"🎲 {display_name + ' が ' if display_name else ''}{expression} → ({rolls_str}){mod_str} = {roll['total']}"
        ]
        if target is not None:
            if ruleset_result.get("ruleset") == COC7_RULESET_TAG and ruleset_result.get("success_label"):
                content_parts.append(
                    f"[{ruleset_result['difficulty_label']}目標 "
                    f"{ruleset_result['difficulty_target']}/{target}] "
                    f"→ {ruleset_result['success_label']}"
                )
            elif ruleset_result.get("ruleset") == COC6_RULESET_TAG and ruleset_result.get("success_label"):
                content_parts.append(f"[目標 {target}] → {ruleset_result['success_label']}")
            else:
                content_parts.append(f"[目標 {target}] → {'成功' if success else '失敗'}")
        if san_check:
            content_parts.append(
                f"[SAN -{san_check['loss']} ({san_check['before']}→{san_check['after']})]"
            )
        if note:
            content_parts.append(f"『{note}』")
        content = " ".join(content_parts)
        rule_reference: Dict[str, Any] = {}
        if target is not None and roll.get("count") == 1 and roll.get("faces") == 100:
            rule_reference = await _mechanic_rule_reference(
                ruleset,
                "evaluate_coc6_d100",
                note or "1d100 check",
            )

        log = await _append_log_internal(
            session,
            room_uid,
            pid,
            "dice",
            content,
            {
                **roll,
                "target": target,
                "difficulty": difficulty,
                "success": success,
                "ruleset": ruleset,
                "ruleset_result": ruleset_result,
                "coc7": ruleset_result if ruleset_result.get("ruleset") == COC7_RULESET_TAG else {},
                "san_check": san_check,
                "note": note,
                "rule_reference": rule_reference,
            },
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(log)
        return log.to_dict()


async def _load_coc_participant_for_update(session, room_uid: uuid.UUID, participant_uid: uuid.UUID):
    play_session = await session.get(ScenarioPlaySession, room_uid)
    if play_session is None:
        raise RoomNotFoundError(str(room_uid))
    participant = await session.get(ScenarioParticipant, participant_uid)
    if participant is None or participant.play_session_id != room_uid:
        raise ParticipantNotFoundError(str(participant_uid))
    if not is_coc_sheet(participant.pc_state or {}):
        raise TRPGPlayError("CoCキャラクターシートを持つ参加者ではありません", status_code=400)
    scenario = await session.get(Scenario, play_session.scenario_id)
    ruleset = _ruleset_for_scenario(scenario)
    return play_session, participant, ruleset


def _finalize_coc_participant_state(participant: ScenarioParticipant, ruleset: str, state: Dict[str, Any]) -> None:
    rebuilt = rebuild_coc_state_runtime(state)
    participant.pc_state = normalize_coc_state(
        rebuilt,
        participant.display_name,
        str(rebuilt.get("ruleset") or ruleset or COC6_RULESET_TAG),
    )


async def _save_coc_player_sheet_from_participant(
    session,
    play_session: ScenarioPlaySession,
    participant: ScenarioParticipant,
    ruleset: str,
) -> None:
    if not participant.user_id or participant.role != "player":
        return
    if ruleset not in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        return
    await _upsert_player_character_sheet(
        session,
        play_session.scenario_id,
        participant.user_id,
        ruleset,
        participant.display_name,
        participant.pc_state or {},
        participant.avatar_url or "",
    )


async def _mechanic_rule_reference(
    ruleset: str,
    mechanic_key: str,
    query: str = "",
) -> Dict[str, Any]:
    try:
        bundle = await get_mechanic_rule_context(ruleset, mechanic_key, query=query, limit=3)
        return {
            "mechanic_key": mechanic_key,
            "rules": bundle.get("rules", []),
            "context": format_rule_reference_context(bundle, max_excerpt_chars=420),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("TRPG mechanic rule lookup skipped: %s", exc)
        return {"mechanic_key": mechanic_key, "rules": [], "context": ""}


def _short_coc_event_content(name: str, event: Dict[str, Any], reason: str = "") -> str:
    event_type = event.get("type")
    suffix = f"（{reason}）" if reason else ""
    if event_type == "damage":
        return f"{name} HP {event['before']}→{event['after']} / ダメージ {event['amount']}{suffix}"
    if event_type == "heal":
        return f"{name} HP {event['before']}→{event['after']} / 回復 {event['amount']}{suffix}"
    if event_type == "mp_cost":
        return f"{name} MP {event['before']}→{event['after']} / 消費 {event['amount']}{suffix}"
    if event_type == "mp_recover":
        return f"{name} MP {event['before']}→{event['after']} / 回復 {event['amount']}{suffix}"
    if event_type == "sanity_loss":
        return f"{name} SAN {event['before']}→{event['after']} / 喪失 {event['amount']}{suffix}"
    if event_type == "sanity_recovery":
        return f"{name} SAN {event['before']}→{event['after']} / 回復 {event['amount']}{suffix}"
    return f"{name} の状態を更新しました{suffix}"


async def coc_apply_resource(
    room_id: str,
    participant_id: str,
    resource: str,
    operation: str,
    amount: int,
    reason: str = "",
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    resource_key = str(resource or "").strip().lower()
    operation_key = str(operation or "").strip().lower()
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        state = participant.pc_state or {}
        if resource_key in {"hp", "耐久力"}:
            state, event = heal_coc_hp(state, amount, reason) if operation_key in {"heal", "recover", "回復"} else apply_coc_damage(state, amount, reason)
        elif resource_key in {"mp", "magic", "マジックポイント"}:
            state, event = recover_coc_mp(state, amount, reason) if operation_key in {"heal", "recover", "回復"} else spend_coc_mp(state, amount, reason)
        elif resource_key in {"san", "sanity", "正気度"}:
            state, event = apply_coc_sanity_recovery(state, amount, reason) if operation_key in {"heal", "recover", "回復"} else apply_coc_sanity_loss(state, amount, reason)
        else:
            raise TRPGPlayError(f"未対応のCoCリソースです: {resource}", status_code=400)
        _finalize_coc_participant_state(participant, ruleset, state)
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_apply_resource",
            f"{resource_key} {operation_key} {reason}",
        )
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "system",
            _short_coc_event_content(participant.display_name, event, reason),
            {"event": "coc_resource", "resource": resource_key, "operation": operation_key, "result": event, "rule_reference": rule_reference},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(participant)
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": event}


async def coc_skill_check(
    room_id: str,
    participant_id: str,
    skill: str,
    difficulty: str = "regular",
    note: str = "",
    mark_experience: bool = True,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        state = participant.pc_state or {}
        target = resolve_roll_target_from_state(ruleset, state, skill)
        if target is None:
            raise TRPGPlayError(f"技能/能力値が見つかりません: {skill}", status_code=400)
        roll = roll_dice_expression("1d100")
        evaluated = evaluate_roll_for_ruleset(ruleset, roll, target, difficulty)
        result = evaluated["details"]
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_skill_check",
            f"{skill} {difficulty} {note}",
        )
        if mark_experience and evaluated["success"]:
            state = mark_coc_skill_experience(state, skill)
            _finalize_coc_participant_state(participant, ruleset, state)
        content = (
            f"🎲 {participant.display_name} が {skill} / 1d100 → "
            f"({roll['rolls'][0]}) = {roll['total']} [目標 {target}] → "
            f"{result.get('success_label') or ('成功' if evaluated['success'] else '失敗')}"
        )
        if note:
            content += f" 『{note}』"
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "dice",
            content,
            {
                **roll,
                "target": target,
                "skill": skill,
                "difficulty": difficulty,
                "success": evaluated["success"],
                "ruleset": ruleset,
                "ruleset_result": result,
                "experience_marked": bool(mark_experience and evaluated["success"]),
                "note": note,
                "rule_reference": rule_reference,
            },
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(participant)
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": result}


async def coc_resistance_check(
    room_id: str,
    participant_id: str,
    active_value: int,
    passive_value: int,
    note: str = "",
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        roll = roll_dice_expression("1d100")
        result = evaluate_coc6_resistance(roll["total"], active_value, passive_value)
        target = coc6_resistance_target(active_value, passive_value)
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_resistance_check",
            f"{active_value} {passive_value} {note}",
        )
        content = (
            f"🎲 {participant.display_name} が抵抗表 / 1d100 → ({roll['rolls'][0]}) = {roll['total']} "
            f"[能動 {active_value} / 受動 {passive_value} / 目標 {target}] → {result['success_label']}"
        )
        if note:
            content += f" 『{note}』"
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "dice",
            content,
            {**roll, "target": target, "success": result["success"], "ruleset": ruleset, "ruleset_result": result, "note": note, "rule_reference": rule_reference},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": result}


async def coc_development_check(
    room_id: str,
    participant_id: str,
    skill: str,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        roll = roll_dice_expression("1d100")
        state, result = apply_coc_skill_development(participant.pc_state or {}, skill, roll["total"])
        _finalize_coc_participant_state(participant, ruleset, state)
        content = (
            f"{participant.display_name} の成長チェック: {skill} / 1d100={roll['total']} "
            f"{'上昇' if result['improved'] else '変化なし'}"
        )
        if result["improved"]:
            content += f" ({result['before']}→{result['after']})"
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "system",
            content,
            {"event": "coc_development", "skill": skill, "roll": roll, "result": result},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(participant)
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": result}


async def coc_post_session_summary(
    room_id: str,
    participant_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    selected_ids = {_parse_uuid_strict(pid) for pid in (participant_ids or [])}
    async with await get_db_session() as session:
        play_session = await _load_room_with_children(session, room_uid)
        participants = []
        for participant in sorted(
            play_session.participants or [],
            key=lambda p: p.seat_index or 0,
        ):
            if selected_ids and participant.id not in selected_ids:
                continue
            if not is_coc_sheet(participant.pc_state or {}):
                continue
            state = rebuild_coc_state_runtime(participant.pc_state or {})
            participants.append(
                {
                    "participant_id": str(participant.id),
                    "display_name": participant.display_name,
                    "role": participant.role,
                    "sanity": state.get("sanity"),
                    "max_sanity": state.get("max_sanity"),
                    "checked_skills": checked_coc_development_skills(state),
                }
            )
        return {
            "room_id": str(play_session.id),
            "status": play_session.status,
            "participants": participants,
        }


def _post_session_result_content(
    participant: ScenarioParticipant,
    developments: List[Dict[str, Any]],
    sanity_event: Optional[Dict[str, Any]],
) -> str:
    parts = [f"{participant.display_name} のセッション後処理"]
    if developments:
        improved = [item for item in developments if item.get("improved")]
        parts.append(f"成長チェック {len(developments)}件")
        if improved:
            labels = [
                f"{item['skill']} {item['before']}→{item['after']}"
                for item in improved[:5]
            ]
            parts.append("成長: " + "、".join(labels))
    else:
        parts.append("成長チェック対象なし")
    if sanity_event:
        parts.append(
            f"SAN {sanity_event['before']}→{sanity_event['after']} (+{sanity_event['amount']})"
        )
    return " / ".join(parts)


async def coc_apply_post_session(
    room_id: str,
    participant_ids: Optional[List[str]] = None,
    sanity_recovery_expression: str = "",
    outcome: str = "",
    close_room: bool = False,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    selected_ids = {_parse_uuid_strict(pid) for pid in (participant_ids or [])}
    recovery_expr = str(sanity_recovery_expression or "").strip()
    outcome_text = str(outcome or "").strip()

    async with await get_db_session() as session:
        play_session = await _load_room_with_children(session, room_uid)
        scenario = await session.get(Scenario, play_session.scenario_id)
        ruleset = _ruleset_for_scenario(scenario)
        logs: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []

        target_participants = [
            participant
            for participant in sorted(play_session.participants or [], key=lambda p: p.seat_index or 0)
            if participant.is_active_participant
            and is_coc_sheet(participant.pc_state or {})
            and (not selected_ids or participant.id in selected_ids)
        ]
        if selected_ids and len(target_participants) != len(selected_ids):
            raise TRPGPlayError("後処理対象のCoC参加者が見つかりません", status_code=404)

        for participant in target_participants:
            state = participant.pc_state or {}
            state, developments = apply_coc_checked_skill_development(state)
            sanity_roll = None
            sanity_event = None
            if recovery_expr:
                try:
                    sanity_roll = roll_coc_dice_expression(recovery_expr)
                except ValueError as exc:
                    raise TRPGPlayError(f"SAN回復ダイス式が不正です: {recovery_expr}") from exc
                state, sanity_event = apply_coc_sanity_recovery(
                    state,
                    int(sanity_roll["total"]),
                    source="セッション後回復",
                )
            _finalize_coc_participant_state(participant, ruleset, state)
            await _save_coc_player_sheet_from_participant(session, play_session, participant, ruleset)

            log = await _append_log_internal(
                session,
                room_uid,
                participant.id,
                "system",
                _post_session_result_content(participant, developments, sanity_event),
                {
                    "event": "coc_post_session",
                    "developments": developments,
                    "sanity_roll": sanity_roll,
                    "sanity_recovery": sanity_event,
                    "outcome": outcome_text,
                },
            )
            logs.append(log.to_dict())
            results.append(
                {
                    "participant_id": str(participant.id),
                    "display_name": participant.display_name,
                    "developments": developments,
                    "sanity_roll": sanity_roll,
                    "sanity_recovery": sanity_event,
                }
            )

        if close_room:
            shared_state = dict(play_session.shared_state or {})
            shared_state["post_session"] = {
                **(shared_state.get("post_session") if isinstance(shared_state.get("post_session"), dict) else {}),
                "outcome": outcome_text or "completed",
                "completed_at": datetime.utcnow().isoformat(),
                "sanity_recovery_expression": recovery_expr,
            }
            play_session.shared_state = shared_state
            play_session.status = "completed"
            closing_log = await _append_log_internal(
                session,
                room_uid,
                None,
                "system",
                f"CoCセッション後処理を完了し、セッションを終了しました。{f' 結果: {outcome_text}' if outcome_text else ''}",
                {
                    "event": "session_completed",
                    "outcome": outcome_text,
                    "post_session": "coc",
                },
            )
            logs.append(closing_log.to_dict())

        play_session.updated_at = datetime.utcnow()
        await session.commit()
        for participant in target_participants:
            await session.refresh(participant)
        room = await _hydrate_room_dict(session, await _load_room_with_children(session, room_uid))
        return {
            "room": room,
            "participants": [participant.to_dict() for participant in target_participants],
            "logs": logs,
            "results": results,
        }


async def coc_attack_action(
    room_id: str,
    attacker_id: str,
    defender_id: Optional[str],
    weapon: str,
    defense_type: str = "回避",
    note: str = "",
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    attacker_uid = _parse_uuid_strict(attacker_id)
    defender_uid = _parse_uuid(defender_id) if defender_id else None
    async with await get_db_session() as session:
        play_session, attacker, ruleset = await _load_coc_participant_for_update(
            session, room_uid, attacker_uid
        )
        defender = None
        if defender_uid:
            defender = await session.get(ScenarioParticipant, defender_uid)
            if defender is None or defender.play_session_id != room_uid or not is_coc_sheet(defender.pc_state or {}):
                raise ParticipantNotFoundError(str(defender_id))
        attack_roll = roll_dice_expression("1d100")
        defense_roll = roll_dice_expression("1d100") if defender else None
        updated_defender_state, result = resolve_coc_attack(
            attacker.pc_state or {},
            weapon,
            attack_roll["total"],
            defender.pc_state if defender else None,
            defense_roll["total"] if defense_roll else None,
            defense_type,
        )
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_attack_action",
            f"{weapon} {defense_type} {note}",
        )
        weapon_profile = find_coc_weapon(attacker.pc_state or {}, weapon)
        if result["attack"].get("success"):
            _finalize_coc_participant_state(
                attacker,
                ruleset,
                mark_coc_skill_experience(attacker.pc_state or {}, str(weapon_profile.get("skill") or weapon)),
            )
        if defender and updated_defender_state is not None:
            _finalize_coc_participant_state(defender, ruleset, updated_defender_state)
        content = (
            f"⚔ {attacker.display_name} が {weapon_profile.get('name') or weapon} で攻撃: "
            f"1d100={attack_roll['total']} → {result['attack'].get('success_label')}"
        )
        if defender:
            content += f" / {defender.display_name} {defense_type}: 1d100={defense_roll['total']} → {result['defense'].get('success_label') if result.get('defense') else 'なし'}"
        if result.get("damage"):
            damage = result["damage"]["result"]
            content += f" / ダメージ {damage['amount']} HP {damage['before']}→{damage['after']}"
        elif result["hit"]:
            content += " / 命中"
        else:
            content += " / 非命中"
        if note:
            content += f" 『{note}』"
        log = await _append_log_internal(
            session,
            room_uid,
            attacker.id,
            "dice",
            content,
            {
                "event": "coc_attack",
                "attack_roll": attack_roll,
                "defense_roll": defense_roll,
                "weapon": weapon_profile,
                "result": result,
                "note": note,
                "rule_reference": rule_reference,
            },
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(attacker)
        if defender:
            await session.refresh(defender)
        await session.refresh(log)
        return {
            "participant": attacker.to_dict(),
            "defender": defender.to_dict() if defender else None,
            "log": log.to_dict(),
            "result": result,
        }


async def coc_spell_cost_action(
    room_id: str,
    participant_id: str,
    spell_name: str,
    mp_cost: int = 0,
    san_cost: int = 0,
    hp_cost: int = 0,
    pow_cost: int = 0,
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        state, result = apply_coc_spell_cost(participant.pc_state or {}, spell_name, mp_cost, san_cost, hp_cost, pow_cost)
        _finalize_coc_participant_state(participant, ruleset, state)
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_spell_cost_action",
            spell_name,
        )
        parts = []
        for event in result["events"]:
            parts.append(f"{event['type']} {event.get('before')}→{event.get('after')}")
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "system",
            f"{participant.display_name} が呪文コストを適用: {spell_name} / " + (", ".join(parts) or "コストなし"),
            {"event": "coc_spell_cost", "result": result, "rule_reference": rule_reference},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(participant)
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": result}


async def coc_insanity_action(
    room_id: str,
    participant_id: str,
    kind: str = "temporary",
    reason: str = "",
) -> Dict[str, Any]:
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid_strict(participant_id)
    async with await get_db_session() as session:
        play_session, participant, ruleset = await _load_coc_participant_for_update(
            session, room_uid, participant_uid
        )
        effect = roll_coc_insanity_effect(kind)
        state = participant.pc_state or {}
        insanity = state.get("insanity") if isinstance(state.get("insanity"), dict) else {}
        active = insanity.get("active") if isinstance(insanity.get("active"), list) else []
        active.append({"kind": effect["kind"], "effect": effect["effect"], "reason": reason})
        insanity["active"] = active
        state["insanity"] = insanity
        state = rebuild_coc_state_runtime(state)
        _finalize_coc_participant_state(participant, ruleset, state)
        rule_reference = await _mechanic_rule_reference(
            ruleset,
            "coc_insanity_action",
            f"{kind} {reason}",
        )
        label = "不定の狂気" if effect["kind"] == "indefinite" else "一時的狂気"
        log = await _append_log_internal(
            session,
            room_uid,
            participant.id,
            "system",
            f"{participant.display_name} に{label}: {effect['effect']}" + (f"（{reason}）" if reason else ""),
            {"event": "coc_insanity", "result": effect, "reason": reason, "rule_reference": rule_reference},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(participant)
        await session.refresh(log)
        return {"participant": participant.to_dict(), "log": log.to_dict(), "result": effect}


# ────────────────────────────────────────────
# ターン / シーン / 共有状態
# ────────────────────────────────────────────


async def advance_turn(room_id: str) -> Dict[str, Any]:
    """ターンを次のプレイヤーに進める。turn_order は参加順で自動構築する。"""
    room_uid = _parse_uuid_strict(room_id)
    async with await get_db_session() as session:
        play_session = await _load_room_with_children(session, room_uid)
        players = [
            p
            for p in (play_session.participants or [])
            if p.is_active_participant and p.role == "player"
        ]
        players.sort(key=lambda p: p.seat_index or 0)
        if not players:
            raise TRPGPlayError(
                "プレイヤーが居ないためターンを進められません",
                status_code=400,
            )

        order = [str(p.id) for p in players]
        play_session.turn_order = order
        current = str(play_session.current_turn_participant_id or "")
        if current in order:
            idx = order.index(current)
            next_idx = (idx + 1) % len(order)
        else:
            next_idx = 0
        next_id = order[next_idx]
        play_session.current_turn_participant_id = uuid.UUID(next_id)

        next_participant = next(p for p in players if str(p.id) == next_id)
        await _append_log_internal(
            session,
            room_uid,
            None,
            "system",
            f"▶ {next_participant.display_name} のターンです。",
            {"event": "turn_change", "next_participant_id": next_id},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        return {
            "turn_order": order,
            "current_turn_participant_id": next_id,
            "display_name": next_participant.display_name,
        }


async def change_scene(
    room_id: str,
    next_scene_id: str,
    announcement: str = "",
) -> Dict[str, Any]:
    """シーンを切り替え、ログに記録する。"""
    room_uid = _parse_uuid_strict(room_id)
    scene_uid = _parse_uuid_strict(next_scene_id)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        next_scene = await session.get(ScenarioScene, scene_uid)
        if next_scene is None:
            raise TRPGPlayError(
                f"シーンが見つかりません: {next_scene_id}",
                status_code=404,
            )

        from_id = (
            str(play_session.current_scene_id)
            if play_session.current_scene_id
            else None
        )
        play_session.current_scene_id = scene_uid
        play_session.updated_at = datetime.utcnow()

        await _append_log_internal(
            session,
            room_uid,
            None,
            "scene_change",
            announcement or f"【シーン切替】{next_scene.title}",
            {
                "from_scene_id": from_id,
                "to_scene_id": str(scene_uid),
                "title": next_scene.title,
            },
        )
        await session.commit()
        return {
            "from_scene_id": from_id,
            "to_scene_id": str(scene_uid),
            "title": next_scene.title,
        }


async def update_shared_state(
    room_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """shared_state（天候・時刻・BGM・ラウンド数）をマージ更新する。"""
    room_uid = _parse_uuid_strict(room_id)
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        current = play_session.shared_state or {}
        current.update(updates)
        play_session.shared_state = current
        play_session.updated_at = datetime.utcnow()
        await _append_log_internal(
            session,
            room_uid,
            None,
            "state_change",
            "",
            {"shared_state": updates},
        )
        await session.commit()
        return current


def _normalize_ui_module_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    modules: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            modules.append(item)
    return modules


def _find_ui_module(modules: List[Dict[str, Any]], module_id: str) -> Optional[Dict[str, Any]]:
    for module in modules:
        if str(module.get("id")) == module_id:
            return module
    return None


def _set_dotted_value(root: Dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    cursor: Dict[str, Any] = root
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def _get_dotted_number(root: Dict[str, Any], path: str, fallback: float = 0) -> float:
    cursor: Any = root
    for part in [part for part in path.split(".") if part]:
        if not isinstance(cursor, dict):
            return fallback
        cursor = cursor.get(part)
    return cursor if isinstance(cursor, (int, float)) else fallback


def _ui_module_title(module: Dict[str, Any]) -> str:
    return str(module.get("title") or module.get("id") or "UI module")


def _apply_ui_module_effects(
    shared_state: Dict[str, Any],
    module_state: Dict[str, Any],
    effects: Any,
) -> List[str]:
    log_messages: List[str] = []
    if not isinstance(effects, list):
        return log_messages
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        set_state = effect.get("setState")
        if isinstance(set_state, dict):
            if isinstance(set_state.get("path"), str):
                _set_dotted_value(shared_state, set_state["path"], set_state.get("value"))
            else:
                for path, value in set_state.items():
                    if isinstance(path, str):
                        _set_dotted_value(shared_state, path, value)
        set_module_state = effect.get("setModuleState")
        if isinstance(set_module_state, dict):
            module_state.update(set_module_state)
        increment = effect.get("increment")
        if isinstance(increment, dict) and isinstance(increment.get("path"), str):
            path = increment["path"]
            amount = increment.get("amount", 1)
            if isinstance(amount, (int, float)):
                _set_dotted_value(
                    shared_state,
                    path,
                    _get_dotted_number(shared_state, path) + amount,
                )
        append_log = effect.get("appendLog")
        if isinstance(append_log, str) and append_log.strip():
            log_messages.append(append_log.strip())
        elif isinstance(append_log, dict) and isinstance(append_log.get("content"), str):
            log_messages.append(append_log["content"].strip())
    return log_messages


async def apply_ui_module_action(
    room_id: str,
    module_id: str,
    action_type: str,
    payload: Optional[Dict[str, Any]] = None,
    participant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a generic TRPG UI module action and persist shared module state."""
    room_uid = _parse_uuid_strict(room_id)
    participant_uid = _parse_uuid(participant_id) if participant_id else None
    action_payload = payload or {}

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)

        shared_state = dict(play_session.shared_state or {})
        modules = _normalize_ui_module_list(shared_state.get("ui_modules"))
        if play_session.current_scene_id:
            scene = await session.get(ScenarioScene, play_session.current_scene_id)
            if scene and isinstance(scene.state_snapshot, dict):
                modules.extend(_normalize_ui_module_list(scene.state_snapshot.get("ui_modules")))

        module = _find_ui_module(modules, module_id)
        if module is None:
            raise TRPGPlayError(f"UI module not found: {module_id}", status_code=404)

        config = module.get("config") if isinstance(module.get("config"), dict) else {}
        module_states = shared_state.get("ui_module_state")
        if not isinstance(module_states, dict):
            module_states = {}
        state = dict(module_states.get(module_id) or {})
        kind = str(module.get("module") or module.get("type") or "button_grid")
        log_messages: List[str] = []

        if action_type == "button_press":
            button_id = str(action_payload.get("button_id") or action_payload.get("id") or "")
            if not button_id:
                raise TRPGPlayError("button_id is required")
            if config.get("mode") in {"single", "radio", "select"}:
                state["selected"] = button_id
            else:
                buttons = state.get("buttons") if isinstance(state.get("buttons"), dict) else {}
                buttons[button_id] = not bool(buttons.get(button_id))
                state["buttons"] = buttons
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        elif action_type == "choice_select":
            choice_id = str(action_payload.get("choice_id") or action_payload.get("id") or "")
            if not choice_id:
                raise TRPGPlayError("choice_id is required")
            state["selected"] = choice_id
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        elif action_type == "keypad_submit":
            value = str(action_payload.get("value") or "")
            answer = (
                config.get("successCode")
                or config.get("answer")
                or (
                    config.get("solution", {}).get("gm_only")
                    if isinstance(config.get("solution"), dict)
                    else None
                )
            )
            success = bool(answer is not None and value == str(answer))
            state["attempts"] = int(state.get("attempts") or 0) + 1
            state["last_success"] = success
            if success:
                state["unlocked"] = True
            log_messages.extend(
                _apply_ui_module_effects(
                    shared_state,
                    state,
                    module.get("onSuccess") if success else module.get("onFailure"),
                )
            )

        elif action_type == "counter_update":
            current = state.get("value")
            value = current if isinstance(current, (int, float)) else config.get("initial", 0)
            if not isinstance(value, (int, float)):
                value = 0
            if "value" in action_payload and isinstance(action_payload["value"], (int, float)):
                value = action_payload["value"]
            else:
                delta = action_payload.get("delta", 0)
                if isinstance(delta, (int, float)):
                    value = value + delta
            if isinstance(config.get("min"), (int, float)):
                value = max(config["min"], value)
            if isinstance(config.get("max"), (int, float)):
                value = min(config["max"], value)
            state["value"] = int(value) if float(value).is_integer() else value
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        elif action_type == "checklist_toggle":
            item_id = str(action_payload.get("item_id") or action_payload.get("id") or "")
            if not item_id:
                raise TRPGPlayError("item_id is required")
            checked = state.get("checked") if isinstance(state.get("checked"), dict) else {}
            checked[item_id] = not bool(checked.get(item_id))
            state["checked"] = checked
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        elif action_type in {"hotspot_select", "map_pin_select"}:
            key = "hotspot_id" if action_type == "hotspot_select" else "pin_id"
            selected_id = str(action_payload.get(key) or action_payload.get("id") or "")
            if not selected_id:
                raise TRPGPlayError(f"{key} is required")
            state["selected"] = selected_id
            if action_type == "hotspot_select":
                discovered = state.get("discovered") if isinstance(state.get("discovered"), dict) else {}
                discovered[selected_id] = True
                state["discovered"] = discovered
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        else:
            state["last_action"] = {"type": action_type, "payload": action_payload}
            log_messages.extend(_apply_ui_module_effects(shared_state, state, module.get("onAction")))

        state["last_action_type"] = action_type
        state["last_updated_at"] = datetime.utcnow().isoformat()
        module_states[module_id] = state
        shared_state["ui_module_state"] = module_states
        play_session.shared_state = shared_state
        play_session.updated_at = datetime.utcnow()

        content = "\n".join([message for message in log_messages if message])
        log = await _append_log_internal(
            session,
            room_uid,
            participant_uid,
            "state_change",
            content,
            {
                "ui_module_id": module_id,
                "ui_module_title": _ui_module_title(module),
                "ui_module_kind": kind,
                "action_type": action_type,
                "payload": action_payload,
            },
        )
        await session.commit()
        await session.refresh(log)
        return {
            "shared_state": shared_state,
            "module_state": state,
            "log": log.to_dict(),
        }
