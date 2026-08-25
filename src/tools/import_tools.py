"""シナリオ素材インポートツール

ディレクトリ/ファイルからキャラクター設定・世界設定・シーンを
AoiTalkのシナリオデータベースに取り込むためのツールを提供する。
"""

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import UUID

import aiofiles
from sqlalchemy import select

from .core import tool
from ..memory.database import get_db_session
from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryNote,
    StoryWork,
    StoryWorkCharacter,
)
from ..services.story_studio import StoryEpisodeService
from ..llm.conversation_context import normalize_usage, persist_usage_sync
from ..services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from ..services.turn_context import get_turn_context

logger = logging.getLogger(__name__)


def persist_usage_sync(*args, **kwargs):
    """Lazy usage persistence avoids importing database services eagerly."""

    from ..llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

_RECORDED_IMPORT_RESPONSES: list[object] = []


def _usage_client(context=None):
    """Return a persist_usage_sync-compatible context for import tools."""

    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context
    try:
        from ..services.turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None

    def _value(name, default=None):
        if isinstance(context, Mapping):
            value = context.get(name)
            if value is not None:
                return value
        value = getattr(context, name, None)
        if value is not None:
            return value
        return getattr(turn, name, default) if turn is not None else default

    user_id = _value("user_id")
    return SimpleNamespace(
        current_session_id=_value("current_session_id", _value("session_id")),
        current_project_id=_value("current_project_id", _value("project_id")),
        character_name=_value("character_name"),
        _get_session_user_id=lambda: user_id,
    )


def _mark_response_recorded(response: object) -> bool:
    try:
        if getattr(response, "_aoitalk_usage_recorded", False):
            return True
        object.__setattr__(response, "_aoitalk_usage_recorded", True)
        return False
    except Exception:
        if any(item is response for item in _RECORDED_IMPORT_RESPONSES):
            return True
        _RECORDED_IMPORT_RESPONSES.append(response)
        del _RECORDED_IMPORT_RESPONSES[:-16]
        return False


def _record_import_usage(
    response,
    *,
    usage_context=None,
    model: str | None = None,
    started: float | None = None,
) -> bool:
    raw_usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
    if raw_usage is None:
        return False
    usage = normalize_usage(
        raw_usage,
        provider="openai",
        resolved_model=(
            response.get("model") if isinstance(response, Mapping) else getattr(response, "model", None)
        ),
    )
    if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
        return False
    if _mark_response_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(usage_context),
            provider="openai",
            model=str(model or "gpt-5.6-luna"),
            usage=usage,
            request_type="import",
            latency_ms=(
                max(0, int((time.monotonic() - started) * 1000))
                if started is not None
                else 0
            ),
            is_streaming=False,
        )
        return True
    except Exception:
        logger.debug("インポートLLMのusage記録に失敗しました", exc_info=True)
        return False

# ────────────────────────────────────────────
# LLM抽出ヘルパー
# ────────────────────────────────────────────


async def _llm_extract(prompt: str, text: str, usage_context=None) -> str:
    """LLMを使ってテキストから構造化データを抽出する。

    OpenAI APIを直接使用（抽出用なので軽量モデル）。
    """
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が設定されていません")

    client = AsyncOpenAI(api_key=api_key)
    started = time.monotonic()
    inherited = get_privacy_policy_context()

    def _value(name: str, *aliases: str):
        keys = (name, *aliases)
        if isinstance(usage_context, Mapping):
            for key in keys:
                value = usage_context.get(key)
                if value is not None:
                    return value
        for key in keys:
            value = getattr(usage_context, key, None)
            if value is not None:
                return value
        return None

    privacy_config = _value("config", "privacy_config")
    if privacy_config is None:
        try:
            from ..config import Config

            privacy_config = Config()
        except Exception as exc:
            raise PrivacyError(
                "インポート抽出のプライバシー設定を解決できないため生成を停止しました"
            ) from exc
    try:
        turn = get_turn_context()
    except Exception:
        turn = None
    session_context = _value("session_context", "privacy_context")
    project_metadata = _value("project_metadata", "project_context")
    if not isinstance(session_context, Mapping):
        session_context = inherited.session_context
    if not isinstance(project_metadata, Mapping):
        project_metadata = inherited.project_metadata
    user_id = _value("user_id", "session_user_id") or getattr(turn, "user_id", None)
    session_id = _value("current_session_id", "session_id") or getattr(
        turn, "session_id", None
    )
    gateway = OutboundPrivacyGateway(
        privacy_config,
        user_id=str(user_id or ""),
        session_id=str(session_id or ""),
        session_context=session_context if isinstance(session_context, Mapping) else None,
        project_metadata=project_metadata if isinstance(project_metadata, Mapping) else None,
    )
    def _config_value(key: str, default: Any = None) -> Any:
        getter = getattr(privacy_config, "get", None)
        if callable(getter):
            try:
                value = getter(key, default)
            except TypeError:
                value = getter(key)
            if value is not None:
                return value
        if isinstance(privacy_config, Mapping):
            current: Any = privacy_config
            for part in key.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    return default
                current = current[part]
            return current
        return default

    model = str(
        _config_value("llm_model", "")
        or _config_value("openai.model", "")
        or "gpt-5.6-luna"
    ).strip()
    model_leaf = model.lower().rsplit("/", 1)[-1]
    effort = str(_config_value("openai.reasoning_effort", "") or "").strip().lower()
    if not effort and model_leaf.startswith("gpt-5.6-luna"):
        effort = "max"
    try:
        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        if effort not in reasoning_effort_options_for_model("openai", model):
            effort = ""
    except Exception:
        effort = ""

    try:
        request_kwargs = {
            "model": model,
            "instructions": prompt,
            "input": text[:8000],
            "text": {"format": {"type": "json_object"}},
        }
        if effort:
            # Reasoning models reject the legacy temperature control; carry
            # the configured effort through the Responses payload instead.
            request_kwargs["reasoning"] = {"effort": effort, "summary": "auto"}
        else:
            request_kwargs["temperature"] = 0.1
        protected = await gateway.protect(
            request_kwargs,
            provider="openai",
            base_url=str(getattr(client, "base_url", "") or ""),
            source_kind="import_model_request",
        )
        response = await client.responses.create(
            **protected.payload
        )
        _record_import_usage(
            response,
            usage_context=usage_context,
            model=model,
            started=started,
        )
        return gateway.restore(getattr(response, "output_text", "") or "")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass


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


