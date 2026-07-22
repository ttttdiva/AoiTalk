"""ターン進行・シーン切替・共有状態・汎用UIモジュール操作。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...memory.database import get_db_session
from ...models.ecc_models import ScenarioPlaySession, ScenarioScene
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _load_room_with_children,
)


async def advance_turn(room_id: str) -> Dict[str, Any]:
    """ターンを次のプレイヤーに進める。turn_order は参加順で自動構築する。"""
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    scene_uid = parse_uuid_strict(next_scene_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))

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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
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
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    participant_uid = parse_uuid(participant_id) if participant_id else None
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
