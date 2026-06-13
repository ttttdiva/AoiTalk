"""シナリオ執筆支援ツール

WritingAgentが使用する4つのツール:
- get_writing_context: 執筆に必要なコンテキストを自動収集
- save_scene_draft: 生成テキストをシーンのcontentに保存
- update_canon_from_content: 確定事実をcanonに追加
- get_character_voice: キャラクターの口調設定を取得
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .core import tool

logger = logging.getLogger(__name__)


@tool
async def get_writing_context(conversation_id: str) -> str:
    """執筆に必要なコンテキスト（シーン設定、キャラクター、文体定義など）を自動収集して返す。

    Args:
        conversation_id: 現在の会話セッションID

    Returns:
        Markdown形式の執筆コンテキスト
    """
    try:
        from ..services.scenario_service import (
            get_writing_session_by_conversation,
            get_scenario,
        )
        from ..memory.database import get_db_session
        from ..models.ecc_models import (
            ScenarioScene,
            ScenarioCharacter,
            ScenarioEpisode,
            ScenarioCanonEntry,
        )
        from sqlalchemy import select

        # 1. writing session取得
        writing_session = await get_writing_session_by_conversation(conversation_id)
        if not writing_session:
            return "エラー: この会話に関連付けられた執筆セッションが見つかりません。"

        target_scene_id = writing_session.get("target_scene_id")
        scenario_id = writing_session.get("scenario_id")

        if not scenario_id:
            return "エラー: 執筆セッションにシナリオが設定されていません。"

        # シナリオ全体を取得（キャラクター・シーン含む）
        scenario_data = await get_scenario(str(scenario_id), include_children=True)

        sections = []

        # シナリオ概要
        scenario_title = scenario_data.get("title", "")
        scenario_description = scenario_data.get("description", "")
        overview_parts = []
        if scenario_title:
            overview_parts.append(f"タイトル: {scenario_title}")
        if scenario_description:
            overview_parts.append(scenario_description)
        if overview_parts:
            sections.append("## シナリオ概要\n" + "\n\n".join(overview_parts))

        # 文体設定
        voice_tone = scenario_data.get("voice_tone", "")
        voice_tense = scenario_data.get("voice_tense_rules", "")
        voice_vocab = scenario_data.get("voice_vocabulary_register", "")
        voice_banned = scenario_data.get("voice_banned_expressions", "")
        if voice_tone or voice_tense or voice_vocab:
            voice_parts = []
            if voice_tone:
                voice_parts.append(f"トーン: {voice_tone}")
            if voice_tense:
                voice_parts.append(f"時制ルール: {voice_tense}")
            if voice_vocab:
                voice_parts.append(f"語彙レベル: {voice_vocab}")
            sections.append("## 文体設定\n" + "\n".join(voice_parts))
            if voice_banned:
                sections.append(f"禁止表現: {voice_banned}")

        # 世界設定
        setting = scenario_data.get("setting", "")
        if setting:
            sections.append(f"## 世界設定\n{setting}")

        gm_instructions = scenario_data.get("gm_instructions", "")
        if gm_instructions:
            sections.append(f"## 執筆指示\n{gm_instructions}")

        # エピソード情報（存在する場合）
        async with await get_db_session() as session:
            target_scene = None
            scenes = scenario_data.get("scenes", [])
            for s in scenes:
                if s.get("id") == str(target_scene_id):
                    target_scene = s
                    break

            # エピソード取得（episode_idがある場合）
            episode_id = None
            if target_scene:
                episode_id = target_scene.get("episode_id")

            if episode_id:
                try:
                    episode = await session.get(
                        ScenarioEpisode, uuid.UUID(str(episode_id))
                    )
                    if episode:
                        ep_dict = episode.to_dict()
                        ep_parts = [
                            f"## 現在のエピソード\nタイトル: {ep_dict.get('title', '')}"
                        ]
                        synopsis = (
                            ep_dict.get("synopsis_full")
                            or ep_dict.get("synopsis_paragraph")
                            or ep_dict.get("synopsis_sentence", "")
                        )
                        if synopsis:
                            ep_parts.append(f"あらすじ: {synopsis}")
                        beat_sheet = ep_dict.get("beat_sheet", [])
                        if beat_sheet:
                            beats_text = "\n".join(
                                (
                                    f"- {b}"
                                    if isinstance(b, str)
                                    else f"- {json.dumps(b, ensure_ascii=False)}"
                                )
                                for b in beat_sheet
                            )
                            ep_parts.append(f"ビートシート:\n{beats_text}")
                        sections.append("\n".join(ep_parts))
                except Exception as e:
                    logger.warning("エピソード取得エラー: %s", e)

            # 現在のシーン
            if target_scene:
                scene_parts = [
                    "## 現在のシーン",
                    f"タイトル: {target_scene.get('title', '')}",
                    f"タイプ: {target_scene.get('scene_type', 'normal')}",
                ]
                if target_scene.get("description"):
                    scene_parts.append(f"説明: {target_scene['description']}")
                if target_scene.get("gm_instructions"):
                    scene_parts.append(f"GM指示: {target_scene['gm_instructions']}")
                if target_scene.get("content"):
                    scene_parts.append(f"現在のコンテンツ:\n{target_scene['content']}")
                sections.append("\n".join(scene_parts))

            # 前後のシーン取得（対象シーンがある場合のみ）
            sorted_scenes = sorted(scenes, key=lambda x: x.get("sort_order", 0))
            target_idx = None
            if target_scene_id:
                for i, s in enumerate(sorted_scenes):
                    if s.get("id") == str(target_scene_id):
                        target_idx = i
                        break

            if target_idx is not None:
                # 前のシーン
                if target_idx > 0:
                    prev_scene = sorted_scenes[target_idx - 1]
                    prev_content = prev_scene.get("content", "")
                    if prev_content:
                        # 末尾2000文字のみ
                        prev_content = prev_content[-2000:]
                    sections.append(
                        f"## 前のシーン（末尾）\n"
                        f"タイトル: {prev_scene.get('title', '')}\n"
                        f"{prev_content if prev_content else '（コンテンツなし）'}"
                    )
                else:
                    sections.append("## 前のシーン（末尾）\n（なし - 最初のシーン）")

                # 次のシーン
                if target_idx < len(sorted_scenes) - 1:
                    next_scene = sorted_scenes[target_idx + 1]
                    sections.append(
                        f"## 次のシーン（概要）\n"
                        f"{next_scene.get('title', '')}: {next_scene.get('description', '')}"
                    )
            elif sorted_scenes:
                episodes = scenario_data.get("episodes", [])
                if episodes:
                    episode_lines = ["## エピソード一覧"]
                    for episode in sorted(
                        episodes, key=lambda x: x.get("sort_order", 0)
                    ):
                        synopsis = (
                            episode.get("synopsis_sentence")
                            or episode.get("synopsis_paragraph")
                            or ""
                        )
                        episode_lines.append(
                            f"- {episode.get('title', '')}: {synopsis}"
                        )
                    sections.append("\n".join(episode_lines))

                scene_lines = ["## シーン一覧"]
                for scene in sorted_scenes:
                    scene_lines.append(
                        f"- {scene.get('title', '')}: {scene.get('description', '')}"
                    )
                sections.append("\n".join(scene_lines))

            # 登場キャラクター
            characters = scenario_data.get("characters", [])
            if characters:
                char_sections = ["## 登場キャラクター"]
                for char in characters:
                    char_parts = [
                        f"### {char.get('name', '')} ({char.get('role', 'npc')})"
                    ]
                    if char.get("description"):
                        char_parts.append(f"性格: {char['description']}")
                    if char.get("speech_patterns"):
                        char_parts.append(f"口調: {char['speech_patterns']}")
                    if char.get("psychology"):
                        char_parts.append(f"心理: {char['psychology']}")
                    if char.get("example_dialogues"):
                        char_parts.append(f"会話例:\n{char['example_dialogues']}")
                    # personality_override がある場合
                    if char.get("personality_override"):
                        char_parts.append(
                            f"シナリオ固有の性格: {char['personality_override']}"
                        )
                    char_sections.append("\n".join(char_parts))
                sections.append("\n\n".join(char_sections))

            # Canon（確定事実）取得
            try:
                stmt = select(ScenarioCanonEntry).where(
                    ScenarioCanonEntry.scenario_id == uuid.UUID(str(scenario_id))
                )
                canon_result = await session.execute(stmt)
                canon_entries = canon_result.scalars().all()

                if canon_entries:
                    canon_by_category: Dict[str, List[str]] = {}
                    for entry in canon_entries:
                        cat = getattr(entry, "category", "その他") or "その他"
                        fact = getattr(entry, "fact", "") or str(entry)
                        canon_by_category.setdefault(cat, []).append(fact)

                    canon_parts = ["## 確定事実（Canon）"]
                    for cat, facts in canon_by_category.items():
                        canon_parts.append(f"### {cat}")
                        for f in facts:
                            canon_parts.append(f"- {f}")
                    sections.append("\n".join(canon_parts))
            except Exception as e:
                logger.warning("Canon取得エラー（テーブル未作成の可能性）: %s", e)

        # WorldBookエントリ取得
        try:
            from ..services.worldbook_service import get_matching_entries

            # 最新の会話テキストでマッチ（シーンのcontentを使用）
            recent_text = ""
            if target_scene and target_scene.get("content"):
                recent_text = target_scene["content"][-1000:]
            elif target_scene:
                recent_text = (
                    target_scene.get("description", "")
                    + " "
                    + target_scene.get("title", "")
                )

            # シナリオタイトルでもマッチを試みる
            recent_text += " " + scenario_data.get("title", "")

            # キャラクター名の特定（character_slugとしてシナリオタイトルを使用）
            character_slug = scenario_data.get("title", "")
            entries = await get_matching_entries(
                character_slug,
                recent_text,
                scenario_id=str(scenario_id),
            )
            if entries:
                wb_parts = ["## 世界情報（WorldBook）"]
                for e in entries:
                    name = e.get("name", "")
                    content = e.get("content", "")
                    if name:
                        wb_parts.append(f"### {name}\n{content}")
                    else:
                        wb_parts.append(content)
                sections.append("\n\n".join(wb_parts))
        except Exception as e:
            logger.warning("WorldBookエントリ取得エラー: %s", e)

        if not sections:
            return "コンテキスト情報が見つかりませんでした。シーンとシナリオの設定を確認してください。"

        return "\n\n".join(sections)

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("執筆コンテキスト取得エラー: %s", e)
        return f"コンテキスト取得中にエラーが発生しました: {e}"


@tool
async def save_scene_draft(conversation_id: str, content: str) -> str:
    """生成テキストをシーンのcontentに保存する。

    Args:
        conversation_id: 現在の会話セッションID
        content: 保存するシーンの本文テキスト

    Returns:
        保存結果のメッセージ
    """
    try:
        from ..services.scenario_service import (
            get_writing_session_by_conversation,
            save_scene_content,
        )

        # writing session取得
        writing_session = await get_writing_session_by_conversation(conversation_id)
        if not writing_session:
            return "エラー: この会話に関連付けられた執筆セッションが見つかりません。"

        target_scene_id = writing_session.get("target_scene_id")
        if not target_scene_id:
            return "エラー: 執筆セッションに対象シーンが設定されていません。"

        # シーンコンテンツを保存（バージョン作成あり）
        await save_scene_content(
            scene_id=str(target_scene_id),
            content=content,
            create_version=True,
        )

        # 文字数カウント
        char_count = len(content)
        return f"シーンの下書きを保存しました（{char_count}文字）。"

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("シーン下書き保存エラー: %s", e)
        return f"シーン下書きの保存中にエラーが発生しました: {e}"


@tool
async def update_canon_from_content(conversation_id: str, new_facts_json: str) -> str:
    """執筆中に確立された新しい確定事実をCanonに追加する。

    Args:
        conversation_id: 現在の会話セッションID
        new_facts_json: JSON文字列。[{"category": "geography|timeline|character|event|...", "fact": "確定事実テキスト"}]

    Returns:
        追加結果のメッセージ
    """
    try:
        from ..services.scenario_service import (
            get_writing_session_by_conversation,
        )
        from ..memory.database import get_db_session
        from ..models.ecc_models import ScenarioCanonEntry

        # writing session取得
        writing_session = await get_writing_session_by_conversation(conversation_id)
        if not writing_session:
            return "エラー: この会話に関連付けられた執筆セッションが見つかりません。"

        scenario_id = writing_session.get("scenario_id")
        target_scene_id = writing_session.get("target_scene_id")

        if not scenario_id:
            return "エラー: 執筆セッションにシナリオが設定されていません。"

        # JSONパース
        try:
            new_facts = json.loads(new_facts_json)
        except json.JSONDecodeError as e:
            return f"エラー: JSONのパースに失敗しました: {e}"

        if not isinstance(new_facts, list):
            return "エラー: new_facts_jsonはリスト形式である必要があります。"

        # 各factをcanonに追加
        added_count = 0
        async with await get_db_session() as session:
            for fact_data in new_facts:
                if not isinstance(fact_data, dict):
                    continue
                category = fact_data.get("category", "その他")
                fact = fact_data.get("fact", "")
                if not fact:
                    continue

                entry = ScenarioCanonEntry(
                    id=uuid.uuid4(),
                    scenario_id=uuid.UUID(str(scenario_id)),
                    category=category,
                    fact=fact,
                    source_scene_id=(
                        uuid.UUID(str(target_scene_id)) if target_scene_id else None
                    ),
                )
                session.add(entry)
                added_count += 1

            await session.commit()

        return f"Canon に {added_count} 件の確定事実を追加しました。"

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("Canon更新エラー: %s", e)
        return f"Canon更新中にエラーが発生しました: {e}"


@tool
async def get_character_voice(conversation_id: str, character_name: str) -> str:
    """特定キャラクターの口調設定と会話サンプルを返す。

    Args:
        conversation_id: 現在の会話セッションID
        character_name: キャラクター名

    Returns:
        キャラクターの口調設定情報
    """
    try:
        from ..services.scenario_service import (
            get_writing_session_by_conversation,
        )
        from ..services.scenario_service import get_scenario

        # writing session取得
        writing_session = await get_writing_session_by_conversation(conversation_id)
        if not writing_session:
            return "エラー: この会話に関連付けられた執筆セッションが見つかりません。"

        scenario_id = writing_session.get("scenario_id")
        if not scenario_id:
            return "エラー: 執筆セッションにシナリオが設定されていません。"

        # シナリオからキャラクター一覧取得
        scenario_data = await get_scenario(str(scenario_id), include_children=True)
        characters = scenario_data.get("characters", [])

        # 名前で検索（部分一致対応）
        target_char = None
        for char in characters:
            if char.get("name", "").lower() == character_name.lower():
                target_char = char
                break

        # 完全一致がなければ部分一致
        if not target_char:
            for char in characters:
                if character_name.lower() in char.get("name", "").lower():
                    target_char = char
                    break

        if not target_char:
            available = ", ".join(c.get("name", "") for c in characters)
            return f"キャラクター「{character_name}」が見つかりません。利用可能なキャラクター: {available}"

        # 口調情報を構築
        parts = [f"## {target_char.get('name', '')} ({target_char.get('role', 'npc')})"]

        if target_char.get("description"):
            parts.append(f"### 性格・概要\n{target_char['description']}")

        if target_char.get("personality_override"):
            parts.append(
                f"### シナリオ固有の性格\n{target_char['personality_override']}"
            )

        if target_char.get("speech_patterns"):
            parts.append(f"### 口調パターン\n{target_char['speech_patterns']}")

        if target_char.get("example_dialogues"):
            parts.append(f"### 会話例\n{target_char['example_dialogues']}")

        if target_char.get("psychology"):
            parts.append(f"### 心理\n{target_char['psychology']}")

        return "\n\n".join(parts)

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("キャラクター口調取得エラー: %s", e)
        return f"キャラクター口調の取得中にエラーが発生しました: {e}"
