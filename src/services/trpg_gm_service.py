"""TRPG AI GM 連携サービス

ルームのコンテキストを GMAgent に流し込み、ナレーションを生成して
ログに追加する。描写テキストからマーカー
    [SCENE_CHANGE:...]  # アプリ内シーン定義がある卓のみ
    [IMAGE_TRIGGER:...]
    [REQUEST_ROLL:...]
    [NPC_STRATEGY:...]
    [SESSION_END:...]
を抽出してサイドエフェクトも実行する。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..agents.gm_agent import GMAgent
from ..memory.database import get_db_session
from ..models.ecc_models import (
    Scenario,
    ScenarioCharacter,
    ScenarioScene,
    TRPGScenarioDocument,
    TRPGRulesetProfile,
    ScenarioPlaySession,
    ScenarioParticipant,
    ScenarioPlayLog,
)
from .trpg_play_service import (
    TRPGPlayError,
    RoomNotFoundError,
    _append_log_internal,
    _load_room_with_children,
    _hydrate_room_dict,
    append_private_message_internal,
)
from .trpg_rules import get_ruleset_gm_rules_brief, normalize_ruleset_key
from .trpg_coc import is_coc_sheet, summarize_coc_state
from .trpg_rulebook_service import profile_model_to_runtime_dict
from .codex_image_generation_service import (
    CodexImageGenerationError,
    generate_codex_image,
)
from .trpg_rule_reference_service import build_ai_rule_context

logger = logging.getLogger(__name__)


_SCENE_CHANGE_RE = re.compile(r"\[SCENE_CHANGE:([^\]]+)\]")
_IMAGE_TRIGGER_RE = re.compile(r"\[IMAGE_TRIGGER:([^\]]+)\]")
_REQUEST_ROLL_RE = re.compile(
    r"\[REQUEST_ROLL:([^\]]+)\]"
)  # 例: [REQUEST_ROLL:participant=all,dice=1d100,target=察知]
_BGM_RE = re.compile(r"\[BGM:([^\]]+)\]")
_SESSION_END_RE = re.compile(r"\[SESSION_END(?::([^\]]+))?\]")
_NPC_STRATEGY_RE = re.compile(r"\[NPC_STRATEGY:([^\]]+)\]")
_DEFAULT_BGM_VOLUME = 0.45


def _clip_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _participants_summary(
    participants: List[ScenarioParticipant],
) -> str:
    lines = []
    for p in sorted(
        [p for p in participants if p.is_active_participant],
        key=lambda x: x.seat_index or 0,
    ):
        pc = p.pc_state or {}
        hp = pc.get("hp")
        max_hp = pc.get("max_hp")
        hp_str = f"HP {hp}/{max_hp}" if hp is not None else ""
        sheet_summary = summarize_coc_state(pc) if is_coc_sheet(pc) else ""
        role_label = {
            "player": "PC",
            "gm": "GM",
            "npc": "NPC",
            "observer": "観戦",
        }.get(p.role, p.role)
        conds = pc.get("conditions") or []
        cond_str = f" [{', '.join(conds)}]" if conds else ""
        detail = sheet_summary or hp_str
        lines.append(f"- {p.display_name}（{role_label}） {detail}{cond_str}")
    return "\n".join(lines) if lines else "（参加者なし）"


def _scenario_characters_summary(
    scenario_characters: List[ScenarioCharacter],
) -> str:
    lines = []
    for ch in sorted(scenario_characters, key=lambda c: c.sort_order or 0):
        role = ch.role or "npc"
        details = []
        for label, value in (
            ("説明", ch.description),
            ("性格", ch.personality_override),
            ("背景", ch.backstory),
            ("心理", ch.psychology),
            ("口調", ch.speech_patterns),
        ):
            text = str(value or "").strip()
            if text:
                details.append(f"{label}: {text}")
        desc = " / ".join(details)
        if desc and len(desc) > 420:
            desc = desc[:420] + "…"
        sheet = summarize_coc_state(ch.trpg_pc_state or "")
        sheet_part = f" / {sheet}" if sheet else ""
        lines.append(f"- {ch.name}（{role}）: {desc}{sheet_part}")
    return "\n".join(lines) if lines else ""


def _trpg_structure_summary(structure: Optional[Dict[str, Any]]) -> str:
    if not isinstance(structure, dict):
        return ""
    nodes = structure.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ""

    lines = [
        "## TRPG構造化インデックス",
        "卓進行で使う自己完結データ。場所、手掛かり、脅威、分岐はこの構造データとキャラクターDBを優先する。",
    ]
    for raw_node in nodes[:60]:
        if not isinstance(raw_node, dict):
            continue
        node_type = _clip_text(raw_node.get("type") or "custom", 40)
        title = _clip_text(raw_node.get("title") or raw_node.get("id") or "無題", 80)
        node_id = _clip_text(raw_node.get("id") or "", 80)
        tags = raw_node.get("tags") if isinstance(raw_node.get("tags"), list) else []
        tag_text = f" / tags: {', '.join(str(tag) for tag in tags[:6])}" if tags else ""
        header = f"- [{node_type}] {title}"
        if node_id:
            header += f" (id: {node_id})"
        lines.append(header + tag_text)
        summary = _clip_text(raw_node.get("summary"), 260)
        body = _clip_text(raw_node.get("body"), 320)
        if summary:
            lines.append(f"  要約: {summary}")
        if body:
            lines.append(f"  補足: {body}")

    links = structure.get("links")
    if isinstance(links, list) and links:
        lines.append("### ノード関係")
        for raw_link in links[:80]:
            if not isinstance(raw_link, dict):
                continue
            source = _clip_text(raw_link.get("from") or raw_link.get("source"), 80)
            target = _clip_text(raw_link.get("to") or raw_link.get("target"), 80)
            if not source or not target:
                continue
            relation = _clip_text(raw_link.get("relation") or "related", 40)
            lines.append(f"- {source} --{relation}--> {target}")
    return "\n".join(lines)


def _recent_logs_text(logs: List[ScenarioPlayLog], limit: int = 20) -> str:
    """直近ログを時系列順にGMへ渡すテキスト形式に整形する。"""
    tail = logs[-limit:] if len(logs) > limit else logs
    lines = []
    for log in tail:
        if log.log_type == "narration":
            lines.append(f"[GM] {log.content}")
        elif log.log_type == "action":
            lines.append(f"[行動] {log.content}")
        elif log.log_type == "speech":
            lines.append(f"[発言] {log.content}")
        elif log.log_type == "dice":
            lines.append(f"[ダイス] {log.content}")
        elif log.log_type == "scene_change":
            lines.append(f"[シーン切替] {log.content}")
        elif log.log_type == "system":
            # system ログは簡略化
            lines.append(f"[システム] {log.content}")
    return "\n".join(lines)


def _image_scene_context(
    *,
    scenario: Scenario,
    current_scene: Optional[ScenarioScene],
    participants: Optional[List[ScenarioParticipant]] = None,
    logs: Optional[List[ScenarioPlayLog]] = None,
    visible_narration: str = "",
    user_prompt: str = "",
) -> str:
    parts: List[str] = [f"Scenario title: {scenario.title}"]
    if scenario.description:
        parts.append(f"Scenario description:\n{scenario.description}")
    if scenario.setting:
        parts.append(f"Scenario setting / tone:\n{scenario.setting}")
    if current_scene:
        scene_lines = [f"Current scene: {current_scene.title}"]
        if current_scene.description:
            scene_lines.append(f"Description:\n{current_scene.description}")
        if current_scene.content:
            scene_lines.append(f"Scenario text:\n{_clip_text(current_scene.content, 900)}")
        if current_scene.gm_instructions:
            scene_lines.append(f"GM instructions:\n{current_scene.gm_instructions}")
        parts.append("\n".join(scene_lines))
    if participants:
        parts.append("Participants:\n" + _participants_summary(participants))
    if logs:
        parts.append("Recent play logs:\n" + (_recent_logs_text(logs, limit=12) or "No logs yet."))
    if visible_narration:
        parts.append(f"Latest GM narration:\n{visible_narration}")
    if user_prompt:
        parts.append(f"Participant image request:\n{user_prompt}")
    return "\n\n".join(part for part in parts if part.strip())


def _auto_bgm_enabled(shared_state: Optional[Dict[str, Any]]) -> bool:
    """BGM自動切替の有効状態。既存ルームは未設定なら有効として扱う。"""
    if not isinstance(shared_state, dict):
        return True
    value = shared_state.get("bgm_auto_enabled")
    return True if value is None else bool(value)


def _build_gm_input(
    play_session: ScenarioPlaySession,
    scenario: Scenario,
    current_scene: Optional[ScenarioScene],
    trpg_document: Optional[TRPGScenarioDocument],
    participants: List[ScenarioParticipant],
    logs: List[ScenarioPlayLog],
    user_request: str = "",
    ruleset_profile: Optional[Dict[str, Any]] = None,
    structured_rule_context: str = "",
) -> Dict[str, str]:
    """GMAgentへ渡すコンテキストを構築する。"""
    setting_parts: List[str] = []
    if scenario.description:
        setting_parts.append(f"## シナリオ概要\n{scenario.description}")
    if scenario.setting:
        setting_parts.append(f"## 運用方針\n{scenario.setting}")
    opening_text = str(getattr(scenario, "opening_text", "") or "").strip()
    if opening_text:
        setting_parts.append(
            "## 導入素材\n"
            "この文章はGMが導入描写へ展開するための素材。ログへそのまま貼らず、"
            "場所、周囲の物、同席者、できる行動が分かる形に再構成する。\n"
            f"{opening_text}"
        )
    if trpg_document:
        structure_summary = _trpg_structure_summary(
            getattr(trpg_document, "structure", {}) or {}
        )
        if structure_summary:
            setting_parts.append(structure_summary)
    ruleset_key = normalize_ruleset_key(
        getattr(scenario, "ruleset", "") if getattr(scenario, "scenario_kind", "trpg") == "trpg" else ""
    )
    if ruleset_profile:
        profile_lines = [
            "## TRPGルールシステム",
            f"ruleset: {ruleset_profile.get('key') or ruleset_key}",
            f"name: {ruleset_profile.get('display_name') or ruleset_key}",
        ]
        if ruleset_profile.get("edition"):
            profile_lines.append(f"edition: {ruleset_profile['edition']}")
        if ruleset_profile.get("description"):
            profile_lines.append(str(ruleset_profile["description"]))
        setting_parts.append("\n".join(profile_lines))
    if structured_rule_context:
        setting_parts.append("## Structured TRPG Rule References\n" + structured_rule_context)
    setting = "\n\n".join(setting_parts)

    scene_text = ""
    if current_scene:
        scene_text = f"【{current_scene.title}】\n{current_scene.description or ''}"
        if current_scene.content:
            scene_text += f"\n\n## シナリオ本文\n{current_scene.content}"
        if current_scene.gm_instructions:
            scene_text += f"\n\n(GM指示: {current_scene.gm_instructions})"

    ruleset_brief = get_ruleset_gm_rules_brief(
        scenario.tags,
        scenario.genre,
        getattr(scenario, "ruleset", "")
        if getattr(scenario, "scenario_kind", "trpg") == "trpg"
        else "",
        ruleset_profile,
    )
    ruleset_instructions = f"\n\n{ruleset_brief}" if ruleset_brief else ""
    coc_runtime_instructions = ""
    profile_system_type = ""
    if isinstance(ruleset_profile, dict):
        profile_system_type = str(ruleset_profile.get("system_type") or "")
    if ruleset_key in {"coc6", "coc7"} or profile_system_type == "coc":
        coc_runtime_instructions = (
            "\n\n## CoC実行処理との接続\n"
            "- CoC卓では、技能判定、抵抗表、HP/MP/SAN増減、戦闘、成長、呪文コスト、狂気は専用操作で状態反映できる。\n"
            "- HP/MP/SAN/状態異常を物語上で変える時は、描写内で対象、リソース、増減量、理由を明確に書く。ログに反映済みでなければ確定値として扱わない。\n"
            "- 戦闘は攻撃技能、防御、命中、ダメージ、重傷/意識不明/死亡の順に処理する。攻撃や回避の成否ログが無い時はREQUEST_ROLLで要求する。\n"
            "- 抵抗表が必要な時は、能動値と受動値を明記する。成長チェック、狂気表、呪文コストが必要な時も専用操作に渡せる粒度で示す。\n"
            "- CoC資料参照は構造化済みのルール項目・サプリ項目・神話生物DBを正本とし、未構造化の本文DBを正本にしない。"
        )
    bgm_instructions = ""
    if _auto_bgm_enabled(play_session.shared_state):
        bgm_instructions = (
            "\n\n## BGM自動切替\n"
            "- 場面、緊張/戦闘/探索/休息など雰囲気が大きく変わった時だけ、"
            "末尾に `[BGM:検索キーワード]` を1つ付ける。\n"
            "- 検索キーワードはBGMフォルダから探しやすい短い語にする"
            "（例: mysterious, battle, tense, sad, peaceful, horror）。\n"
            "- 無音にしたい時は `[BGM:stop]` を使う。変化がない時はBGMマーカーを出さない。"
        )
    npc_strategy_instructions = (
        "\n\n## AI NPC作戦フェーズ\n"
        "- 心理戦、秘密投票、秘密選択、協定、作戦会議、ピリオド開始直後、投票/選択前の相談時間に入る時だけ、"
        "末尾に `[NPC_STRATEGY:phase=作戦タイム,delay=30,focus=今回の争点]` を1つ付ける。\n"
        "- delay は通常30秒。すぐ考えさせたい明確な理由がある時だけ 0〜10 秒にする。\n"
        "- NPC_STRATEGY はNPCの内部思考予約用であり、NPCの秘密や本音を本文に書かない。"
    )
    opening_phase_instructions = (
        "\n\n## セッション開始時の導入方針\n"
        "- ログがまだ少ない初回描写では、PLが招待状・案内・受付・入室・主催アナウンスを経て卓へ入るところから始める。\n"
        "- 初回描写の冒頭1段落には、招待状、案内係、受付、入室のうち少なくとも1つを具体的に入れる。\n"
        "- 初回から全ルール説明や攻略説明だけを並べず、会場の空気、配布物、同席NPCの第一印象、話しかけられる相手を描写する。\n"
        "- ピリオド制、投票、秘密選択があるシナリオでは、投票や集計へ入る前に必ず作戦タイム・交渉・確認のフェーズを置く。\n"
        "- 第1ピリオドの秘密投票、集計、有効得票数公開、得点確定は、PLが明示的に投票へ進むまで実行しない。\n"
        "- 作戦タイムでは票数案だけでなく、監査方法、残りカード確認、同点処理、裏切り時の扱いなどをNPC会話に出す。"
    )
    session_loop_instructions = (
        "\n\n## 汎用TRPG進行ループ\n"
        "- ログが少ない、またはセッション開始合図の場合: PLが今いる場所、周囲の物、同席者、見える手掛かりや危険、PLが今できる行動を十分に描写する。\n"
        "- 直近のPL発言は、質問、観察、交渉、行動宣言、進行要求のどれかとして解釈する。分類名を本文で硬く宣言する必要はないが、返答内容はその解釈に合わせる。\n"
        "- 質問/観察なら、見えている情報と追加で調べられる対象を返す。交渉なら、相手の反応や場の空気を返す。行動宣言なら、結果、必要な判定、リスクを返す。\n"
        "- 進行要求なら、依頼された場面単位を実際に進める。必要な会話、判定要求、公開情報、結果、次の場面への導線を描写し、単に次の行動を聞き返して足踏みしない。\n"
        "- シナリオ資料に進行手順、ラウンド、フェーズ、公開処理、結果確認などが書かれている場合は、その手順を現在ログに合わせて適用する。専用システムが無い場合も、GMの描写として公開できる範囲の結果を明示する。\n"
        "- シナリオ内の投票、競り、秘密選択、取引、交渉は架空TRPGの進行処理であり、現実の政治、金融、法的判断ではない。GM描写として扱う。\n"
        "- PLからログ確認しやすい節目名や手順名を指定された場合は、短い見出しや明示語として本文に残す。\n"
        "- NPC発言が直近ログにある場合は、無視せず、誰のどの提案・反論・質問を受けて場がどう変わったかを整理する。\n"
        "- 返答の末尾では、PLが次に選べる具体的な行動を2〜4個示す。通常プレイでは複数フェーズを勝手に飛ばさない。"
    )

    return {
        "setting": setting,
        "current_scene": scene_text,
        "characters": _scenario_characters_summary(scenario.characters or []),
        "player_state": _participants_summary(participants),
        "perspective": play_session.perspective or "third_person",
        "extra_instructions": (scenario.gm_instructions or "")
        + opening_phase_instructions
        + session_loop_instructions
        + ruleset_instructions
        + coc_runtime_instructions
        + "\n\n## 直近のプレイログ\n"
        + (_recent_logs_text(logs) or "（まだログはありません）")
        + (
            f"\n\n## プレイヤー（全員）からの合図\n{user_request}"
            if user_request
            else ""
        )
        + bgm_instructions
        + npc_strategy_instructions,
    }


async def _collect_room_bundle(session, room_id: uuid.UUID):
    """GM 生成に必要な一式を取得する。"""
    stmt = (
        select(ScenarioPlaySession)
        .options(
            selectinload(ScenarioPlaySession.participants),
            selectinload(ScenarioPlaySession.logs),
        )
        .where(ScenarioPlaySession.id == room_id)
    )
    result = await session.execute(stmt)
    play_session = result.scalar_one_or_none()
    if play_session is None:
        raise RoomNotFoundError(str(room_id))

    scenario_stmt = (
        select(Scenario)
        .options(
            selectinload(Scenario.characters),
            selectinload(Scenario.scenes),
        )
        .where(Scenario.id == play_session.scenario_id)
    )
    scenario_res = await session.execute(scenario_stmt)
    scenario = scenario_res.scalar_one_or_none()
    if scenario is None:
        raise TRPGPlayError(
            f"シナリオが見つかりません: {play_session.scenario_id}",
            status_code=404,
        )

    current_scene = None
    if play_session.current_scene_id:
        current_scene = await session.get(ScenarioScene, play_session.current_scene_id)

    doc_res = await session.execute(
        select(TRPGScenarioDocument)
        .where(TRPGScenarioDocument.scenario_id == scenario.id)
        .order_by(TRPGScenarioDocument.created_at.desc())
    )
    trpg_document = doc_res.scalars().first()

    ruleset_key = normalize_ruleset_key(
        getattr(scenario, "ruleset", "") or getattr(trpg_document, "ruleset", "")
    )
    profile = await session.get(TRPGRulesetProfile, ruleset_key)
    return play_session, scenario, current_scene, trpg_document, profile


def _strip_markers(text: str) -> str:
    """プレイヤーに見せる描写からマーカーだけを取り除く。"""
    text = _SCENE_CHANGE_RE.sub("", text)
    text = _IMAGE_TRIGGER_RE.sub("", text)
    text = _REQUEST_ROLL_RE.sub("", text)
    text = _BGM_RE.sub("", text)
    text = _SESSION_END_RE.sub("", text)
    text = _NPC_STRATEGY_RE.sub("", text)
    return text.strip()


def _looks_like_gm_refusal(text: str) -> bool:
    """Detect generic model refusal text that is not useful as GM narration."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    refusal_markers = (
        "申し訳ありませんが、そのリクエストにはお応えできません",
        "そのリクエストにはお応えできません",
        "I can't comply",
        "I can’t comply",
        "I can't assist",
        "I’m sorry, but I can’t",
        "I'm sorry, but I can't",
    )
    return any(marker in normalized for marker in refusal_markers)


