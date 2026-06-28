"""Unified system prompts for LLM clients."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from ..config import Config
logger = logging.getLogger(__name__)


def _build_skills_section() -> str:
    """Keep installed skill details out of the global prompt.

    Skill names/descriptions can strongly bias unrelated chats, especially
    project/WBS skills. The main assistant can invoke an installed skill
    directly when it is explicitly relevant.
    """
    return ""


def build_unified_instructions(
    character_name: str,
    config: Optional[Config] = None,
    include_mcp_info: bool = False,
    available_mcp_servers: Optional[Dict] = None,
    rp_settings: Optional[Dict] = None,
    custom_instructions: Optional[str] = None,
) -> str:
    """Build the shared system prompt for LLM clients.

    キャラクタータイプに応じてプロンプトを分岐する:
    - assistant: 従来のアシスタント用プロンプト
    - roleplay / trpg_npc / gm: ロールプレイ用プロンプト
    """
    if config:
        character_config = config.get_character_config(character_name)
        db_char = character_config.get("_db_character", {})
        char_type = (
            db_char.get("character_type", "assistant")
            if isinstance(db_char, dict)
            else "assistant"
        )

        if char_type in ("roleplay", "trpg_npc", "gm"):
            instructions = _build_roleplay_prompt(
                character_config, db_char, rp_settings=rp_settings
            )
        elif char_type == "writer":
            instructions = _build_writer_prompt(character_config, db_char)
        else:
            instructions = _build_assistant_prompt(character_name, config)
    else:
        instructions = _build_assistant_prompt(character_name, config)

    extra = str(custom_instructions or "").strip()
    if not extra:
        return instructions

    return (
        f"{instructions}\n\n"
        "ユーザー別の追加指示:\n"
        f"{extra}"
    )


def _build_assistant_prompt(
    character_name: str,
    config: Optional[Config] = None,
) -> str:
    """従来のアシスタント用システムプロンプトを構築する。"""
    if config:
        character_config = config.get_character_config(character_name)
        personality = character_config.get("personality", {})
        display_name = character_config.get("name", character_name)
        details = personality.get("details", "")
        character_intro = f"あなたは「{display_name}」です。{details}".strip()
    else:
        character_intro = "あなたは親切なAIアシスタントです。"

    instructions = f"""
{character_intro}
あなたはAoiTalk上で動作する会話エージェントです。
選択されたキャラクターの口調と設定を守り、ユーザーの依頼に直接答えてください。

基本方針:
- 日本語で簡潔に答えてください。
- 不明な事実を断定しないでください。
- ツール結果や明示された文脈にない外部状態を、確認済みのように扱わないでください。
- 依頼を満たすために必要な情報源とツールは漏れなく使ってください。
- 依頼に関係しないツールや文脈は使わないでください。

