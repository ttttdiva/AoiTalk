"""参加者管理・プレイヤーキャラクターシート・即席NPC生成。"""

from __future__ import annotations

import json
import random
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...memory.database import get_db_session
from ...models.ecc_models import (
    Scenario,
    ScenarioCharacter,
    TRPGPlayerCharacterSheet,
    ScenarioPlaySession,
    ScenarioParticipant,
)
from ..trpg_rules import COC6_RULESET_TAG, COC7_RULESET_TAG, create_pc_state_for_ruleset
from ..trpg_coc import (
    extract_coc_pc_state_from_relationships,
    is_coc_sheet,
    normalize_coc_state,
)
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    ParticipantNotFoundError,
    RoomFullError,
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _next_seat_and_color,
    _pc_state_for_ruleset,
    _resolve_play_session,
    _ruleset_for_scenario,
    logger,
)


# ────────────────────────────────────────────
# 即席NPC生成
# ────────────────────────────────────────────


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
        from ...llm.native_runtime import AgentDefinition, run_native_agent_once

        agent = AgentDefinition(
            name="trpg_quick_npc_profile_generator",
            instructions=(
                "あなたはTRPG卓の即席NPCを作る補助AIです。"
                "返答は必ずJSONオブジェクト1つだけにします。"
            ),
            model="gpt-4o-mini",
        )
        result = await run_native_agent_once(agent, prompt)
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


# ────────────────────────────────────────────
# プレイヤーキャラクターシート
# ────────────────────────────────────────────


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
        uid = parse_uuid(room_id_or_code)
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
                TRPGPlayerCharacterSheet.user_id == parse_uuid_strict(user_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}")),
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
        uid = parse_uuid(room_id_or_code)
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
        user_uid = parse_uuid(user_id) if user_id else None
        char_uid = parse_uuid(character_id) if character_id else None
        scenario_char_uid = parse_uuid(scenario_character_id) if scenario_character_id else None
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    pid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))

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
    pid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))

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