def _parse_request_roll(marker: str) -> Dict[str, Any]:
    """[REQUEST_ROLL:participant=all,dice=1d100,target=30] を dict に。"""
    parts = {}
    for chunk in marker.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _parse_session_end(marker: Optional[str]) -> Dict[str, str]:
    if not marker:
        return {"outcome": "completed", "summary": ""}
    parts = {}
    for chunk in marker.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    if not parts and marker.strip():
        parts["summary"] = marker.strip()
    return {
        "outcome": parts.get("outcome") or "completed",
        "summary": parts.get("summary") or "",
    }


async def _generate_trpg_scene_image(
    image_prompt: str,
    scenario: Scenario,
    current_scene: Optional[ScenarioScene],
    visible_narration: str,
    *,
    participants: Optional[List[ScenarioParticipant]] = None,
    logs: Optional[List[ScenarioPlayLog]] = None,
) -> Optional[Dict[str, Any]]:
    """TRPG GM描写からCodex CLI経由で画像を生成する。"""
    try:
        fixed_scene_tags = ""
        if current_scene and current_scene.image_prompt:
            fixed_scene_tags = current_scene.image_prompt

        result = await generate_codex_image(
            visual_request=image_prompt,
            scene_context=_image_scene_context(
                scenario=scenario,
                current_scene=current_scene,
                participants=participants,
                logs=logs,
                visible_narration=visible_narration,
            ),
            fixed_scene_tags=fixed_scene_tags,
        )
        if result and result.get("success"):
            return result
    except CodexImageGenerationError as e:
        logger.warning("TRPG シーン画像生成をスキップ: %s", e)
    except Exception as e:
        logger.warning("TRPG シーン画像生成で予期せぬエラー: %s", e, exc_info=True)
    return None


