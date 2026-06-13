"""シナリオ素材インポートツール

ディレクトリ/ファイルからキャラクター設定・世界設定・シーンを
AoiTalkのシナリオデータベースに取り込むためのツールを提供する。
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

from .core import tool

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────
# LLM抽出ヘルパー
# ────────────────────────────────────────────


async def _llm_extract(prompt: str, text: str) -> str:
    """LLMを使ってテキストから構造化データを抽出する。

    OpenAI APIを直接使用（抽出用なので軽量モデル）。
    """
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が設定されていません")

    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:8000]},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


# ────────────────────────────────────────────
# ファイル内容タイプ推定
# ────────────────────────────────────────────

_CATEGORY_PATTERNS = {
    "character": {
        "filename": re.compile(
            r"(キャラ|character|chara|人物|登場人物|設定|profile)", re.IGNORECASE
        ),
        "content": re.compile(
            r"(基本プロフィール|性格|口調|セリフ|台詞|外見|容姿|人物設定|backstory|personality)",
            re.IGNORECASE,
        ),
    },
    "world": {
        "filename": re.compile(
            r"(世界|world|設定|magic|魔法|地理|geography|歴史|history|国|政治)",
            re.IGNORECASE,
        ),
        "content": re.compile(
            r"(世界観|舞台|地理|大陸|王国|帝国|魔法体系|歴史年表|政治体制|文化)",
            re.IGNORECASE,
        ),
    },
    "scene": {
        "filename": re.compile(
            r"(シーン|scene|話|エピソード|episode|章|chapter|プロローグ|エピローグ)",
            re.IGNORECASE,
        ),
        "content": re.compile(
            r"(「[^」]+」|――|──|シーン\d|第\d+話|prologue|epilogue)", re.IGNORECASE
        ),
    },
    "reference": {
        "filename": re.compile(
            r"(参考|ref|分析|メモ|note|資料|research)", re.IGNORECASE
        ),
        "content": re.compile(r"(参考文献|出典|引用|分析結果|調査)", re.IGNORECASE),
    },
}

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".json"}


def _classify_file(filename: str, content_preview: str) -> str:
    """ファイル名と内容プレビューからカテゴリを推定する。"""
    # ファイル名ベースの判定（優先）
    for category, patterns in _CATEGORY_PATTERNS.items():
        if patterns["filename"].search(filename):
            return category

    # 内容ベースの判定
    for category, patterns in _CATEGORY_PATTERNS.items():
        if patterns["content"].search(content_preview):
            return category

    return "unknown"


_CATEGORY_LABELS = {
    "character": "キャラクター設定",
    "world": "世界設定",
    "scene": "シーン/エピソード",
    "reference": "参考資料",
    "unknown": "不明",
}


# ────────────────────────────────────────────
# ツール定義
# ────────────────────────────────────────────


@tool
async def analyze_import_files(directory_path: str) -> str:
    """ディレクトリ内のファイルを走査し、各ファイルの内容タイプを推定する。

    Args:
        directory_path: 走査するディレクトリのパス

    Returns:
        ディレクトリ構造分析のMarkdownテキスト
    """
    try:
        dir_path = Path(directory_path)
        if not dir_path.exists():
            return f"エラー: ディレクトリが見つかりません: {directory_path}"
        if not dir_path.is_dir():
            return (
                f"エラー: 指定されたパスはディレクトリではありません: {directory_path}"
            )

        # サポート対象ファイルを列挙
        files_info: List[Dict[str, Any]] = []
        for ext in _SUPPORTED_EXTENSIONS:
            for file_path in dir_path.rglob(f"*{ext}"):
                if file_path.is_file():
                    try:
                        stat = file_path.stat()
                        async with aiofiles.open(
                            str(file_path), "r", encoding="utf-8"
                        ) as f:
                            content_preview = await f.read(500)

                        category = _classify_file(file_path.name, content_preview)
                        files_info.append(
                            {
                                "path": str(file_path),
                                "name": file_path.name,
                                "size": stat.st_size,
                                "category": category,
                                "preview": content_preview[:100],
                            }
                        )
                    except UnicodeDecodeError:
                        # UTF-8以外のエンコーディングの場合
                        try:
                            async with aiofiles.open(
                                str(file_path), "r", encoding="shift_jis"
                            ) as f:
                                content_preview = await f.read(500)
                            category = _classify_file(file_path.name, content_preview)
                            files_info.append(
                                {
                                    "path": str(file_path),
                                    "name": file_path.name,
                                    "size": stat.st_size,
                                    "category": category,
                                    "preview": content_preview[:100],
                                }
                            )
                        except Exception:
                            files_info.append(
                                {
                                    "path": str(file_path),
                                    "name": file_path.name,
                                    "size": stat.st_size,
                                    "category": "unknown",
                                    "preview": "（読み取り不可）",
                                }
                            )
                    except Exception as e:
                        logger.warning("ファイル読み取りエラー: %s: %s", file_path, e)

        if not files_info:
            return f"ディレクトリ内にインポート可能なファイル（.md, .txt, .json）が見つかりません: {directory_path}"

        # カテゴリ別に分類
        by_category: Dict[str, List[Dict[str, Any]]] = {}
        for info in files_info:
            cat = info["category"]
            by_category.setdefault(cat, []).append(info)

        # レポート構築
        sections = [
            f"## ディレクトリ構造分析\nパス: {directory_path}\nファイル数: {len(files_info)}",
            "",
            "### 推定分類",
        ]

        for cat_key, label in _CATEGORY_LABELS.items():
            cat_files = by_category.get(cat_key, [])
            if cat_files:
                names = ", ".join(f["name"] for f in cat_files)
                sections.append(f"- {label}: {names}")

        sections.extend(["", "### 各ファイルの先頭プレビュー"])

        for info in files_info:
            char_count = info["size"]
            label = _CATEGORY_LABELS.get(info["category"], "不明")
            preview = info["preview"].replace("\n", " ")
            sections.append(
                f"[{info['name']}] ({char_count:,}バイト) [{label}]\n> {preview}..."
            )

        return "\n".join(sections)

    except Exception as e:
        logger.error("ディレクトリ分析エラー: %s", e)
        return f"ディレクトリ分析中にエラーが発生しました: {e}"


@tool
async def import_file_as_character(
    scenario_id: str, file_path: str, llm_extract: bool = True
) -> str:
    """ファイルからキャラクター設定を抽出してScenarioCharacterに追加する。

    Args:
        scenario_id: インポート先のシナリオID
        file_path: インポートするファイルのパス
        llm_extract: LLMを使って構造化データを抽出するかどうか（デフォルト: True）

    Returns:
        インポート結果のメッセージ
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return f"エラー: ファイルが見つかりません: {file_path}"

        # ファイル読み取り
        try:
            async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
                content = await f.read()
        except UnicodeDecodeError:
            async with aiofiles.open(str(path), "r", encoding="shift_jis") as f:
                content = await f.read()

        if not content.strip():
            return f"エラー: ファイルが空です: {file_path}"

        from ..services.scenario_service import add_scenario_character
        from ..services.screenplay_character_import import build_character_payloads

        if path.name == "キャラ倉庫.md":
            payloads = build_character_payloads(content)
            if not payloads:
                return f"エラー: キャラ倉庫からキャラクターを抽出できませんでした: {file_path}"

            imported_names = []
            for payload in payloads:
                result = await add_scenario_character(scenario_id, payload)
                imported_names.append(result.get("name", payload["name"]))

            return (
                f"キャラ倉庫から{len(imported_names)}件のキャラクターを"
                f"インポートしました: {', '.join(imported_names)}"
            )

        if llm_extract:
            # LLMで構造化データを抽出
            extract_prompt = """\
以下のテキストからキャラクター設定を抽出してJSON形式で返してください。
フィールドが不明な場合は空文字列を設定してください。

必ず以下の形式のJSONオブジェクトを返してください:
{
  "name": "キャラクター名",
  "role": "protagonist/antagonist/npc/companion のいずれか",
  "description": "キャラクターの概要・説明",
  "personality_summary": "性格の要約",
  "backstory": "経歴・背景",
  "psychology": "心理・動機",
  "speech_patterns": "口調パターンの説明",
  "example_dialogues": "会話例（複数行可）",
  "relationships": [{"target": "関連キャラ名", "type": "関係の種類", "description": "詳細"}],
  "character_arc": "成長軌道・変化の方向性"
}"""
            try:
                extracted_json = await _llm_extract(extract_prompt, content)
                extracted = json.loads(extracted_json)
            except Exception as e:
                logger.warning("LLM抽出に失敗、フォールバック: %s", e)
                extracted = {
                    "name": path.stem,
                    "description": content,
                }

            # ScenarioCharacterに変換
            name = extracted.get("name", "") or path.stem
            role_map = {
                "protagonist": "npc",
                "antagonist": "enemy",
                "companion": "ally",
                "npc": "npc",
            }
            raw_role = extracted.get("role", "npc")
            role = role_map.get(raw_role, "npc")

            # description に詳細情報をまとめる
            desc_parts = []
            if extracted.get("description"):
                desc_parts.append(extracted["description"])
            if extracted.get("personality_summary"):
                desc_parts.append(f"【性格】{extracted['personality_summary']}")
            if extracted.get("backstory"):
                desc_parts.append(f"【経歴】{extracted['backstory']}")
            if extracted.get("psychology"):
                desc_parts.append(f"【心理】{extracted['psychology']}")
            if extracted.get("character_arc"):
                desc_parts.append(f"【成長軌道】{extracted['character_arc']}")
            if extracted.get("relationships"):
                rel_lines = []
                for rel in extracted["relationships"]:
                    if isinstance(rel, dict):
                        rel_lines.append(
                            f"  - {rel.get('target', '?')}: "
                            f"{rel.get('type', '')} - {rel.get('description', '')}"
                        )
                if rel_lines:
                    desc_parts.append("【人間関係】\n" + "\n".join(rel_lines))

            description = "\n\n".join(desc_parts)

            # personality_override に口調・会話例を格納
            personality_parts = []
            if extracted.get("speech_patterns"):
                personality_parts.append(f"口調: {extracted['speech_patterns']}")
            if extracted.get("example_dialogues"):
                personality_parts.append(f"会話例:\n{extracted['example_dialogues']}")
            personality_override = "\n\n".join(personality_parts)

            char_data = {
                "name": name,
                "role": role,
                "description": description,
                "personality_override": personality_override,
            }
        else:
            # LLM不使用: ファイル全体をdescriptionに格納
            char_data = {
                "name": path.stem,
                "role": "npc",
                "description": content,
            }

        result = await add_scenario_character(scenario_id, char_data)
        char_name = result.get("name", "不明")
        return f"キャラクター「{char_name}」をシナリオにインポートしました。(ID: {result.get('id', '?')})"

    except Exception as e:
        logger.error("キャラクターインポートエラー: %s", e)
        return f"キャラクターインポート中にエラーが発生しました: {e}"


