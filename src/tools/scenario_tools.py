"""TRPGシナリオ進行・自動化ツール

会話内容からHP減少、フラグ更新、シーン移動などを自動で判断し、
ScenarioPlaySession を更新するためのツールを提供する。
"""

import logging
import random
import re
from typing import Dict, Any, Optional, List

from .core import tool
from ..services.scenario_service import (
    get_play_session_by_conversation_id,
    update_play_state,
)
from ..services.trpg_rule_reference_service import build_scenario_creation_rule_context

logger = logging.getLogger(__name__)


@tool
async def get_coc_creation_reference_context(ruleset_key: str = "coc6", premise: str = "") -> str:
    """CoC scenario creation reference from structured rule/supplement DB only.

    Args:
        ruleset_key: CoC ruleset key, usually "coc6" or "coc7".
        premise: Short scenario premise, creature name, scene idea, or topic.

    Returns:
        Relevant structured rule and creature excerpts, or a short empty notice.
    """
    context = await build_scenario_creation_rule_context(
        ruleset_key=ruleset_key or "coc6",
        premise=premise or "",
        limit=8,
    )
    return context or "No structured CoC rule or creature references are registered yet."

@tool
async def roll_dice(expression: str) -> str:
    """ダイスを振って結果を返す。

    Args:
        expression: ダイス表記（例: "1d100", "2d6+3", "3d10"）

    Returns:
        ダイスロール結果の文字列
    """
    try:
        # 1d100, 2d6+3 などの形式をパース
        match = re.match(r"^(\d+)d(\d+)([+-]\d+)?$", expression.lower().replace(" ", ""))
        if not match:
            return f"エラー: 無効なダイス表記です: {expression}"

        num = int(match.group(1))
        sides = int(match.group(2))
        modifier = int(match.group(3)) if match.group(3) else 0

        if num > 100 or sides > 1000:
            return "エラー: ダイスの数が多すぎるか、面数が大きすぎます。"

        rolls = [random.randint(1, sides) for _ in range(num)]
        total = sum(rolls) + modifier

        result_str = f"Rolling {expression}: ({' + '.join(map(str, rolls))})"
        if modifier != 0:
            result_str += f" {'+' if modifier > 0 else ''}{modifier}"
        result_str += f" = {total}"

        return result_str
    except Exception as e:
        logger.error("ダイスロールエラー: %s", e)
        return f"ダイスロール中にエラーが発生しました: {e}"

@tool
async def get_scenario_state(conversation_id: str) -> str:
    """現在のシナリオ進行状態（シーン、プレイヤーのHP・フラグ等）を取得する。

    Args:
        conversation_id: 現在の会話セッションID

    Returns:
        進行状態のJSON文字列。シナリオに関連付いていない場合はその旨を返す。
    """
    try:
        session = await get_play_session_by_conversation_id(conversation_id)
        if not session:
            return "この会話は現在シナリオに関連付けられていません。"

        import json
        return json.dumps(session, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("シナリオ状態取得エラー: %s", e)
        return f"シナリオ状態の取得中にエラーが発生しました: {e}"

@tool
async def update_scenario_state(
    conversation_id: str,
    hp: Optional[int] = None,
    add_flags: Optional[List[str]] = None,
    remove_flags: Optional[List[str]] = None,
    add_items: Optional[List[str]] = None,
    remove_items: Optional[List[str]] = None,
    current_scene_id: Optional[str] = None,
    status: Optional[str] = None,
    other_state_json: Optional[str] = None,
) -> str:
    """シナリオの進行状態（HP、フラグ、アイテム、シーン等）を更新する。

    HPの減少、フラグの追加、アイテムの入手、次のシーンへの移動などに使用する。

    Args:
        conversation_id: 現在の会話セッションID
        hp: 新しいHPの値（現在の値を上書きする場合）
        add_flags: 追加するフラグのリスト
        remove_flags: 削除するフラグのリスト
        add_items: 入手したアイテムのリスト
        remove_items: 失ったアイテムのリスト
        current_scene_id: 移動先のシーンID（UUID）
        status: セッションのステータス ("in_progress", "completed" 等)
        other_state_json: その他の player_state 内の更新データ（JSON文字列）

    Returns:
        更新結果のメッセージ
    """
    try:
        play_session = await get_play_session_by_conversation_id(conversation_id)
        if not play_session:
            return "エラー: この会話に関連付けられたシナリオプレイセッションが見つかりません。"

        play_session_id = play_session["id"]
        updates = {}

        if current_scene_id:
            updates["current_scene_id"] = current_scene_id
        if status:
            updates["status"] = status

        player_state_updates = {}
        if other_state_json:
            import json
            try:
                player_state_updates = json.loads(other_state_json)
            except:
                logger.error("Failed to parse other_state_json: %s", other_state_json)

        if hp is not None:
            player_state_updates["hp"] = hp

        # フラグ管理
        current_flags = play_session.get("player_state", {}).get("flags", [])
        if not isinstance(current_flags, list):
            current_flags = []
        
        new_flags = set(current_flags)
        if add_flags:
            for f in add_flags:
                new_flags.add(f)
        if remove_flags:
            for f in remove_flags:
                if f in new_flags:
                    new_flags.remove(f)
        
        if add_flags or remove_flags:
            player_state_updates["flags"] = list(new_flags)

        # アイテム管理 (inventory)
        current_inventory = play_session.get("player_state", {}).get("inventory", [])
        if not isinstance(current_inventory, list):
            current_inventory = []
        
        new_inventory = set(current_inventory)
        if add_items:
            for item in add_items:
                new_inventory.add(item)
        if remove_items:
            for item in remove_items:
                if item in new_inventory:
                    new_inventory.remove(item)
        
        if add_items or remove_items:
            player_state_updates["inventory"] = list(new_inventory)

        if player_state_updates:
            updates["player_state"] = player_state_updates

        await update_play_state(play_session_id, updates)
        return "シナリオ状態を正常に更新しました。"

    except Exception as e:
        logger.error("シナリオ状態更新エラー: %s", e)
        return f"シナリオ状態の更新中にエラーが発生しました: {e}"