async def generate_gm_narration(
    room_id: str,
    user_request: str = "",
) -> Dict[str, Any]:
    """GMAgent を起動してナレーションを生成し、ルームログに追加する。

    Returns:
        {
          "narration": 公開ログに入れた描写（マーカー除去済み）,
          "raw": 生成そのまま,
          "markers": {"scene_change":..., "image":..., "request_roll":..., "bgm":...},
          "log": 追加された narration ログ dict,
        }
    """
    room_uid = None
    try:
        room_uid = uuid.UUID(str(room_id))
    except ValueError:
        raise TRPGPlayError(f"無効なルームID: {room_id}")

    async with await get_db_session() as session:
        (
            play_session,
            scenario,
            current_scene,
            trpg_document,
            ruleset_profile_model,
        ) = await _collect_room_bundle(
            session, room_uid
        )

        # 参加者とログを DB オブジェクトからそのまま使う
        participants = list(play_session.participants or [])
        logs = sorted(
            list(play_session.logs or []),
            key=lambda x: x.created_at or datetime.utcnow(),
        )
        ruleset_key = normalize_ruleset_key(
            getattr(scenario, "ruleset", "") or getattr(trpg_document, "ruleset", "")
        )
        structured_rule_context = await build_ai_rule_context(
            ruleset_key=ruleset_key,
            query="\n".join(
                part
                for part in [
                    user_request,
                    _recent_logs_text(logs, limit=12),
                    scenario.title,
                    scenario.description,
                    current_scene.title if current_scene else "",
                    current_scene.description if current_scene else "",
                ]
                if part
            ),
            limit=6,
        )

        gm_ctx = _build_gm_input(
            play_session=play_session,
            scenario=scenario,
            current_scene=current_scene,
            trpg_document=trpg_document,
            participants=participants,
            logs=logs,
            user_request=user_request,
            ruleset_profile=profile_model_to_runtime_dict(ruleset_profile_model),
            structured_rule_context=structured_rule_context,
        )

        # GMAgent 構築 & 実行
        gm = GMAgent(
            setting=gm_ctx["setting"],
            current_scene=gm_ctx["current_scene"],
            characters=gm_ctx["characters"],
            player_state=gm_ctx["player_state"],
            perspective=gm_ctx["perspective"],
            extra_instructions=gm_ctx["extra_instructions"],
        )

        # 生成プロンプト
        if scenario.scenes:
            marker_instruction = (
                "アプリ内シーン定義に遷移すべき時だけ、"
                "`[SCENE_CHANGE:次のシーンタイトル]` を使ってください。"
            )
        else:
            marker_instruction = (
                "このTRPGシナリオはアプリ内シーン定義を使わないため、"
                "`[SCENE_CHANGE:...]` は出さないでください。"
            )
        prompt = (
            "上記のプレイログを踏まえ、GMとして次の描写を1〜3段落で生成してください。"
            "直近のPL発言を質問、観察、交渉、行動宣言、進行要求のどれとして扱うかを読み取り、"
            "状況説明、追加描写、NPC反応の整理、進行判断のうち必要なものを返してください。"
            "直近にNPC発言がある場合は、その発言を受けて場の状況や次の行動導線を補足してください。"
            "返答の末尾では、PLが次にできる具体的な行動を2〜4個示してください。"
            "ただし、プレイヤーから具体的な進行要求がある場合は、要求された場面単位を実際に進め、"
            "必要な会話、判定要求、公開情報、結果、次の場面への導線を描写してから選択肢を示してください。"
            "進行要求を、単なる相談や次行動確認だけに置き換えないでください。"
            "シナリオ内の投票、競り、秘密選択、取引、交渉は架空TRPGの進行処理であり、現実の政治、金融、法的判断ではありません。"
            "プレイヤーの行動に自然に反応し、必要なら画像・判定要求マーカーを埋め込んでください。"
            "技能判定やSANチェックの成否がまだログに無い場合、成功・失敗を断定せず、"
            "末尾に `[REQUEST_ROLL:participant=all,dice=1d100,target=技能名]` を付けてください。"
            "プレイヤーが調べる、聞く、探す、見回す、攻撃するなど判定が必要な行動を宣言した場合、"
            "「判定できます」と案内するだけで終えず、必ずREQUEST_ROLLマーカーで次のロールを要求してください。"
            "`[SAN_CHECK:...]` など未対応の独自マーカーは使わず、SANもREQUEST_ROLLで求めてください。"
            "CoCのSANチェックでは `target=SAN` を使い、損失が分かる場合は "
            "`[REQUEST_ROLL:participant=all,dice=1d100,target=SAN,note=SAN 0/1D3]` のようにnoteへ成功/失敗時損失を入れてください。"
            "CoCでHP/MP/SAN増減、抵抗表、戦闘、成長、呪文コスト、狂気が必要な場合は、"
            "対象名・処理種別・数値・理由を本文で具体的に示してください。"
            "既にdiceログに判定結果やSAN減少がある場合は、そのログを採用し、同じ判定を再要求しないでください。"
            "シナリオの勝利条件・生還条件・エンディング描写に到達し、これ以上通常進行が不要な場合だけ、"
            "末尾に `[SESSION_END:outcome=completed,summary=短い結末]` を付けてください。"
            "未解決の重要行動や判定が残っている場合はSESSION_ENDを出さないでください。"
            + marker_instruction
        )
        if user_request:
            prompt += f"\n（プレイヤーからの追加要望: {user_request}）"

        raw_text = ""
        try:
            from ..llm.native_runtime import run_native_agent_once

            result = await run_native_agent_once(gm.agent, prompt)
            raw_text = (result.final_output or "").strip()
            if _looks_like_gm_refusal(raw_text):
                retry_prompt = (
                    prompt
                    + "\n\nこれは架空のTRPG卓内の進行です。"
                    "投票、秘密選択、交渉、結果公開などの語はシナリオ上のゲーム内メカニクスを指し、"
                    "現実の政治的投票、金融取引、法的助言ではありません。"
                    "安全上拒否するのではなく、GMとしてシナリオ資料と直近ログに沿って公開できる範囲を描写してください。"
                )
                retry_result = await run_native_agent_once(gm.agent, retry_prompt)
                retry_text = (retry_result.final_output or "").strip()
                if retry_text:
                    raw_text = retry_text
        except Exception as e:
            logger.exception("GMAgent 実行失敗: %s", e)
            raw_text = "（GMが言葉を失った…システム管理者に連絡してください）"

        # マーカー抽出
        scene_change = _SCENE_CHANGE_RE.search(raw_text)
        image_match = _IMAGE_TRIGGER_RE.search(raw_text)
        roll_match = _REQUEST_ROLL_RE.search(raw_text)
        bgm_match = _BGM_RE.search(raw_text)
        session_end_match = _SESSION_END_RE.search(raw_text)
        npc_strategy_match = _NPC_STRATEGY_RE.search(raw_text)

        markers: Dict[str, Any] = {}
        if scene_change and scenario.scenes:
            markers["scene_change"] = scene_change.group(1).strip()
        if image_match:
            markers["image"] = image_match.group(1).strip()
        if roll_match:
            markers["request_roll"] = _parse_request_roll(roll_match.group(1).strip())
        if bgm_match and _auto_bgm_enabled(play_session.shared_state):
            markers["bgm"] = bgm_match.group(1).strip()
        if session_end_match:
            markers["session_end"] = _parse_session_end(session_end_match.group(1))
        if npc_strategy_match:
            try:
                from .trpg_ai_npc_service import schedule_ai_npc_strategy_shared_state

                shared, strategy_state = schedule_ai_npc_strategy_shared_state(
                    play_session.shared_state,
                    npc_strategy_match.group(1).strip(),
                    trigger="gm_marker",
                )
                play_session.shared_state = shared
                markers["npc_strategy"] = strategy_state
            except Exception as e:  # noqa: BLE001
                logger.warning("AI NPC strategy scheduling skipped: %s", e)

        visible = _strip_markers(raw_text) or "……"

        side_effect_logs: List[ScenarioPlayLog] = []

        # narration ログ
        narration_log = await _append_log_internal(
            session,
            room_uid,
            None,
            "narration",
            visible,
            {"raw": raw_text, "markers": markers},
        )
        side_effect_logs.append(narration_log)

        # サイドエフェクト: シーン切替マーカー → 該当シーンを探して遷移
        if "scene_change" in markers:
            target_title = markers["scene_change"]
            matched_scene = None
            for sc in sorted(scenario.scenes or [], key=lambda s: s.sort_order or 0):
                if sc.title and target_title.lower() in sc.title.lower():
                    matched_scene = sc
                    break
            if matched_scene:
                play_session.current_scene_id = matched_scene.id
                scene_log = await _append_log_internal(
                    session,
                    room_uid,
                    None,
                    "scene_change",
                    f"【シーン切替】{matched_scene.title}",
                    {
                        "from_scene_id": (
                            str(current_scene.id) if current_scene else None
                        ),
                        "to_scene_id": str(matched_scene.id),
                        "title": matched_scene.title,
                    },
                )
                side_effect_logs.append(scene_log)

        # サイドエフェクト: 画像マーカー → ComfyUIで生成して image ログにする
        if "image" in markers:
            image_result = await _generate_trpg_scene_image(
                markers["image"],
                scenario,
                current_scene,
                visible,
                participants=participants,
                logs=logs,
            )
            image_log = await _append_log_internal(
                session,
                room_uid,
                None,
                "image",
                markers["image"],
                {
                    "prompt": markers["image"],
                    "path": image_result.get("image_path") if image_result else None,
                    "url": image_result.get("image_url") if image_result else None,
                    "filename": image_result.get("filename") if image_result else None,
                    "engine": image_result.get("engine") if image_result else "codex-cli",
                    "model": image_result.get("model") if image_result else None,
                    "reasoning_effort": (
                        image_result.get("reasoning_effort") if image_result else None
                    ),
                },
            )
            side_effect_logs.append(image_log)

        # サイドエフェクト: BGMマーカー → shared_state 更新
        if "bgm" in markers:
            shared = play_session.shared_state or {}
            shared["bgm"] = {
                "track": markers["bgm"],
                "volume": _DEFAULT_BGM_VOLUME,
                "at": datetime.utcnow().isoformat(),
            }
            play_session.shared_state = shared
            action = "stop" if markers["bgm"].lower() == "stop" else "play"
            bgm_log = await _append_log_internal(
                session,
                room_uid,
                None,
                "bgm",
                f"♪ BGM: {markers['bgm']}",
                {
                    "action": action,
                    "track": markers["bgm"],
                    "volume": _DEFAULT_BGM_VOLUME,
                    "source": "ai_gm",
                },
            )
            side_effect_logs.append(bgm_log)

        if "session_end" in markers:
            end_info = markers["session_end"]
            shared = dict(play_session.shared_state or {})
            shared["post_session"] = {
                **(shared.get("post_session") if isinstance(shared.get("post_session"), dict) else {}),
                "outcome": end_info.get("outcome") or "completed",
                "summary": end_info.get("summary") or "",
                "completed_at": datetime.utcnow().isoformat(),
                "source": "ai_gm",
            }
            play_session.shared_state = shared
            play_session.status = "completed"
            end_log = await _append_log_internal(
                session,
                room_uid,
                None,
                "system",
                f"セッション終了: {end_info.get('summary') or end_info.get('outcome') or 'completed'}",
                {"event": "session_completed", **end_info, "source": "ai_gm"},
            )
            side_effect_logs.append(end_log)

        play_session.last_gm_activity_at = datetime.utcnow()
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        for log in side_effect_logs:
            await session.refresh(log)

        return {
            "narration": visible,
            "raw": raw_text,
            "markers": markers,
            "log": narration_log.to_dict(),
            "logs": [log.to_dict() for log in side_effect_logs],
            "room": (
                await _hydrate_room_dict(session, await _load_room_with_children(session, room_uid))
                if "session_end" in markers
                else None
            ),
            "shared_state": play_session.shared_state or {},
        }


