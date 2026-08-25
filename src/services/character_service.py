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
from ..models.ecc_models import Character
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
# A Character row has two independent concerns:
#
# * ``character_type`` selects the execution surface (ordinary assistant vs
#   Story/Roleplay/TRPG persona).
# * ``allowed_tools`` describes the tools available *inside* that Character's
#   own execution.
#
# The latter must never become an allow-list for publishing Character bridges
# into the ordinary Main runtime.  Only assistant Characters are eligible for
# that public root bridge; Roleplay/TRPG/GM rows remain available to their
# dedicated direct/group/story routes.
_ROOT_AGENT_CHARACTER_TYPES = frozenset({"assistant"})
_CHARACTER_SLUG_ALIASES = {
    "project_management_assistant": "project_manager",
}


def canonicalize_character_slug(slug: str) -> str:
    value = str(slug or "").strip()
    return _CHARACTER_SLUG_ALIASES.get(value, value)


def character_slug_lookup_candidates(slug: str) -> list[str]:
    """同一キャラクターを指すslugをcanonical優先で返す。"""
    canonical = canonicalize_character_slug(slug)
    candidates = [canonical]
    candidates.extend(
        legacy
        for legacy, target in _CHARACTER_SLUG_ALIASES.items()
        if target == canonical and legacy not in candidates
    )
    return candidates


def _validate_slug(slug: str) -> None:
    if not slug or not _SLUG_PATTERN.match(slug):
        raise CharacterError(
            "slugは小文字英数字とアンダースコアのみ使用可能です "
            "(3〜100文字、先頭は英字): " + repr(slug)
        )


_VALID_IMAGE_GEN_ENGINES = {"", "comfyui"}


def _validate_image_gen_engine(engine: Any) -> None:
    normalized = str(engine or "").strip().lower()
    if normalized == "gemini":
        raise CharacterError(
            "image_gen_engine=gemini はサポートされていません",
            status_code=422,
        )
    if normalized not in _VALID_IMAGE_GEN_ENGINES:
        raise CharacterError(
            f"未対応の image_gen_engine です: {engine}",
            status_code=422,
        )


def _validate_create_data(data: dict) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise CharacterError(f"必須項目が不足しています: {', '.join(missing)}")
    _validate_slug(data["slug"])
    ctype = data.get("character_type", "assistant")
    if ctype not in _VALID_TYPES:
        raise CharacterError(f"無効なキャラクタータイプ: {ctype}")
    if "image_gen_engine" in data:
        _validate_image_gen_engine(data.get("image_gen_engine"))


# ────────────────────────────────────────────
# 旧ツール名の正規化
# ────────────────────────────────────────────

# 統合前のツール名で保存済みの allowed_tools を救済するための変換表。
# コード側にエイリアスは残さず、DBに残った旧名だけをロード/保存時に寄せる。
_LEGACY_TOOL_NAME_MAP: Dict[str, str] = {
    "read_workspace_file": "read_file",
    "view_file": "read_file",
    "list_workspace_files": "list_directory",
    "inspect_workspace_tree": "list_directory",
    "find_workspace_items": "search_files",
    "search_memory": "search_past_chats",
    "search_chat_messages": "search_past_chats",
    # Spotify moved from a nested specialist Agent to the Shared Integration
    # capability group.  Preserve old Character settings at the persistence
    # boundary without exposing the assistant/LLM bridge at runtime.
    "spotify_assistant": "spotify",
}


def normalize_allowed_tools(values: Any) -> List[str]:
    """allowed_tools の旧ツール名を統合後の名前へ寄せ、重複を畳む。"""
    if not isinstance(values, (list, tuple, set)):
        return []

    normalized: List[str] = []
    for raw in values:
        name = str(raw or "").strip()
        if not name:
            continue
        name = _LEGACY_TOOL_NAME_MAP.get(name, name)
        if name not in normalized:
            normalized.append(name)
    return normalized


