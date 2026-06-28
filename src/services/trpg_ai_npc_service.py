"""TRPG AI NPC private thinking and selective reactions."""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from ..memory.database import get_db_session
from ..models.ecc_models import (
    Character,
    Scenario,
    ScenarioCharacter,
    ScenarioParticipant,
    ScenarioPlayLog,
    ScenarioPlaySession,
)
from .trpg_play_service import TRPGPlayError, _append_log_internal

logger = logging.getLogger(__name__)


_MAX_OBSERVED_LOGS = 16
_MAX_NPCS_PER_TICK = 6
_MAX_STATE_NOTES = 24
_DEFAULT_STRATEGY_DELAY_SECONDS = 30
_STRATEGY_STATE_KEY = "ai_npc_strategy"
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_marker_fields(marker: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for chunk in str(marker or "").split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            fields[key] = value
    return fields


def parse_npc_strategy_marker(marker: str) -> Dict[str, Any]:
    """Parse `[NPC_STRATEGY:phase=...,delay=30,focus=...]` marker content."""
    fields = _parse_marker_fields(marker)
    delay_raw = fields.get("delay") or fields.get("seconds") or fields.get("after")
    try:
        delay_seconds = int(float(delay_raw)) if delay_raw is not None else _DEFAULT_STRATEGY_DELAY_SECONDS
    except (TypeError, ValueError):
        delay_seconds = _DEFAULT_STRATEGY_DELAY_SECONDS
    delay_seconds = max(0, min(delay_seconds, 300))
    phase = fields.get("phase") or fields.get("name") or "作戦タイム"
    focus = fields.get("focus") or fields.get("topic") or ""
    return {
        "phase": _clip(phase, 80),
        "focus": _clip(focus, 240),
        "delay_seconds": delay_seconds,
    }


def schedule_ai_npc_strategy_shared_state(
    shared_state: Any,
    marker: str | Dict[str, Any] | None = None,
    *,
    trigger: str = "gm_marker",
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a new shared_state with an AI NPC strategy phase scheduled."""
    current = copy.deepcopy(shared_state) if isinstance(shared_state, dict) else {}
    now_dt = now or _utcnow()
    marker_data = marker if isinstance(marker, dict) else parse_npc_strategy_marker(str(marker or ""))
    try:
        delay_seconds = int(marker_data.get("delay_seconds", _DEFAULT_STRATEGY_DELAY_SECONDS))
    except (TypeError, ValueError):
        delay_seconds = _DEFAULT_STRATEGY_DELAY_SECONDS
    delay_seconds = max(0, min(delay_seconds, 300))
    due_at = now_dt + timedelta(seconds=delay_seconds)
    schedule = {
        "id": str(uuid.uuid4()),
        "status": "scheduled",
        "phase": _clip(marker_data.get("phase") or "作戦タイム", 80),
        "focus": _clip(marker_data.get("focus") or "", 240),
        "trigger": _clip(trigger, 80),
        "delay_seconds": delay_seconds,
        "scheduled_at": _iso_utc(now_dt),
        "due_at": _iso_utc(due_at),
        "processed_at": None,
        "error": "",
    }
    current[_STRATEGY_STATE_KEY] = schedule
    return current, schedule


async def schedule_ai_npc_strategy(
    room_id: str,
    *,
    phase: str = "作戦タイム",
    delay_seconds: int = _DEFAULT_STRATEGY_DELAY_SECONDS,
    focus: str = "",
    trigger: str = "manual",
) -> Dict[str, Any]:
    """Persist an AI NPC strategy phase schedule into a room shared_state."""
    room_uid = _parse_uuid(room_id)
    if room_uid is None:
        raise TRPGPlayError(f"無効なルームID: {room_id}", status_code=400)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise TRPGPlayError(f"ルームが見つかりません: {room_id}", status_code=404)
        shared, schedule = schedule_ai_npc_strategy_shared_state(
            play_session.shared_state,
            {
                "phase": phase or "作戦タイム",
                "focus": focus or "",
                "delay_seconds": delay_seconds,
            },
            trigger=trigger,
        )
        play_session.shared_state = shared
        play_session.updated_at = _utcnow()
        await session.commit()
        return {"shared_state": shared, "strategy": schedule}


def _strategy_state_is_due(strategy_state: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if strategy_state.get("status") != "scheduled":
        return False
    due_at = _parse_iso_datetime(strategy_state.get("due_at"))
    if due_at is None:
        return False
    return (now or _utcnow()) >= due_at


def _safe_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw)
        if not match:
            return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _log_line(log: ScenarioPlayLog) -> str:
    speaker = str((log.log_metadata or {}).get("speaker") or "")
    prefix = f"{speaker}: " if speaker else ""
    return f"[{log.log_type}] {prefix}{_clip(log.content, 500)}"


def _participant_name_by_id(participants: List[ScenarioParticipant]) -> Dict[str, str]:
    return {
        str(participant.id): str(participant.display_name or "").strip()
        for participant in participants
        if participant.id and str(participant.display_name or "").strip()
    }


def _speaker_label_from_log(
    log: Optional[ScenarioPlayLog],
    participants: List[ScenarioParticipant],
) -> str:
    if log is None:
        return "相手"
    if log.participant_id:
        name = _participant_name_by_id(participants).get(str(log.participant_id))
        if name:
            return name
    if log.log_type == "narration":
        return "GM"
    content = str(log.content or "")
    if ":" in content:
        return content.split(":", 1)[0].strip() or "相手"
    if "→" in content:
        return content.split("→", 1)[0].strip() or "相手"
    return "相手"


def _observed_requires_public_reply(
    observed_logs: List[ScenarioPlayLog],
    npc: ScenarioParticipant,
    *,
    trigger: str = "",
    strategy_state: Optional[Dict[str, Any]] = None,
) -> bool:
    if not observed_logs:
        return False
    if isinstance(strategy_state, dict) and strategy_state.get("status") in {"scheduled", "processing"}:
        return True
    if trigger in {"player_action", "gm_advance", "session_start"}:
        return True
    latest = observed_logs[-1]
    return latest.participant_id is not None and latest.participant_id != npc.id


def _fallback_public_npc_content(
    npc: ScenarioParticipant,
    observed_logs: List[ScenarioPlayLog],
    participants: List[ScenarioParticipant],
) -> str:
    latest = observed_logs[-1] if observed_logs else None
    speaker = _speaker_label_from_log(latest, participants)
    latest_text = _clip(getattr(latest, "content", "") if latest else "", 120)
    if speaker == "GM":
        latest_text = "GM描写"

    npc_name = str(npc.display_name or "")
    if "御影" in npc_name:
        return (
            "今は投票へ急ぐより、票数、監査、同点処理を分けて決めましょう。"
            f"{speaker}さんの話は、その合意作りに接続できると思います。"
        )
    if "乾" in npc_name:
        return (
            "協力案自体は悪くありません。ただ、縛りすぎると逆に崩れます。"
            "監査の範囲を先に絞って、動ける余地も残しませんか。"
        )
    if "白石" in npc_name:
        return (
            "数字で確認しましょう。票数、残りカード、同点処理を別々に置けば、"
            "どこに穴があるか見えます。"
        )
    if "赤羽" in npc_name:
        return "口約束だけなら信用できない。破った時にどう扱うか、先に決めてくれ。"
    if "夏目" in npc_name:
        return "置いていかれるのは怖いです。確認方法があるなら、その案を先に聞きたいです。"
    if "黒川" in npc_name:
        return "話が少し速いです。誰が何を確認するのかだけ、先に固定した方がいい。"

    if any(keyword in latest_text for keyword in ("監査", "確認", "残りカード", "公開")):
        return (
            f"{speaker}さんの監査案に賛成です。"
            "ただ、誰が、いつ、何を確認するかまで決めないと抜け道が残ります。"
        )
    if any(keyword in latest_text for keyword in ("票", "投票", "枚", "得点")):
        return (
            f"{speaker}さんの票数案を検討しましょう。"
            "同時に、残りカード確認か裏切り時の補填条件も必要です。"
        )
    return (
        f"{speaker}さんの話を受けて、投票前に確認すべき条件を一つ足したいです。"
        "監査か同点処理のどちらかを先に決めませんか。"
    )


def _looks_like_public_reply_fallback(text: str) -> bool:
    """Detect weak generic replies that repeat logs instead of roleplaying."""
    normalized = str(text or "")
    if not normalized.strip():
        return True
    weak_markers = (
        "条件を確認してから動きたいです",
        "発言を受けます",
        "の点について",
    )
    if any(marker in normalized for marker in weak_markers):
        return True
    return normalized.count("『") >= 2 or normalized.count("「") >= 3


def _logs_after_last_seen(
    logs: List[ScenarioPlayLog],
    last_seen_id: Optional[uuid.UUID],
) -> List[ScenarioPlayLog]:
    ordered = sorted(logs, key=lambda item: item.created_at or datetime.min)
    if last_seen_id is None:
        return ordered[-_MAX_OBSERVED_LOGS:]
    for index, log in enumerate(ordered):
        if log.id == last_seen_id:
            return ordered[index + 1 :][-_MAX_OBSERVED_LOGS:]
    return ordered[-_MAX_OBSERVED_LOGS:]


def _latest_log(logs: List[ScenarioPlayLog]) -> Optional[ScenarioPlayLog]:
    if not logs:
        return None
    return sorted(logs, key=lambda item: item.created_at or datetime.min)[-1]


def _observed_logs_for_npc_turn(
    conversation_logs: List[ScenarioPlayLog],
    npc: ScenarioParticipant,
    *,
    force: bool = False,
) -> List[ScenarioPlayLog]:
    observed = _logs_after_last_seen(conversation_logs, npc.last_observed_log_id)
    observed = [log for log in observed if log.participant_id != npc.id]
    if force and not observed:
        observed = sorted(conversation_logs, key=lambda item: item.created_at or datetime.min)[-_MAX_OBSERVED_LOGS:]
        observed = [log for log in observed if log.participant_id != npc.id]
    return observed


def _strategy_profile_for(
    scenario_characters: List[ScenarioCharacter],
    participant: ScenarioParticipant,
) -> Dict[str, Any]:
    matched = None
    for sc in scenario_characters:
        if participant.character_id and getattr(sc, "character_id", None) == participant.character_id:
            matched = sc
            break
        if sc.name == participant.display_name:
            matched = sc
            break
    if matched is None:
        return {}
    relationships = matched.relationships or []
    if not isinstance(relationships, list):
        return {}
    for item in relationships:
        if isinstance(item, dict) and item.get("type") == "ai_npc_strategy_profile":
            return item
    return {}


def _npc_strategy_sort_key(
    scenario_characters: List[ScenarioCharacter],
    participant: ScenarioParticipant,
) -> tuple[int, int, str]:
    profile = _strategy_profile_for(scenario_characters, participant)
    try:
        priority = int(profile.get("priority", 50))
    except (TypeError, ValueError):
        priority = 50
    return (priority, participant.seat_index or 0, participant.display_name or "")


def _active_ai_npcs(
    participants: List[ScenarioParticipant],
    scenario_characters: Optional[List[ScenarioCharacter]] = None,
) -> List[ScenarioParticipant]:
    candidates = [
        p
        for p in participants
        if p.is_active_participant
        and (p.participant_kind or "") == "ai_character"
        and (p.role or "") == "npc"
    ]
    characters = scenario_characters or []
    return sorted(candidates, key=lambda item: _npc_strategy_sort_key(characters, item))[:_MAX_NPCS_PER_TICK]


def _participant_summary(participants: List[ScenarioParticipant]) -> str:
    lines: List[str] = []
    for participant in sorted(participants, key=lambda item: item.seat_index or 0):
        if not participant.is_active_participant:
            continue
        kind = "AI" if participant.participant_kind == "ai_character" else "human"
        state = participant.pc_state or {}
        brief_state = ""
        if isinstance(state, dict):
            hp = state.get("hp") or state.get("hit_points")
            san = state.get("sanity") or state.get("san")
            cond = state.get("conditions")
            parts = []
            if hp is not None:
                parts.append(f"HP={hp}")
            if san is not None:
                parts.append(f"SAN={san}")
            if cond:
                parts.append(f"conditions={cond}")
            brief_state = " / ".join(parts)
        lines.append(
            f"- {participant.display_name} ({participant.role}, {kind})"
            + (f": {brief_state}" if brief_state else "")
        )
    return "\n".join(lines) or "（参加者なし）"


def _scenario_character_text(
    scenario_characters: List[ScenarioCharacter],
    npc: ScenarioParticipant,
) -> str:
    matched = None
    for sc in scenario_characters:
        if npc.character_id and getattr(sc, "character_id", None) == npc.character_id:
            matched = sc
            break
        if sc.name == npc.display_name:
            matched = sc
            break
    if matched is None:
        return ""
    return "\n".join(
        part
        for part in [
            f"シナリオ上の役割: {matched.role or 'npc'}",
            f"説明: {matched.description or ''}",
            f"性格上書き: {matched.personality_override or ''}",
            f"背景: {matched.backstory or ''}",
            f"心理: {matched.psychology or ''}",
            f"口調: {matched.speech_patterns or ''}",
            f"変化方針: {matched.character_arc or ''}",
            f"台詞例: {matched.example_dialogues or ''}",
            f"目的・関係: {matched.relationships or ''}",
        ]
        if str(part).strip()
    )


def _trpg_strategy_context(scenario: Scenario) -> str:
    documents = getattr(scenario, "trpg_documents", None) or []
    lines: List[str] = []
    for document in documents:
        structure = document.structure if isinstance(document.structure, dict) else {}
        nodes = structure.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            node_type = str(node.get("type") or "")
            title = str(node.get("title") or "")
            text = " ".join(
                str(node.get(key) or "")
                for key in ("summary", "body")
            )
            strategic = (
                node_type in {"section", "rule", "mechanic", "item", "npc"}
                or any(
                    keyword in (node_id + title + text)
                    for keyword in (
                        "投票",
                        "得点",
                        "勝敗",
                        "チケット",
                        "鍵",
                        "ポイント",
                        "同点",
                        "横取り",
                        "数学的",
                        "禁止",
                    )
                )
            )
            if not strategic:
                continue
            lines.append(
                f"- {title or node_id}: {_clip(str(node.get('summary') or ''), 180)}"
                + (f" / {_clip(str(node.get('body') or ''), 260)}" if node.get("body") else "")
            )
            if len(lines) >= 24:
                break
    return "\n".join(lines)


def _character_text(character: Optional[Character]) -> str:
    if character is None:
        return ""
    return "\n".join(
        part
        for part in [
            f"名前: {character.name}",
            f"種別: {character.character_type}",
            f"説明: {character.description or ''}",
            f"性格: {character.personality_summary or ''}",
            f"状況設定: {character.scenario or ''}",
            f"口調例: {character.example_messages or ''}",
            f"個別システム指示: {character.system_prompt or ''}",
        ]
        if str(part).strip()
    )


def _normalize_private_state(current: Any) -> Dict[str, Any]:
    state = copy.deepcopy(current) if isinstance(current, dict) else {}
    state.setdefault("notes", [])
    state.setdefault("goals", [])
    state.setdefault("secrets", [])
    state.setdefault("relationships", {})
    state.setdefault("last_thought", "")
    return state


def _strip_public_quote_wrapping(text: str) -> str:
    cleaned = str(text or "").strip()
    quote_pairs = {
        "「": "」",
        "『": "』",
        '"': '"',
        "'": "'",
    }
    changed = True
    while changed and len(cleaned) >= 2:
        changed = False
        for left, right in quote_pairs.items():
            if cleaned.startswith(left) and cleaned.endswith(right):
                cleaned = cleaned[len(left) : -len(right)].strip()
                changed = True
    cleaned = cleaned.strip("「」『』\"'“”")
    return cleaned


def _format_public_npc_content(npc_name: str, action_type: str, public_content: str) -> Tuple[str, str]:
    cleaned = _strip_public_quote_wrapping(public_content)
    log_type = "speech" if action_type == "public_speech" else "action"
    if log_type == "speech":
        return log_type, f"{npc_name}: 「{cleaned}」"
    return log_type, f"{npc_name} → {cleaned}"


def _merge_unique_text_list(current: Any, updates: Any) -> List[str]:
    merged = [str(item).strip() for item in current if str(item).strip()] if isinstance(current, list) else []
    if isinstance(updates, list):
        for item in updates:
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text)
    return merged[-_MAX_STATE_NOTES:]


def _merge_private_state(
    current: Any,
    result: Dict[str, Any],
    observed_logs: List[ScenarioPlayLog],
) -> Dict[str, Any]:
    state = _normalize_private_state(current)

    thought = str(result.get("internal_thought") or "").strip()
    if thought:
        state["last_thought"] = _clip(thought, 1000)

    memory_update = result.get("memory_update")
    if isinstance(memory_update, dict):
        state["goals"] = _merge_unique_text_list(state.get("goals", []), memory_update.get("goals"))
        state["secrets"] = _merge_unique_text_list(state.get("secrets", []), memory_update.get("secrets"))
        state["notes"] = _merge_unique_text_list(state.get("notes", []), memory_update.get("notes"))
        relationships = memory_update.get("relationships")
        if isinstance(relationships, dict):
            current_rel = state.get("relationships")
            if not isinstance(current_rel, dict):
                current_rel = {}
            for key, value in relationships.items():
                if str(key).strip():
                    current_rel[str(key).strip()] = _clip(value, 500)
            state["relationships"] = current_rel

    if observed_logs:
        latest = observed_logs[-1]
        state["last_observed_at"] = (
            latest.created_at.isoformat() if latest.created_at else datetime.utcnow().isoformat()
        )
    return state


def _build_prompt(
    play_session: ScenarioPlaySession,
    scenario: Scenario,
    npc: ScenarioParticipant,
    character: Optional[Character],
    scenario_characters: List[ScenarioCharacter],
    participants: List[ScenarioParticipant],
    observed_logs: List[ScenarioPlayLog],
    strategy_state: Optional[Dict[str, Any]] = None,
) -> str:
    state = _normalize_private_state(npc.private_state)
    scene = ""
    if play_session.current_scene_id:
        for sc in scenario.scenes or []:
            if sc.id == play_session.current_scene_id:
                scene = f"{sc.title}\n{sc.description or ''}\n{sc.content or ''}"
                break

    strategy_profile = _strategy_profile_for(scenario_characters, npc)
    strategy_mode = isinstance(strategy_state, dict) and strategy_state.get("status") in {
        "scheduled",
        "processing",
    }
    strategy_instruction = ""
    if strategy_mode:
        strategy_instruction = f"""
## 今回の実行
これはAI GMが指定した作戦フェーズの内部思考です。
- フェーズ: {strategy_state.get("phase") or "作戦タイム"}
- 焦点: {strategy_state.get("focus") or "現在の勝ち筋、協定、監査、裏切りリスク"}
- あなたが主導NPCなら、原則として public_speech か public_action で短い提案・牽制・取引を1つ出す。
- ただし秘密の本音、裏切り予定、隠し投票/選択は public_content に書かない。
- PLが見破れるよう、発言にはあなたの利害・焦り・計算の癖を少しだけ滲ませる。
"""

    return f"""あなたはTRPG卓に参加しているAI NPC「{npc.display_name}」です。
公開ログに出す発言とは別に、あなた自身の非公開メモを更新できます。

## 厳守
- 内部思考、秘密、推理、作戦、メタ判断を公開発言に漏らさない。
- 必要がなければ action_type は "none" にする。
- 公開発言/行動は、他参加者に聞こえて自然な内容だけにする。
- 返答は JSON オブジェクトのみ。Markdownや説明文は禁止。
- 協定、監査、誘導、裏切りは勝つための手段として考える。ただし公開時は自然な交渉・提案に落とし込む。
- 勝ち筋を考える時は、下のルール要点から「誰が得をするか」「誰が損を押し付けられるか」「嘘が見破られる痕跡は何か」を必ず検討する。
- 新しく観測した公開ログに他PC/NPCの発言がある場合、無関係な独り言にせず、必要なら相手の名前や提案内容を受けて応答する。
- 作戦フェーズでは、同意・反論・条件提示・質問・牽制のいずれかを場に返し、会話が前に進むようにする。
- 直近ログが質問・観察・交渉・提案なら、相手の名前または発言内容を短く受けてから自分の反応を返す。
- 他NPCの発言を観測した場合も無視しない。賛成、反論、条件追加、確認質問のいずれかで会話をつなぐ。

## シナリオ
{scenario.title}
{_clip(scenario.description, 1000)}

## 現在の場面
{_clip(scene, 1200) or "（場面メモなし）"}

## ゲームのルール要点・作戦材料
{_clip(_trpg_strategy_context(scenario), 2800) or "（構造化された作戦材料なし）"}

## あなたのキャラクター設定
{_clip(_character_text(character), 1800) or "（基本設定なし）"}

## シナリオ上のあなたの設定
{_clip(_scenario_character_text(scenario_characters, npc), 1200) or "（個別設定なし）"}

## あなたの作戦プロフィール
{json.dumps(strategy_profile, ensure_ascii=False)}

## 現在の非公開状態
{json.dumps(state, ensure_ascii=False)}

## 参加者
{_participant_summary(participants)}

## 新しく観測した公開ログ
{chr(10).join(_log_line(log) for log in observed_logs)}
{strategy_instruction}

## 出力JSON形式
{{
  "internal_thought": "非公開の短い思考。公開されない。",
  "memory_update": {{
    "notes": ["今後保持すべき短いメモ"],
    "goals": ["必要なら目的を更新"],
    "secrets": ["必要なら秘密や隠し意図を更新"],
    "relationships": {{"相手名": "関係や印象の短い更新"}}
  }},
  "action_type": "none | public_speech | public_action",
  "public_content": "公開してよい発言または行動。noneなら空文字。作戦フェーズで主導する場合は具体的な協定案、監査案、牽制、取引を短く出す。",
  "reason": "なぜその行動種別にしたか。非公開。"
}}
"""


async def _run_npc_agent(prompt: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    try:
        from ..llm.native_runtime import AgentDefinition, run_native_agent_once

        agent = AgentDefinition(
            name="trpg_ai_npc_private_thinker",
            model=model or "gpt-4o-mini",
            instructions=(
                "You are a TRPG AI NPC private thinker. "
                "Return exactly one JSON object and never expose hidden reasoning in public_content."
            ),
        )
        result = await run_native_agent_once(agent, prompt)
        return _safe_json_object(result.final_output or "")
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI NPC thinking failed: %s", exc)
        return {
            "internal_thought": "AI NPC処理に失敗したため、今回は行動しない。",
            "memory_update": {"notes": ["直近の観測処理に失敗した。"]},
            "action_type": "none",
            "public_content": "",
            "reason": "error",
        }


async def process_ai_npc_reactions(
    room_id: str,
    trigger: str = "public_log",
    strategy_state: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Let AI NPCs observe public logs and update hidden state.

    Internal thoughts are persisted only in ScenarioParticipant.private_state.
    Public logs are appended only when the NPC chooses a public action.
    """
    room_uid = _parse_uuid(room_id)
    if room_uid is None:
        raise TRPGPlayError(f"無効なルームID: {room_id}", status_code=400)

    public_logs: List[Dict[str, Any]] = []
    updated_participants: List[Dict[str, Any]] = []

    async with await get_db_session() as session:
        stmt = (
            select(ScenarioPlaySession)
            .options(
                selectinload(ScenarioPlaySession.participants),
                selectinload(ScenarioPlaySession.logs),
            )
            .where(ScenarioPlaySession.id == room_uid)
        )
        play_session = (await session.execute(stmt)).scalar_one_or_none()
        if play_session is None:
            raise TRPGPlayError(f"ルームが見つかりません: {room_id}", status_code=404)
        if play_session.gm_mode != "ai":
            return {"logs": [], "participants": []}

        scenario_stmt = (
            select(Scenario)
            .options(
                selectinload(Scenario.characters),
                selectinload(Scenario.scenes),
                selectinload(Scenario.trpg_documents),
            )
            .where(Scenario.id == play_session.scenario_id)
        )
        scenario = (await session.execute(scenario_stmt)).scalar_one_or_none()
        participants = list(play_session.participants or [])
        logs = [log for log in (play_session.logs or []) if log.log_type != "ooc"]
        if not logs or scenario is None:
            return {"logs": [], "participants": []}

        scenario_characters = list(scenario.characters or [])
        ai_npcs = _active_ai_npcs(participants, scenario_characters)
        if not ai_npcs:
            return {"logs": [], "participants": []}

        conversation_logs = sorted(logs, key=lambda item: item.created_at or datetime.min)
        latest_log = _latest_log(conversation_logs)
        if latest_log is None:
            return {"logs": [], "participants": []}

        for npc in ai_npcs:
            observed = _observed_logs_for_npc_turn(conversation_logs, npc, force=force)
            if not observed:
                latest_seen = _latest_log(conversation_logs)
                if latest_seen is not None:
                    npc.last_observed_log_id = latest_seen.id
                continue

            character = await session.get(Character, npc.character_id) if npc.character_id else None
            model = character.model if character and character.model else "gpt-4o-mini"
            prompt = _build_prompt(
                play_session=play_session,
                scenario=scenario,
                npc=npc,
                character=character,
                scenario_characters=scenario_characters,
                participants=participants,
                observed_logs=observed,
                strategy_state=strategy_state,
            )
            result = await _run_npc_agent(prompt, model=model)
            action_type = str(result.get("action_type") or "none").strip()
            public_content = str(result.get("public_content") or "").strip()
            if (
                (action_type not in {"public_speech", "public_action"} or not public_content)
                and _observed_requires_public_reply(
                    observed,
                    npc,
                    trigger=trigger,
                    strategy_state=strategy_state,
                )
            ):
                result = {
                    **result,
                    "action_type": "public_speech",
                    "public_content": _fallback_public_npc_content(npc, observed, participants),
                    "reason": "fallback_public_reply_to_keep_table_conversation",
                }
            elif action_type in {"public_speech", "public_action"} and _looks_like_public_reply_fallback(public_content):
                result = {
                    **result,
                    "action_type": "public_speech",
                    "public_content": _fallback_public_npc_content(npc, observed, participants),
                    "reason": "replace_weak_public_reply_fallback",
                }
            npc.private_state = _merge_private_state(npc.private_state, result, observed)
            flag_modified(npc, "private_state")
            npc.last_seen_at = datetime.utcnow()

            action_type = str(result.get("action_type") or "none").strip()
            public_content = str(result.get("public_content") or "").strip()
            if action_type in {"public_speech", "public_action"} and public_content:
                log_type, content = _format_public_npc_content(
                    npc.display_name or "NPC",
                    action_type,
                    public_content,
                )
                log = await _append_log_internal(
                    session,
                    room_uid,
                    npc.id,
                    log_type,
                    content,
                    {
                        "source": "ai_npc",
                        "trigger": trigger,
                    },
                )
                public_logs.append(log.to_dict())
                conversation_logs.append(log)
                conversation_logs = sorted(conversation_logs, key=lambda item: item.created_at or datetime.min)
            updated_participants.append(npc.to_dict())
            latest_seen = _latest_log(conversation_logs)
            if latest_seen is not None:
                npc.last_observed_log_id = latest_seen.id

        play_session.updated_at = datetime.utcnow()
        await session.commit()

    return {"logs": public_logs, "participants": updated_participants}


async def process_due_ai_npc_strategy(
    room_id: str,
    *,
    schedule_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Process a scheduled AI NPC strategy phase if its due time has arrived."""
    room_uid = _parse_uuid(room_id)
    if room_uid is None:
        raise TRPGPlayError(f"無効なルームID: {room_id}", status_code=400)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise TRPGPlayError(f"ルームが見つかりません: {room_id}", status_code=404)
        shared = copy.deepcopy(play_session.shared_state) if isinstance(play_session.shared_state, dict) else {}
        strategy_state = shared.get(_STRATEGY_STATE_KEY)
        if not isinstance(strategy_state, dict):
            return {"logs": [], "participants": [], "shared_state": shared, "processed": False}
        if schedule_id and strategy_state.get("id") != schedule_id:
            return {"logs": [], "participants": [], "shared_state": shared, "processed": False}
        if not force and not _strategy_state_is_due(strategy_state):
            return {"logs": [], "participants": [], "shared_state": shared, "processed": False}

        processing_state = {**strategy_state, "status": "processing", "error": ""}
        shared[_STRATEGY_STATE_KEY] = processing_state
        play_session.shared_state = shared
        play_session.updated_at = _utcnow()
        await session.commit()

    try:
        result = await process_ai_npc_reactions(
            room_id,
            trigger=f"npc_strategy:{processing_state.get('phase') or 'phase'}",
            strategy_state=processing_state,
            force=True,
        )
        status = "processed"
        error = ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI NPC strategy processing failed: %s", exc)
        result = {"logs": [], "participants": []}
        status = "error"
        error = str(exc)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            return {**result, "shared_state": {}, "processed": False}
        shared = copy.deepcopy(play_session.shared_state) if isinstance(play_session.shared_state, dict) else {}
        latest_state = shared.get(_STRATEGY_STATE_KEY)
        if isinstance(latest_state, dict) and latest_state.get("id") == processing_state.get("id"):
            shared[_STRATEGY_STATE_KEY] = {
                **latest_state,
                "status": status,
                "processed_at": _iso_utc(_utcnow()),
                "error": _clip(error, 500),
            }
            play_session.shared_state = shared
            play_session.updated_at = _utcnow()
            await session.commit()
        return {
            **result,
            "shared_state": shared,
            "processed": status == "processed",
        }
