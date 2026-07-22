"""統合キャラクター管理サービス

キャラクターYAMLとカスタムエージェントを統合した
統一キャラクターモデルのCRUD操作を提供する。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.database import get_db_session
from ..models.ecc_models import Character, Scenario, ScenarioCharacter
from ..tools.core import ToolDefinition, ToolParam
from ..utils.uuid_utils import parse_uuid, parse_uuid_strict

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
# 例外
# ────────────────────────────────────────────


class CharacterError(Exception):
    """キャラクター操作のドメインエラー"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CharacterNotFoundError(CharacterError):
    """指定されたキャラクターが見つからない"""

    def __init__(self, identifier: str):
        super().__init__(
            f"キャラクターが見つかりません: {identifier}",
            status_code=404,
        )


# ────────────────────────────────────────────
# バリデーション
# ────────────────────────────────────────────

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,98}[a-z0-9]$")
_REQUIRED_FIELDS = ("name", "slug")
_VALID_TYPES = {"assistant", "roleplay", "trpg_npc", "gm"}


def _validate_slug(slug: str) -> None:
    if not slug or not _SLUG_PATTERN.match(slug):
        raise CharacterError(
            "slugは小文字英数字とアンダースコアのみ使用可能です "
            "(3〜100文字、先頭は英字): " + repr(slug)
        )


def _validate_create_data(data: dict) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise CharacterError(f"必須項目が不足しています: {', '.join(missing)}")
    _validate_slug(data["slug"])
    ctype = data.get("character_type", "assistant")
    if ctype not in _VALID_TYPES:
        raise CharacterError(f"無効なキャラクタータイプ: {ctype}")


# ────────────────────────────────────────────
# CRUD
# ────────────────────────────────────────────

_UPDATABLE_FIELDS = {
    "name",
    "slug",
    "character_type",
    "system_prompt",
    "model",
    "allowed_tools",
    "is_enabled",
    # 音声
    "voice_engine",
    "voice_name",
    "voice_id",
    "speaker_id",
    "voice_parameters",
    # 性格
    "greeting",
    "invalid_content_reply",
    "fallback_reply",
    "goodbye_reply",
    "recognition_aliases",
    # ロールプレイ
    "description",
    "personality_summary",
    "first_message",
    "alternate_greetings",
    "example_messages",
    "scenario",
    # RP画像自動生成
    "auto_image_gen",
    "image_gen_trigger",
    "image_gen_interval",
    # 外見・画像生成
    "appearance_tags",
    "negative_tags",
    "image_gen_engine",
    "comfyui_config",
    "avatar_image_path",
}