async def generate_current_scene_image(
    room_id: str,
    participant_id: str,
    user_prompt: str = "",
) -> Dict[str, Any]:
    """参加者の要求で、現在の卓状況を画像化してログへ追加する。"""
    room_uid = uuid.UUID(str(room_id))
    requester_uid = _parse_uuid(participant_id)
    if requester_uid is None:
        raise TRPGPlayError("参加者IDが不正です", status_code=400)

    async with await get_db_session() as session:
        (
            play_session,
            scenario,
            current_scene,
            _trpg_document,
            _profile,
        ) = await _collect_room_bundle(session, room_uid)
        requester = await session.get(ScenarioParticipant, requester_uid)
        if requester is None or requester.play_session_id != room_uid:
            raise TRPGPlayError("参加者が見つかりません", status_code=404)
        requester_display_name = requester.display_name

        participants = list(play_session.participants or [])
        logs = sorted(
            list(play_session.logs or []),
            key=lambda x: x.created_at or datetime.utcnow(),
        )
        visual_request = (
            user_prompt.strip()
            or "現在のTRPGセッション状況を、参加者が共有できる一枚のシーンイラストとして生成してください。"
        )
        context = _image_scene_context(
            scenario=scenario,
            current_scene=current_scene,
            participants=participants,
            logs=logs,
            user_prompt=visual_request,
        )
        fixed_scene_tags = current_scene.image_prompt if current_scene else ""

    try:
        image_result = await generate_codex_image(
            visual_request=visual_request,
            scene_context=context,
            fixed_scene_tags=fixed_scene_tags or "",
        )
    except CodexImageGenerationError as e:
        raise TRPGPlayError(f"画像生成に失敗しました: {e}", status_code=502) from e

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        requester = await session.get(ScenarioParticipant, requester_uid)
        if requester is None or requester.play_session_id != room_uid:
            raise TRPGPlayError("参加者が見つかりません", status_code=404)
        log = await _append_log_internal(
            session,
            room_uid,
            requester_uid,
            "image",
            visual_request,
            {
                "prompt": image_result.get("prompt") or visual_request,
                "path": image_result.get("image_path"),
                "url": image_result.get("image_url"),
                "filename": image_result.get("filename"),
                "engine": image_result.get("engine"),
                "model": image_result.get("model"),
                "reasoning_effort": image_result.get("reasoning_effort"),
                "requested_by_participant_id": str(requester_uid),
                "requested_by": requester_display_name,
                "request_type": "participant_current_scene",
            },
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(log)
        return {
            "image": image_result,
            "log": log.to_dict(),
            "logs": [log.to_dict()],
        }


async def generate_private_gm_reply(
    room_id: str,
    sender_participant_id: str,
    message_text: str,
) -> Dict[str, Any]:
    """PLからAI GM宛の秘匿相談に、公開ログへ出さず個別返信する。"""
    try:
        room_uid = uuid.UUID(str(room_id))
        sender_uid = uuid.UUID(str(sender_participant_id))
    except ValueError:
        raise TRPGPlayError("無効なルームまたは参加者IDです", status_code=400)

    async with await get_db_session() as session:
        (
            play_session,
            scenario,
            current_scene,
            trpg_document,
            ruleset_profile_model,
        ) = await _collect_room_bundle(session, room_uid)

        sender = await session.get(ScenarioParticipant, sender_uid)
        if sender is None or sender.play_session_id != room_uid:
            raise TRPGPlayError("送信者がルーム参加者ではありません", status_code=404)

        participants = list(play_session.participants or [])
        logs = sorted(
            list(play_session.logs or []),
            key=lambda x: x.created_at or datetime.utcnow(),
        )
        ruleset_key = normalize_ruleset_key(
            getattr(scenario, "ruleset", "") or getattr(trpg_document, "ruleset", "")
        )
        structured_rule_context = await build_ai_rule_context(
            ruleset_key=ruleset_key,
            query="\n".join([message_text, _recent_logs_text(logs, limit=12), scenario.title]),
            limit=6,
        )
        gm_ctx = _build_gm_input(
            play_session=play_session,
            scenario=scenario,
            current_scene=current_scene,
            trpg_document=trpg_document,
            participants=participants,
            logs=logs,
            user_request=f"{sender.display_name}からの秘匿相談: {message_text}",
            ruleset_profile=profile_model_to_runtime_dict(ruleset_profile_model),
            structured_rule_context=structured_rule_context,
        )

        gm = GMAgent(
            setting=gm_ctx["setting"],
            current_scene=gm_ctx["current_scene"],
            characters=gm_ctx["characters"],
            player_state=gm_ctx["player_state"],
            perspective=gm_ctx["perspective"],
            extra_instructions=gm_ctx["extra_instructions"],
        )
        prompt = (
            f"{sender.display_name}から、公開ログに出さない秘匿メッセージが届きました。\n"
            f"内容: {message_text}\n\n"
            "GMとして、宛先本人だけに見える返答を1〜2段落で返してください。"
            "他PLに未開示の情報を明かす場合は、本人の行動や所持情報に基づく範囲に留め、"
            "公開描写・公開ログ・画像生成・BGM・REQUEST_ROLLなどのマーカーは使わないでください。"
        )
        try:
            from ..llm.native_runtime import run_native_agent_once

            result = await run_native_agent_once(gm.agent, prompt)
            reply = _strip_markers((result.final_output or "").strip()) or "……"
        except Exception as e:
            logger.exception("Private AI GM reply failed: %s", e)
            reply = "（AI GMの個別返信生成に失敗しました。必要なら公開ログでGM描写を進めてください）"

    return await append_private_message_internal(
        room_id=room_id,
        sender_participant_id=None,
        sender_label="AI GM",
        target_participant_ids=[sender_participant_id],
        content=reply,
        message_type="gm",
        metadata={"reply_to_participant_id": sender_participant_id},
    )


async def submit_player_action(
    room_id: str,
    participant_id: str,
    action_text: str,
    action_kind: str = "action",  # "action" | "speech" | "ooc"
    generate_gm_reply: bool = True,
) -> Dict[str, Any]:
    """プレイヤーの行動/発言を記録し、続けて GM ナレーションを生成する。"""
    room_uid = uuid.UUID(str(room_id))
    pid = _parse_uuid(participant_id)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        participant = None
        if pid:
            participant = await session.get(ScenarioParticipant, pid)

        display = participant.display_name if participant else "誰か"
        log_type = (
            action_kind if action_kind in ("action", "speech", "ooc") else "action"
        )
        content = (
            f"{display}: 「{action_text}」"
            if log_type == "speech"
            else f"{display} → {action_text}"
        )

        action_log = await _append_log_internal(
            session,
            room_uid,
            pid,
            log_type,
            content,
            {"raw": action_text},
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(action_log)
        action_log_dict = action_log.to_dict()

    # OOC とルート側で後続処理を組み立てる場合は GM を起動しない
    if action_kind == "ooc" or not generate_gm_reply:
        return {"action_log": action_log_dict, "narration": None}

    # 続けて GM ナレーション（AI GM モードの時のみ）
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None or play_session.gm_mode != "ai":
            return {"action_log": action_log_dict, "narration": None}

    gm_result = await generate_gm_narration(room_id)
    return {"action_log": action_log_dict, **gm_result}


async def start_session_with_opening(room_id: str) -> Dict[str, Any]:
    """ルーム開始時にオープニングナレーションを生成する。"""
    return await generate_gm_narration(
        room_id,
        user_request=(
            "セッション開始です。既存のopening_textがある場合もそのまま貼らず、"
            "PLが招待状や案内に従って会場へ入り、受付・配布物・主催アナウンス・"
            "同席NPCの第一印象を順に受け取る導入から始めてください。"
            "冒頭1段落には必ず、招待状、案内係、受付、入室のいずれかを具体的に描写してください。"
            "初回から全ルール説明だけにせず、まだ第1ピリオドの秘密投票や集計へは進めず、"
            "作戦タイムで話しかけられる相手と確認すべき論点を提示してください。"
        ),
    )