def _with_normalized_tools(data: Optional[dict]) -> Optional[dict]:
    """キャラクター辞書の allowed_tools を正規化して返す。"""
    if not isinstance(data, dict) or "allowed_tools" not in data:
        return data
    data["allowed_tools"] = normalize_allowed_tools(data.get("allowed_tools"))
    return data


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
    data = {**data, "slug": canonicalize_character_slug(data.get("slug", ""))}
    _validate_create_data(data)

    async with await get_db_session() as session:
        existing = (
            await session.execute(
                select(Character).where(
                    Character.slug.in_(character_slug_lookup_candidates(data["slug"]))
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CharacterError(
                f"slug '{data['slug']}' は既に使用されています",
                status_code=409,
            )

        if "allowed_tools" in data:
            data = {**data, "allowed_tools": normalize_allowed_tools(data["allowed_tools"])}

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
        return _with_normalized_tools(char.to_dict())


async def get_character(identifier: str) -> dict:
    """IDまたはslugでキャラクターを取得する。"""
    async with await get_db_session() as session:
        # まずUUIDとして試行
        uid = parse_uuid(identifier)
        if uid:
            char = await session.get(Character, uid)
        else:
            # canonical slugを優先しつつ、移行前DBの旧slugにもフォールバックする。
            char = None
            for lookup_identifier in character_slug_lookup_candidates(identifier):
                char = (
                    await session.execute(
                        select(Character).where(Character.slug == lookup_identifier)
                    )
                ).scalar_one_or_none()
                if char is not None:
                    break

        if char is None:
            raise CharacterNotFoundError(identifier)
        return _with_normalized_tools(char.to_dict())


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
        return [_with_normalized_tools(c.to_dict()) for c in chars]


async def update_character(character_id: str, data: dict) -> dict:
    """キャラクターを更新する。"""
    uid = parse_uuid_strict(character_id, lambda v: CharacterError(f"無効なUUID形式です: {v}"))

    if "slug" in data:
        data = {**data, "slug": canonicalize_character_slug(data["slug"])}
        _validate_slug(data["slug"])
    if "character_type" in data and data["character_type"] not in _VALID_TYPES:
        raise CharacterError(f"無効なキャラクタータイプ: {data['character_type']}")
    if "image_gen_engine" in data:
        _validate_image_gen_engine(data.get("image_gen_engine"))

    async with await get_db_session() as session:
        char = await session.get(Character, uid)
        if char is None:
            raise CharacterNotFoundError(character_id)

        # slug変更時の重複チェック
        new_slug = data.get("slug")
        if new_slug and new_slug != char.slug:
            dup = (
                await session.execute(
                    select(Character).where(
                        Character.slug.in_(character_slug_lookup_candidates(new_slug)),
                        Character.id != uid,
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise CharacterError(
                    f"slug '{new_slug}' は既に使用されています",
                    status_code=409,
                )

        if "allowed_tools" in data:
            data = {**data, "allowed_tools": normalize_allowed_tools(data["allowed_tools"])}

        for key in _UPDATABLE_FIELDS:
            if key in data:
                setattr(char, key, data[key])

        char.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(char)

        logger.info("キャラクターを更新しました: %s (%s)", char.name, char.id)
        return _with_normalized_tools(char.to_dict())


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
    lookup_slugs = character_slug_lookup_candidates(slug)
    async with await get_db_session() as session:
        char = None
        for lookup_slug in lookup_slugs:
            char = (
                await session.execute(
                    select(Character).where(
                        Character.slug == lookup_slug,
                        Character.is_enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if char is not None:
                break

        if char is None:
            # recognition_aliases で検索
            result = await session.execute(
                select(Character).where(Character.is_enabled.is_(True))
            )
            requested_values = {value.casefold() for value in lookup_slugs if value}
            for c in result.scalars().all():
                aliases = c.recognition_aliases or []
                normalized_aliases = {
                    str(alias).casefold() for alias in aliases if alias is not None
                }
                if requested_values.intersection(normalized_aliases) or str(
                    c.name
                ).casefold() in requested_values:
                    return _with_normalized_tools(c.to_dict())
            return None

        return _with_normalized_tools(char.to_dict())


# ────────────────────────────────────────────
# ランタイムツール構築
# ────────────────────────────────────────────


def build_character_agent_tools(config: Any) -> List[ToolDefinition]:
    """有効なキャラクター（エージェント型）をランタイムツールとして構築する。

    通常のMain runtimeへ公開するのは ``assistant`` Characterだけに限定
    する。Roleplay/TRPG/GM Characterは、直接Character chat、Group chat、
    Story/TRPG経路から同じCharacter DBを参照するため、ここで除外しても
    それらの専用実行経路には影響しない。

    ``allowed_tools`` はCharacter自身の実行時ツール設定であり、公開可否
    の条件には使わない。従来の実装はこの値が空の行を黙って落としていた
    ため、公開境界と内部権限が混同されていた。

    専用経路が必要な場合もこの公開ブリッジを拡張せず、直接Character
    chat/Group/Story/TRPG側の実行経路からCharacter DBを参照する。
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
        # Missing/blank/unknown types are not legacy assistant aliases here:
        # the root publication boundary is fail-closed.  Persisted rows with
        # an explicit ``assistant`` type remain eligible, while Roleplay/TRPG/
        # GM (and malformed rows) stay on their dedicated routes only.
        character_type = str(char_data.get("character_type") or "").strip().lower()
        if character_type not in _ROOT_AGENT_CHARACTER_TYPES:
            # Roleplay/TRPG/GM Character rows are intentionally not root tools;
            # their direct/group/story routes still call get_character_for_prompt.
            continue
        # system_prompt があるキャラクターのみツールとして登録
        if not char_data.get("system_prompt"):
            continue

        try:
            td = _build_single_character_tool(char_data, config)
            # Keep the type available to any later runtime registration seam as
            # additive availability metadata.  The field is not consulted by
            # the Character's internal tool policy and does not affect schema
            # generation for existing callers.
            if isinstance(td.availability, dict):
                td.availability = {
                    **td.availability,
                    "character_type": character_type,
                }
            else:
                td.availability = {"character_type": character_type}
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
    model_value = char_data.get("model")
    model = str(model_value).strip() if model_value is not None else ""
    model = model or None

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
            # SpecialistDelegationRunner supplies the resolved route model for
            # normal execution.  Keep this defensive fallback aligned with the
            # current fresh OpenAI default rather than reviving the retired
            # gpt-4o-mini value if a caller instantiates the dynamic class
            # directly.
            effective_model = model or "gpt-5.6-luna"
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