@tool
async def import_file_as_lore(
    scenario_id: str, file_path: str, category: str = "established"
) -> str:
    """ファイルから世界設定を抽出してCanonエントリに追加する。

    Args:
        scenario_id: インポート先のシナリオID
        file_path: インポートするファイルのパス
        category: デフォルトカテゴリ（geography, timeline, magic, character_facts, political, cultural, established）

    Returns:
        インポート結果のメッセージ
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return f"エラー: ファイルが見つかりません: {file_path}"

        # ファイル読み取り
        try:
            async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
                content = await f.read()
        except UnicodeDecodeError:
            async with aiofiles.open(str(path), "r", encoding="shift_jis") as f:
                content = await f.read()

        if not content.strip():
            return f"エラー: ファイルが空です: {file_path}"

        # LLMで確定事実を抽出
        extract_prompt = f"""\
以下のテキストから世界設定の確定事実を抽出してJSON形式で返してください。

必ず以下の形式で返してください:
{{
  "facts": [
    {{"category": "カテゴリ", "fact": "確定事実のテキスト"}}
  ]
}}

カテゴリは以下から選んでください:
- geography: 地理・地形・場所
- timeline: 時系列・歴史的事実
- magic: 魔法・超常現象の体系
- character_facts: キャラクターに関する確定事実
- political: 政治・国家・組織
- cultural: 文化・風習・宗教
- established: その他の確定事実

