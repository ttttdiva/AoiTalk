"""TRPG マルチプレイヤープレイサービス パッケージ

ココフォリア風のルーム／参加者／ログ管理と、AI GM 連携の入口を提供する。
実装は関心ごとにモジュール分割し、公開 API は従来どおり
`src.services.trpg_play_service` から import できるよう再公開する。
既存の scenario_service.py はシングルプレイ用のAPIを維持する。
"""

from __future__ import annotations

from ._shared import (
    GM_TARGET_ID,
    DISCLOSURE_TYPES,
    DISCLOSURE_VISIBILITIES,
    ParticipantNotFoundError,
    RoomFullError,
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _generate_room_code,
    _hydrate_room_dict,
    _load_room_with_children,
    _next_seat_and_color,
    _normalize_room_status_filter,
    _normalize_target_ids,
    _participant_id_str,
    _participant_is_gm,
    _pc_state_for_ruleset,
    _resolve_play_session,
    _ruleset_for_scenario,
    logger,
)
from .access import (
    _resolve_viewer_context,
    _viewer_can_see_disclosure,
    _viewer_can_see_private_message,
    require_participant_write_access,
    require_room_gm_access,
    require_room_participation_access,
    require_room_view_access,
)
from .rooms import (
    _can_delete_room,
    complete_room,
    create_room,
    delete_room,
    get_room,
    list_rooms,
)
from .participants import (
    join_room,
    leave_room,
    list_player_character_sheets,
    suggest_quick_npc_name,
    update_participant,
)
from .logs import (
    append_log,
    append_private_message_internal,
    create_disclosure,
    list_disclosures,
    list_logs,
    list_private_messages,
    send_private_message,
)
from .dice import (
    coc_apply_post_session,
    coc_apply_resource,
    coc_attack_action,
    coc_development_check,
    coc_insanity_action,
    coc_post_session_summary,
    coc_resistance_check,
    coc_skill_check,
    coc_spell_cost_action,
    roll_dice_expression,
    roll_dice_in_room,
)
from .progression import (
    advance_turn,
    apply_ui_module_action,
    change_scene,
    update_shared_state,
)

__all__ = [
    # 例外
    "TRPGPlayError",
    "RoomNotFoundError",
    "ParticipantNotFoundError",
    "RoomFullError",
    # 定数
    "GM_TARGET_ID",
    "DISCLOSURE_TYPES",
    "DISCLOSURE_VISIBILITIES",
    # ルーム
    "create_room",
    "list_rooms",
    "get_room",
    "delete_room",
    "complete_room",
    # 参加者・シート
    "join_room",
    "leave_room",
    "update_participant",
    "list_player_character_sheets",
    "suggest_quick_npc_name",
    # ログ・開示・個別チャット
    "append_log",
    "list_logs",
    "list_disclosures",
    "create_disclosure",
    "list_private_messages",
    "send_private_message",
    "append_private_message_internal",
    # ダイス・CoC
    "roll_dice_expression",
    "roll_dice_in_room",
    "coc_apply_resource",
    "coc_skill_check",
    "coc_resistance_check",
    "coc_development_check",
    "coc_post_session_summary",
    "coc_apply_post_session",
    "coc_attack_action",
    "coc_spell_cost_action",
    "coc_insanity_action",
    # 進行
    "advance_turn",
    "change_scene",
    "update_shared_state",
    "apply_ui_module_action",
    # アクセス制御
    "require_room_view_access",
    "require_room_participation_access",
    "require_room_gm_access",
    "require_participant_write_access",
    # 他サービス/テストが参照する内部ヘルパー
    "_append_log_internal",
    "_load_room_with_children",
    "_hydrate_room_dict",
    "_resolve_play_session",
    "_can_delete_room",
    "_normalize_room_status_filter",
    "_normalize_target_ids",
    "_viewer_can_see_disclosure",
    "_viewer_can_see_private_message",
    "_resolve_viewer_context",
    "_participant_is_gm",
    "_participant_id_str",
    "_pc_state_for_ruleset",
    "_ruleset_for_scenario",
    "_generate_room_code",
    "_next_seat_and_color",
    "logger",
]