async def _save_story_characters(work_id: str, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """抽出済み payload を Story の共有キャラ + 作品参加へ保存する。"""

    async with await get_db_session() as session:
        work = await session.get(StoryWork, UUID(str(work_id)))
        if work is None:
            raise ValueError("作品が見つかりません")
        result: list[dict[str, Any]] = []
        for index, payload in enumerate(payloads):
            name = str(payload.get("name") or "").strip()
            if not name:
                continue
            character = StoryCharacter(
                user_id=work.user_id,
                name=name,
                aliases=list(payload.get("aliases") or []),
                summary=payload.get("summary") or payload.get("personality_summary"),
                description=payload.get("description"),
                notes=payload.get("notes"),
                ai_mode=payload.get("ai_mode") or "keyword",
                keywords=list(payload.get("keywords") or []),
            )
            session.add(character)
            await session.flush()
            role = payload.get("role")
            role_note = str(role) if role else None
            session.add(
                StoryWorkCharacter(
                    work_id=work.id,
                    character_id=character.id,
                    role_note=role_note,
                    position=float(payload.get("sort_order", index) or index),
                )
            )
            result.append(character.to_dict())
        await session.commit()
        return result


@tool
async def import_file_as_character(
    work_id: str, file_path: str, llm_extract: bool = True
) -> str:
    """ファイルからキャラクター設定を抽出して StoryCharacter に追加する。

    Args:
        work_id: インポート先の StoryWork ID
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

        from ..services.screenplay_character_import import build_character_payloads

        if path.name == "キャラ倉庫.md":
            payloads = build_character_payloads(content)
            if not payloads:
                return f"エラー: キャラ倉庫からキャラクターを抽出できませんでした: {file_path}"

            saved = await _save_story_characters(work_id, payloads)
            imported_names = [item.get("name", "") for item in saved]

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

            # StoryCharacter に変換
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

        saved = await _save_story_characters(work_id, [char_data])
        if not saved:
            return "エラー: キャラクター名が空です"
        char_name = saved[0].get("name", "不明")
        return f"キャラクター「{char_name}」を作品にインポートしました。(ID: {saved[0].get('id', '?')})"

    except Exception as e:
        logger.error("キャラクターインポートエラー: %s", e)
        return f"キャラクターインポート中にエラーが発生しました: {e}"


@tool
async def import_file_as_lore(
    work_id: str, file_path: str, category: str = "established"
) -> str:
    """ファイルから世界設定を抽出して StoryNote に追加する。

    Args:
        work_id: インポート先の StoryWork ID
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

        added_count = 0
        async with await get_db_session() as session:
            work = await session.get(StoryWork, UUID(str(work_id)))
            if work is None:
                return "エラー: 作品が見つかりません"
            for fact_data in facts:
                if not isinstance(fact_data, dict):
                    continue
                fact_text = fact_data.get("fact", "")
                if not fact_text:
                    continue

                entry = StoryNote(
                    work_id=work.id,
                    title=str(fact_data.get("category", category) or category),
                    content=fact_text,
                    ai_mode="always",
                    keywords=[],
                    position=float(added_count),
                )
                session.add(entry)
                added_count += 1

            await session.commit()

        return (
            f"ファイル「{path.name}」から {added_count} 件の確定事実を"
            f"StoryNote にインポートしました。"
        )

    except ImportError as e:
        logger.error("モジュールインポートエラー（並行実装中の可能性）: %s", e)
        return f"エラー: 必要なモジュールがまだ利用できません: {e}"
    except Exception as e:
        logger.error("世界設定インポートエラー: %s", e)
        return f"世界設定インポート中にエラーが発生しました: {e}"


@tool
async def import_file_as_scene(
    work_id: str, file_path: str, episode_id: Optional[str] = None
) -> str:
    """ファイルから StoryEpisode を作成して作品に追加する。

    Args:
        work_id: インポート先の StoryWork ID
        file_path: インポートするファイルのパス
        episode_id: 続き元エピソードID（省略可）

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

        async with await get_db_session() as session:
            work = await session.get(StoryWork, UUID(str(work_id)))
            if work is None:
                return "エラー: 作品が見つかりません"
            episodes = list((await session.scalars(
                select(StoryEpisode).where(StoryEpisode.work_id == work.id)
            )).all())
            sort_hint = max((float(item.sort_hint or 0) for item in episodes), default=-1) + 1
            episode = await StoryEpisodeService(session).create(
                work,
                {
                    "title": title,
                    "plot": description,
                    "body": content,
                    "status": "draft",
                    "sort_hint": sort_hint,
                },
                after_episode_id=UUID(str(episode_id)) if episode_id else None,
            )
            await session.commit()
            episode_id_result = str(episode.id)

        char_count = len(content)
        return (
            f"エピソード「{title}」を作品にインポートしました。"
            f"（{char_count:,}文字、ID: {episode_id_result}）"
        )

    except Exception as e:
        logger.error("シーンインポートエラー: %s", e)
        return f"シーンインポート中にエラーが発生しました: {e}"
