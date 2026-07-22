"""ダイスロールと CoC 判定・リソース処理系。"""

from __future__ import annotations

import random
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...memory.database import get_db_session
from ...models.ecc_models import (
    Scenario,
    ScenarioParticipant,
    ScenarioPlaySession,
    TRPGRulesetProfile,
)
from ..trpg_rules import (
    COC6_RULESET_TAG,
    COC7_RULESET_TAG,
    evaluate_roll_for_ruleset,
    resolve_roll_target_from_state,
)
from ..trpg_coc import (
    apply_coc_san_loss,
    is_coc_sheet,
    is_coc_san_label,
    normalize_coc_state,
    parse_coc_san_loss,
)
from ..trpg_coc_system import (
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
from ..trpg_rulebook_service import profile_model_to_runtime_dict
from ..trpg_rule_reference_service import format_rule_reference_context, get_mechanic_rule_context
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    ParticipantNotFoundError,
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _hydrate_room_dict,
    _load_room_with_children,
    _ruleset_for_scenario,
    logger,
)
from .participants import _upsert_player_character_sheet


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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    pid = parse_uuid(participant_id) if participant_id else None

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


# ────────────────────────────────────────────
# CoC 判定・リソース処理
# ────────────────────────────────────────────


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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    selected_ids = {parse_uuid_strict(pid, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}")) for pid in (participant_ids or [])}
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    selected_ids = {parse_uuid_strict(pid, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}")) for pid in (participant_ids or [])}
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    attacker_uid = parse_uuid_strict(attacker_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    defender_uid = parse_uuid(defender_id) if defender_id else None
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