async def create_character(data: dict) -> dict:
    """キャラクターを新規作成する。"""
    _validate_create_data(data)

    async with await get_db_session() as session:
        existing = (
            await session.execute(
                select(Character).where(Character.slug == data["slug"])
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CharacterError(
                f"slug '{data['slug']}' は既に使用されています",
                status_code=409,
            )

        char = Character(id=uuid.uuid4())
        for key in _UPDATABLE_FIELDS:
            if key in data:
                setattr(char, key, data[key])
        # 必須フィールド
        char.name = data["name"]
        char.slug = data["slug"]

        session.add(char)
        await session.commit()
        await session.refresh(char)

        logger.info("キャラクターを作成しました: %s (%s)", char.name, char.id)
        return char.to_dict()


async def get_character(identifier: str) -> dict:
    """IDまたはslugでキャラクターを取得する。"""
    async with await get_db_session() as session:
        # まずUUIDとして試行
        uid = parse_uuid(identifier)
        if uid:
            char = await session.get(Character, uid)
        else:
            # slugとして検索
            char = (
                await session.execute(
                    select(Character).where(Character.slug == identifier)
                )
            ).scalar_one_or_none()

        if char is None:
            raise CharacterNotFoundError(identifier)
        return char.to_dict()


async def list_characters(
    type_filter: Optional[str] = None,
    enabled_only: bool = False,
) -> list:
    """キャラクター一覧を取得する。"""
    async with await get_db_session() as session:
        stmt = select(Character).order_by(Character.name)
        if type_filter:
            stmt = stmt.where(Character.character_type == type_filter)
        if enabled_only:
            stmt = stmt.where(Character.is_enabled.is_(True))

        result = await session.execute(stmt)
        chars = result.scalars().all()
        return [c.to_dict() for c in chars]


async def update_character(character_id: str, data: dict) -> dict:
    """キャラクターを更新する。"""
    uid = parse_uuid_strict(character_id, lambda v: CharacterError(f"無効なUUID形式です: {v}"))

    if "slug" in data:
        _validate_slug(data["slug"])
    if "character_type" in data and data["character_type"] not in _VALID_TYPES:
        raise CharacterError(f"無効なキャラクタータイプ: {data['character_type']}")

    async with await get_db_session() as session:
        char = await session.get(Character, uid)
        if char is None:
            raise CharacterNotFoundError(character_id)

        # slug変更時の重複チェック
        new_slug = data.get("slug")
        if new_slug and new_slug != char.slug:
            dup = (
                await session.execute(
                    select(Character).where(Character.slug == new_slug)
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise CharacterError(
                    f"slug '{new_slug}' は既に使用されています",
                    status_code=409,
                )

        for key in _UPDATABLE_FIELDS:
            if key in data:
                setattr(char, key, data[key])

        char.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(char)

        logger.info("キャラクターを更新しました: %s (%s)", char.name, char.id)
        return char.to_dict()


async def delete_character(character_id: str) -> bool:
    """キャラクターを削除する。"""
    uid = parse_uuid_strict(character_id, lambda v: CharacterError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        char = await session.get(Character, uid)
        if char is None:
            raise CharacterNotFoundError(character_id)

        char_name = char.name
        await session.execute(sa_delete(Character).where(Character.id == uid))
        await session.commit()

        logger.info("キャラクターを削除しました: %s (%s)", char_name, uid)
        return True


async def get_character_for_prompt(slug: str) -> dict:
    """LLMプロンプト構築用にキャラクター情報を取得する。

    character_manager や prompts.py から呼ばれる。
    見つからない場合はNoneを返す（例外ではなく）。
    """
    scenario_roleplay = await _get_scenario_roleplay_character(slug)
    if scenario_roleplay is not None:
        return scenario_roleplay

    async with await get_db_session() as session:
        char = (
            await session.execute(
                select(Character).where(
                    Character.slug == slug,
                    Character.is_enabled.is_(True),
                )
            )
        ).scalar_one_or_none()

        if char is None:
            # recognition_aliases で検索
            result = await session.execute(
                select(Character).where(Character.is_enabled.is_(True))
            )
            requested = str(slug or "").casefold()
            for c in result.scalars().all():
                aliases = c.recognition_aliases or []
                normalized_aliases = {
                    str(alias).casefold() for alias in aliases if alias is not None
                }
                if requested in normalized_aliases or str(c.name).casefold() == requested:
                    return c.to_dict()
            return None

        return char.to_dict()


async def _get_scenario_roleplay_character(slug: str) -> Optional[dict]:
    """scenario_roleplay:<scenario_id>:<character_id> を動的RPキャラとして解決する。"""
    prefix = "scenario_roleplay:"
    if not slug.startswith(prefix):
        return None

    parts = slug.split(":")
    if len(parts) != 3:
        return None

    scenario_uid = parse_uuid(parts[1])
    character_uid = parse_uuid(parts[2])
    if not scenario_uid or not character_uid:
        return None

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        scenario_char = await session.get(ScenarioCharacter, character_uid)
        if (
            scenario is None
            or scenario_char is None
            or scenario_char.scenario_id != scenario_uid
        ):
            return None

        linked_char = None
        if scenario_char.character_id:
            linked_char = await session.get(Character, scenario_char.character_id)

        base = linked_char.to_dict() if linked_char is not None else {}
        description_parts = [
            scenario_char.description or base.get("description", ""),
            scenario_char.backstory,
            scenario_char.psychology,
        ]
        personality_parts = [
            base.get("personality_summary", ""),
            scenario_char.personality_override,
        ]
        scenario_parts = [
            f"シナリオ名: {scenario.title}",
            f"概要: {scenario.description}" if scenario.description else "",
            f"舞台設定: {scenario.setting}" if scenario.setting else "",
            f"開始状況: {scenario.opening_text}" if scenario.opening_text else "",
        ]

        return {
            **base,
            "id": str(scenario_char.id),
            "slug": slug,
            "name": scenario_char.name or base.get("name", ""),
            "character_type": "roleplay",
            "description": "\n".join(p for p in description_parts if p),
            "personality_summary": "\n".join(p for p in personality_parts if p),
            "scenario": "\n".join(p for p in scenario_parts if p),
            "example_messages": scenario_char.example_dialogues
            or base.get("example_messages", ""),
            "system_prompt": base.get("system_prompt", ""),
            "first_message": base.get("first_message", ""),
            "alternate_greetings": base.get("alternate_greetings", []),
            "auto_image_gen": base.get("auto_image_gen", False),
        }


# ────────────────────────────────────────────
# ランタイムツール構築
# ────────────────────────────────────────────


def build_character_agent_tools(config: Any) -> List[ToolDefinition]:
    """有効なキャラクター（エージェント型）をランタイムツールとして構築する。

    character_type が "assistant" 以外で allowed_tools が設定されている
    キャラクターをツールとして登録する。
    """
    try:
        characters = _run_sync(list_characters(enabled_only=True))
    except Exception:
        logger.warning(
            "キャラクター一覧の取得に失敗しました。ツール登録をスキップします",
            exc_info=True,
        )
        return []

    tools: List[ToolDefinition] = []
    for char_data in characters:
        # system_prompt があるキャラクターのみツールとして登録
        if not char_data.get("system_prompt"):
            continue
        # allowed_tools が空でないキャラクターのみ
        if not char_data.get("allowed_tools"):
            continue

        try:
            td = _build_single_character_tool(char_data, config)
            tools.append(td)
            logger.debug(
                "キャラクターツールを登録: %s (%s)",
                char_data["slug"],
                char_data["id"],
            )
        except Exception:
            logger.warning(
                "キャラクターツールの構築に失敗: %s",
                char_data.get("slug", "unknown"),
                exc_info=True,
            )

    if tools:
        logger.info("キャラクターツールを %d 件登録しました", len(tools))

    return tools


def _build_single_character_tool(char_data: dict, config: Any) -> ToolDefinition:
    """1件のキャラクターを ToolDefinition に変換する。"""
    from ..llm.specialist_delegate import SpecialistDelegationRunner
    from ..services.project_context import get_runtime_project_context

    slug = char_data["slug"]
    name = char_data["name"]
    description = f"{name} キャラクターエージェント"
    system_prompt = char_data["system_prompt"]
    model = char_data.get("model") or None

    agent_class = _make_dynamic_agent_class(
        agent_name=slug,
        system_prompt=system_prompt,
        model=model,
    )

    runner = SpecialistDelegationRunner(
        config,
        domain_key=f"character_{slug}",
        display_name=name,
        agent_class=agent_class,
        model=model,
    )

    def _delegate(request: str) -> str:
        return runner.run(request, project_context=get_runtime_project_context())

    _delegate.__name__ = slug
    _delegate.__doc__ = description

    return ToolDefinition(
        name=slug,
        description=description,
        function=_delegate,
        parameters=[
            ToolParam(
                name="request",
                type="string",
                description=f"{name} への依頼内容",
                required=True,
            ),
        ],
    )


def _make_dynamic_agent_class(
    *,
    agent_name: str,
    system_prompt: str,
    model: Optional[str] = None,
) -> type:
    from ..llm.native_runtime import AgentDefinition

    class DynamicCharacterAgent:
        def __init__(self, model: Optional[str] = None):
            effective_model = model or "gpt-4o-mini"
            self.agent = AgentDefinition(
                name=agent_name,
                instructions=system_prompt,
                model=effective_model,
            )

    DynamicCharacterAgent.__name__ = f"CharacterAgent_{agent_name}"
    DynamicCharacterAgent.__qualname__ = f"CharacterAgent_{agent_name}"

    return DynamicCharacterAgent


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────


def _run_sync(coro):
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