事実は具体的で、曖昧さのない文にしてください。
テキストが長い場合は重要な事実から抽出してください（最大30件）。"""

        try:
            extracted_json = await _llm_extract(extract_prompt, content)
            extracted = json.loads(extracted_json)
            facts = extracted.get("facts", [])
        except Exception as e:
            logger.warning("LLM抽出に失敗、段落単位でフォールバック: %s", e)
            # フォールバック: 段落ごとに1つのfactとして扱う
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            facts = [{"category": category, "fact": p} for p in paragraphs[:30]]

        if not facts:
            return f"ファイルから確定事実を抽出できませんでした: {file_path}"

        # Canon エントリに追加
        from ..memory.database import get_db_session
        from ..models.ecc_models import ScenarioCanonEntry

        added_count = 0
        async with await get_db_session() as session:
            for fact_data in facts:
                if not isinstance(fact_data, dict):
                    continue
                fact_text = fact_data.get("fact", "")
                if not fact_text:
                    continue

                entry = ScenarioCanonEntry(
                    id=uuid.uuid4(),
                    scenario_id=uuid.UUID(str(scenario_id)),
                    category=fact_data.get("category", category),
                    fact=fact_text,
                )
                session.add(entry)
                added_count += 1

            await session.commit()

        return (
            f"ファイル「{path.name}」から {added_count} 件の確定事実を"
            f"Canonにインポートしました。"
        )

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("世界設定インポートエラー: %s", e)
        return f"世界設定インポート中にエラーが発生しました: {e}"


@tool
async def import_file_as_scene(
    scenario_id: str, file_path: str, episode_id: Optional[str] = None
) -> str:
    """ファイルからシーンを作成してシナリオに追加する。

    Args:
        scenario_id: インポート先のシナリオID
        file_path: インポートするファイルのパス
        episode_id: 紐づけるエピソードID（省略可）

    Returns:
        インポート結果のメッセージ
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return f"エラー: ファイルが見つかりません: {file_path}"

        # ファイル読み取り
        try:
            async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
                content = await f.read()
        except UnicodeDecodeError:
            async with aiofiles.open(str(path), "r", encoding="shift_jis") as f:
                content = await f.read()

        if not content.strip():
            return f"エラー: ファイルが空です: {file_path}"

        # LLMでタイトルとシーンタイプを推定
        extract_prompt = """\
以下のテキストのタイトルとシーンタイプを推定してJSON形式で返してください。

必ず以下の形式で返してください:
{
  "title": "シーンのタイトル",
  "scene_type": "シーンタイプ",
  "description": "シーンの概要（1〜2文）"
}

scene_type は以下から選んでください:
- normal: 通常の場面
- combat: 戦闘シーン
- dialogue: 会話シーン
- cutscene: カットシーン（演出重視）

テキストの冒頭や見出しからタイトルを推定してください。
見出しがない場合は内容からふさわしいタイトルを付けてください。"""

        try:
            extracted_json = await _llm_extract(extract_prompt, content)
            extracted = json.loads(extracted_json)
            title = extracted.get("title", "") or path.stem
            scene_type = extracted.get("scene_type", "normal")
            description = extracted.get("description", "")
        except Exception as e:
            logger.warning("LLM抽出に失敗、ファイル名をタイトルに使用: %s", e)
            title = path.stem
            scene_type = "normal"
            description = ""

        # シーンタイプのバリデーション
        valid_types = {"normal", "combat", "dialogue", "cutscene"}
        if scene_type not in valid_types:
            scene_type = "normal"

        from ..services.scenario_service import add_scenario_scene

        # sort_orderを計算（既存シーンの末尾に追加）
        from ..services.scenario_service import get_scenario

        scenario_data = await get_scenario(scenario_id, include_children=True)
        existing_scenes = scenario_data.get("scenes", [])
        max_order = max((s.get("sort_order", 0) for s in existing_scenes), default=-1)

        scene_data: Dict[str, Any] = {
            "title": title,
            "description": description,
            "scene_type": scene_type,
            "sort_order": max_order + 1,
        }

        result = await add_scenario_scene(scenario_id, scene_data)
        scene_id = result.get("id", "?")

        # シーンのcontentを保存
        try:
            from ..services.scenario_service import save_scene_content

            await save_scene_content(
                scene_id=scene_id,
                content=content,
                create_version=False,
            )
        except ImportError:
            # scenario_serviceの保存ヘルパーが使えない場合、直接DBに保存
            try:
                from ..memory.database import get_db_session
                from ..models.ecc_models import ScenarioScene

                async with await get_db_session() as session:
                    scene = await session.get(ScenarioScene, uuid.UUID(str(scene_id)))
                    if scene and hasattr(scene, "content"):
                        scene.content = content
                        await session.commit()
            except Exception as e:
                logger.warning("シーンcontent保存フォールバックも失敗: %s", e)

        char_count = len(content)
        return (
            f"シーン「{title}」（{scene_type}）をシナリオにインポートしました。"
            f"（{char_count:,}文字、ID: {scene_id}）"
        )

    except Exception as e:
        logger.error("シーンインポートエラー: %s", e)
        return f"シーンインポート中にエラーが発生しました: {e}"