ツール使用:
- ツールが必要な場合は、通常回答ではなく次の形式で出力してください。
[TOOL_CALL: tool_name(key=value, key2=value2)]
- 引数が不要な場合は次の形式で出力してください。
[TOOL_CALL: tool_name()]
- ツールを使う必要がない場合は、そのまま通常回答してください。
"""
    return instructions.strip()


def _build_roleplay_prompt(
    character_config: dict,
    db_char: dict,
    rp_settings: Optional[Dict] = None,
) -> str:
    """ロールプレイ専用のシステムプロンプトを構築する。

    Character Card V2 相当のフィールドを使い、
    キャラクターになりきるための指示を生成する。
    """
    name = character_config.get("name", "")
    description = db_char.get("description", "") if isinstance(db_char, dict) else ""
    personality = (
        db_char.get("personality_summary", "") if isinstance(db_char, dict) else ""
    )
    scenario = db_char.get("scenario", "") if isinstance(db_char, dict) else ""
    example_messages = (
        db_char.get("example_messages", "") if isinstance(db_char, dict) else ""
    )
    system_prompt = (
        db_char.get("system_prompt", "") if isinstance(db_char, dict) else ""
    )

    sections = []
    sections.append(f"あなたは「{name}」です。常にキャラクターになりきってください。")
    sections.append(
        "行動やナレーションは *アスタリスク* で囲み、台詞はそのまま記述してください。"
    )

    if description:
        sections.append(f"\n## キャラクター設定\n{description}")
    if personality:
        sections.append(f"\n## 性格\n{personality}")
    if scenario:
        sections.append(f"\n## シナリオ\n{scenario}")
    if example_messages:
        sections.append(f"\n## 会話例\n{example_messages}")
    if system_prompt:
        sections.append(f"\n## 追加指示\n{system_prompt}")

    # RPステアリングスライダー値をプロンプトに注入
    if rp_settings:
        steering_section = _build_rp_steering_section(rp_settings)
        if steering_section:
            sections.append(steering_section)

    # 動的画像生成指示
    auto_image_gen = (
        db_char.get("auto_image_gen", False) if isinstance(db_char, dict) else False
    )
    if auto_image_gen:
        trigger = db_char.get("image_gen_trigger", "scene_change")
        interval = db_char.get("image_gen_interval", 5)
        trigger_hint = {
            "scene_change": "場面転換や背景が変わった時",
            "emotion_change": "感情・表情・姿勢が大きく変わった時",
            "every_n": f"およそ{interval}往復ごと、または絵として見せる価値がある時",
        }.get(trigger, "状況が大きく変化した時")
        sections.append(
            "\n## 画像生成指示\n"
            f"{trigger_hint}、"
            "応答の末尾に以下のタグを付けてください:\n"
            "[SCENE_DESCRIPTION: ここに状況の視覚的描写を英語のDanbooruタグ寄りに記述]\n"
            "このタグは内部処理用です。タグについて説明せず、必要な時だけ1つ付けてください。"
        )

    return "\n".join(sections)


def _build_writer_prompt(
    character_config: dict,
    db_char: dict,
) -> str:
    """執筆支援（writer）用のシステムプロンプトを構築する。

    voice定義やシーン情報を注入し、WritingAgentが一貫した文体で
    執筆できるようにする。
    """
    name = character_config.get("name", "")
    description = db_char.get("description", "") if isinstance(db_char, dict) else ""
    system_prompt = (
        db_char.get("system_prompt", "") if isinstance(db_char, dict) else ""
    )

    sections = []
    sections.append(
        f"あなたは「{name}」として小説・シナリオの執筆を支援するエージェントです。"
    )

    # 文体設定
    voice_tone = db_char.get("voice_tone", "") if isinstance(db_char, dict) else ""
    voice_tense = (
        db_char.get("voice_tense_rules", "") if isinstance(db_char, dict) else ""
    )
    voice_vocab = (
        db_char.get("voice_vocabulary_register", "")
        if isinstance(db_char, dict)
        else ""
    )
    voice_banned = (
        db_char.get("voice_banned_expressions", "") if isinstance(db_char, dict) else ""
    )

    if voice_tone or voice_tense or voice_vocab:
        voice_parts = ["## 文体定義"]
        if voice_tone:
            voice_parts.append(f"- トーン: {voice_tone}")
        if voice_tense:
            voice_parts.append(f"- 時制ルール: {voice_tense}")
        if voice_vocab:
            voice_parts.append(f"- 語彙レベル: {voice_vocab}")
        if voice_banned:
            voice_parts.append(f"- 禁止表現: {voice_banned}")
        sections.append("\n".join(voice_parts))

    if description:
        sections.append(f"\n## 執筆スタイル\n{description}")

    if system_prompt:
        sections.append(f"\n## 追加指示\n{system_prompt}")

    sections.append(
        "\n## 執筆ルール\n"
        "- 地の文と台詞を適切に混ぜる\n"
        "- 感覚描写（視覚以外も）を重視する\n"
        "- 「Show, don't tell」— 感情は行動や反応で示す\n"
        "- 文の長さにバリエーションをつける\n"
        "- AI臭い表現（「確かに」「実に」「まさに」の多用、三つ組リスト）を避ける"
    )

    return "\n".join(sections)


def _build_rp_steering_section(rp_settings: Dict) -> str:
    """RPステアリングスライダー値からプロンプト指示を構築する。

    各スライダーは0.0〜1.0の値を持ち、応答のスタイルを調整する。
    """
    parts = []

    creativity = rp_settings.get("creativity")
    if creativity is not None:
        if creativity > 0.7:
            parts.append("- 創造的で予想外の展開を積極的に取り入れてください。")
        elif creativity < 0.3:
            parts.append("- 設定に忠実で控えめな展開にしてください。")

    detail = rp_settings.get("detail")
    if detail is not None:
        if detail > 0.7:
            parts.append("- 描写を詳細にし、情景や感情を豊かに表現してください。")
        elif detail < 0.3:
            parts.append("- 簡潔に要点だけを述べてください。")

    tempo = rp_settings.get("tempo")
    if tempo is not None:
        if tempo > 0.7:
            parts.append("- テンポよく、短めの応答で会話を進めてください。")
        elif tempo < 0.3:
            parts.append("- ゆったりと、じっくり描写を重ねてください。")

    emotion = rp_settings.get("emotion")
    if emotion is not None:
        if emotion > 0.7:
            parts.append("- 感情表現を豊かにし、喜怒哀楽をはっきり出してください。")
        elif emotion < 0.3:
            parts.append("- 感情を抑えた冷静なトーンで応答してください。")

    if not parts:
        return ""

    return "\n## 応答スタイル指示\n" + "\n".join(parts)
