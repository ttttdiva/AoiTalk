"""シナリオ管理サービス

TRPGシナリオのCRUD操作、キャラクター・シーン管理、
プレイセッションの開始・管理を提供する。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete as sa_delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_db_session
from ..memory.models import ConversationMessage, ConversationSession
from ..models.ecc_models import (
    Scenario,
    ScenarioCharacter,
    ScenarioScene,
    ScenarioPlaySession,
    ScenarioEpisode,
    ScenarioCanonEntry,
    ScenarioWritingSession,
    TRPGScenarioDocument,
)
from .trpg_coc import normalize_coc_state
from .trpg_rules import COC6_RULESET_TAG, COC7_RULESET_TAG, COC_RULESET_TAG
from ..utils.uuid_utils import parse_uuid, parse_uuid_strict

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────
# 例外
# ────────────────────────────────────────────


class ScenarioError(Exception):
    """シナリオ操作のドメインエラー"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ScenarioNotFoundError(ScenarioError):
    """指定されたシナリオが見つからない"""

    def __init__(self, identifier: str):
        super().__init__(
            f"シナリオが見つかりません: {identifier}",
            status_code=404,
        )


class ScenarioCharacterNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"シナリオキャラクターが見つかりません: {identifier}",
            status_code=404,
        )


class ScenarioSceneNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"シナリオシーンが見つかりません: {identifier}",
            status_code=404,
        )


class PlaySessionNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"プレイセッションが見つかりません: {identifier}",
            status_code=404,
        )


class EpisodeNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"エピソードが見つかりません: {identifier}",
            status_code=404,
        )


class CanonEntryNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"Canon エントリが見つかりません: {identifier}",
            status_code=404,
        )


class WritingSessionNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"執筆セッションが見つかりません: {identifier}",
            status_code=404,
        )


class TRPGScenarioDocumentNotFoundError(ScenarioError):
    def __init__(self, identifier: str):
        super().__init__(
            f"TRPGシナリオ本文が見つかりません: {identifier}",
            status_code=404,
        )


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────


def _dt_iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


async def _count_conversation_messages(session: AsyncSession, session_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(ConversationMessage.id)).where(
            ConversationMessage.session_id == session_id,
            ConversationMessage.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one() or 0)


async def _count_play_logs(session: AsyncSession, play_session_id: uuid.UUID) -> int:
    from ..models.ecc_models import ScenarioPlayLog

    result = await session.execute(
        select(func.count(ScenarioPlayLog.id)).where(
            ScenarioPlayLog.play_session_id == play_session_id
        )
    )
    return int(result.scalar_one() or 0)


async def _latest_play_log_at(
    session: AsyncSession, play_session_id: uuid.UUID
) -> Optional[datetime]:
    from ..models.ecc_models import ScenarioPlayLog

    result = await session.execute(
        select(func.max(ScenarioPlayLog.created_at)).where(
            ScenarioPlayLog.play_session_id == play_session_id
        )
    )
    return result.scalar_one_or_none()


def _scenario_log_sort_key(item: Dict[str, Any]) -> str:
    return str(item.get("updated_at") or item.get("created_at") or "")


# ────────────────────────────────────────────
# シナリオ CRUD
# ────────────────────────────────────────────

_SCENARIO_UPDATABLE = {
    "title",
    "scenario_kind",
    "ruleset",
    "description",
    "genre",
    "perspective",
    "setting",
    "opening_text",
    "gm_instructions",
    "tags",
    "cover_image_path",
    "is_published",
    "voice_tone",
    "voice_tense_rules",
    "voice_vocabulary_register",
    "voice_banned_expressions",
    "voice_example_passages",
}

SCENARIO_KIND_WRITING = "writing"
SCENARIO_KIND_TRPG = "trpg"
SUPPORTED_SCENARIO_KINDS = {SCENARIO_KIND_WRITING, SCENARIO_KIND_TRPG}
SUPPORTED_TRPG_RULESETS = {
    "generic",
    COC_RULESET_TAG,
    COC6_RULESET_TAG,
    COC7_RULESET_TAG,
    "shinobigami",
    "swordworld2_5",
}

TRPG_STRUCTURE_VERSION = 1
TRPG_CHARACTER_NODE_TYPES = {"npc", "enemy", "ally", "creature", "monster"}
TRPG_COMBATANT_NODE_TYPES = {"enemy", "creature", "monster"}
TRPG_REFERENCE_ESCAPE_PATTERNS = [
    re.compile(r"(?:原文|本文|source_text|ソース|出典|URL|外部(?:テキスト|ファイル)?)"),
    re.compile(r"(?:参照|見る|確認).{0,24}(?:原文|本文|source_text|出典|URL|外部)"),
    re.compile(r"(?:原文|本文|source_text|出典|URL|外部).{0,24}(?:参照|見る|確認)"),
    re.compile(r"正本(?:本文|として|とする|扱い|扱う)"),
]
EXTERNAL_SOURCE_LABEL_RE = re.compile(
    r"(?i)(?:https?://|www\.|dropbox\.com|^[A-Za-z]:[\\/]|^\\\\|^/)"
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_metadata(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_structure_tags(value: Any) -> List[str]:
    if isinstance(value, list):
        return _normalize_tags(value)
    if isinstance(value, str):
        return _normalize_tags([part.strip() for part in value.split(",")])
    return []


def _fallback_node_id(title: str, index: int) -> str:
    key = title.lower().strip()
    allowed = []
    for char in key:
        if char.isalnum():
            allowed.append(char)
        elif allowed and allowed[-1] != "-":
            allowed.append("-")
    slug = "".join(allowed).strip("-")
    return slug or f"node-{index + 1}"


def _normalize_structure_node(value: Any, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    title = _normalize_text(value.get("title") or value.get("name"))
    node_id = _normalize_text(value.get("id") or value.get("key"))
    if not title:
        title = node_id or f"Node {index + 1}"
    if not node_id:
        node_id = _fallback_node_id(title, index)
    node_type = _normalize_text(value.get("type") or "custom").lower() or "custom"
    return {
        "id": node_id,
        "type": node_type,
        "title": title,
        "summary": _normalize_text(value.get("summary")),
        "body": str(value.get("body") or ""),
        "tags": _normalize_structure_tags(value.get("tags")),
        "metadata": _normalize_metadata(value.get("metadata")),
    }


def _normalize_structure_link(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    source = _normalize_text(value.get("from") or value.get("source"))
    target = _normalize_text(value.get("to") or value.get("target"))
    if not source or not target:
        return None
    relation = _normalize_text(value.get("relation") or "related").lower() or "related"
    return {
        "from": source,
        "to": target,
        "relation": relation,
        "condition": _normalize_metadata(value.get("condition")),
        "metadata": _normalize_metadata(value.get("metadata")),
    }


def _normalize_trpg_structure(value: Any) -> Dict[str, Any]:
    """TRPG本文の補助インデックスを汎用ノード/リンク形式へ正規化する。"""
    if not isinstance(value, dict):
        value = {}
    normalized = dict(value)

    try:
        version = int(normalized.get("version") or TRPG_STRUCTURE_VERSION)
    except (TypeError, ValueError):
        version = TRPG_STRUCTURE_VERSION
    normalized["version"] = version

    raw_nodes = normalized.get("nodes")
    nodes = [
        node
        for index, raw_node in enumerate(raw_nodes if isinstance(raw_nodes, list) else [])
        if (node := _normalize_structure_node(raw_node, index)) is not None
    ]
    normalized["nodes"] = nodes

    raw_links = normalized.get("links")
    links = [
        link
        for raw_link in (raw_links if isinstance(raw_links, list) else [])
        if (link := _normalize_structure_link(raw_link)) is not None
    ]
    normalized["links"] = links
    normalized["metadata"] = _normalize_metadata(normalized.get("metadata"))
    return normalized


def _iter_structure_strings(value: Any, path: str = "structure"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_structure_strings(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_structure_strings(item, f"{path}.{key}")


def _validate_trpg_structure_runtime_ready(structure: Dict[str, Any]) -> None:
    """TRPG構造データが本文への丸投げではなく実行時情報を持つことを保証する。"""
    nodes = structure.get("nodes")
    if not isinstance(nodes, list):
        return

    errors: List[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        title = _normalize_text(node.get("title") or node.get("id") or f"node-{index + 1}")
        summary = _normalize_text(node.get("summary"))
        body = _normalize_text(node.get("body"))
        metadata = _normalize_metadata(node.get("metadata"))
        if not summary and not body and not metadata:
            errors.append(f"{title}: summary/body/metadata のいずれかが必要です")

    for path, text in _iter_structure_strings(structure):
        stripped = text.strip()
        if not stripped:
            continue
        if any(pattern.search(stripped) for pattern in TRPG_REFERENCE_ESCAPE_PATTERNS):
            errors.append(f"{path}: 本文・原文・外部出典への丸投げは禁止です")

    if errors:
        raise ScenarioError("TRPG構造データが自己完結していません: " + "; ".join(errors[:6]))


def _character_runtime_detail_score(character: ScenarioCharacter) -> int:
    text_fields = (
        getattr(character, "description", ""),
        getattr(character, "personality_override", ""),
        getattr(character, "backstory", ""),
        getattr(character, "psychology", ""),
        getattr(character, "speech_patterns", ""),
        getattr(character, "example_dialogues", ""),
    )
    text_score = sum(len(str(value or "").strip()) for value in text_fields)
    raw_pc_state = getattr(character, "trpg_pc_state", {})
    pc_state = raw_pc_state if isinstance(raw_pc_state, dict) else {}
    pc_score = 40 if pc_state else 0
    return text_score + pc_score


def _validate_trpg_character_nodes(
    structure: Dict[str, Any],
    scenario_characters: List[ScenarioCharacter],
) -> None:
    """NPC/敵ノードがキャラクターDBへ展開されていることを確認する。"""
    nodes = structure.get("nodes")
    if not isinstance(nodes, list):
        return
    characters_by_name = {
        _normalize_text(character.name).lower(): character
        for character in scenario_characters
        if _normalize_text(character.name)
    }
    characters_by_id = {str(character.id): character for character in scenario_characters}
    errors: List[str] = []

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = _normalize_text(node.get("type")).lower()
        if node_type not in TRPG_CHARACTER_NODE_TYPES:
            continue
        metadata = _normalize_metadata(node.get("metadata"))
        linked_id = _normalize_text(
            metadata.get("scenario_character_id") or metadata.get("character_id")
        )
        linked_name = _normalize_text(metadata.get("character_name") or node.get("title"))
        character = characters_by_id.get(linked_id) if linked_id else None
        if character is None and linked_name:
            character = characters_by_name.get(linked_name.lower())
        label = linked_name or node.get("id") or node_type
        if character is None:
            errors.append(f"{label}: ScenarioCharacter への展開が必要です")
            continue
        if _character_runtime_detail_score(character) < 80:
            errors.append(f"{label}: ScenarioCharacter の説明・背景・状態が不足しています")
        if node_type in TRPG_COMBATANT_NODE_TYPES and not (
            isinstance(character.trpg_pc_state, dict) and character.trpg_pc_state
        ):
            errors.append(f"{label}: 敵/怪物ノードには trpg_pc_state が必要です")

    if errors:
        raise ScenarioError("TRPGキャラクターノードがキャラクターDBへ展開されていません: " + "; ".join(errors[:6]))


def _validate_trpg_source_label(source_label: str) -> None:
    label = str(source_label or "").strip()
    if label and EXTERNAL_SOURCE_LABEL_RE.search(label):
        raise ScenarioError("TRPGシナリオ文書の出典欄に外部URLやローカルパスは保存できません")


def _normalize_tags(tags: Any) -> List[str]:
    if not isinstance(tags, list):
        return []
    normalized: List[str] = []
    seen = set()
    for tag in tags:
        value = str(tag).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        normalized.append(value)
        seen.add(key)
    return normalized


def _infer_ruleset_from_metadata(tags: Any, genre: str = "") -> str:
    tag_set = {tag.lower() for tag in _normalize_tags(tags)}
    genre_key = str(genre or "").strip().lower()
    if COC7_RULESET_TAG in tag_set or genre_key == COC7_RULESET_TAG:
        return COC7_RULESET_TAG
    if (
        {COC_RULESET_TAG, COC6_RULESET_TAG, "cthulhu"} & tag_set
        or genre_key in {COC_RULESET_TAG, COC6_RULESET_TAG, "call_of_cthulhu"}
    ):
        return COC6_RULESET_TAG
    for ruleset in ("shinobigami", "swordworld2_5"):
        if ruleset in tag_set or genre_key == ruleset:
            return ruleset
    return ""


def normalize_scenario_metadata(data: dict, existing: Optional[Scenario] = None) -> dict:
    normalized = dict(data)
    tags = _normalize_tags(
        normalized["tags"]
        if "tags" in normalized
        else (existing.tags if existing is not None else [])
    )
    genre = str(
        normalized["genre"]
        if "genre" in normalized
        else (existing.genre if existing is not None else "")
        or ""
    )
    explicit_kind = str(
        normalized.get(
            "scenario_kind",
            existing.scenario_kind if existing is not None else "",
        )
        or ""
    ).strip().lower()
    inferred_ruleset = _infer_ruleset_from_metadata(tags, genre)
    ruleset = str(
        normalized.get(
            "ruleset",
            existing.ruleset if existing is not None else inferred_ruleset,
        )
        or ""
    ).strip().lower()
    if not ruleset:
        ruleset = inferred_ruleset
    if ruleset == COC_RULESET_TAG:
        ruleset = COC6_RULESET_TAG
    if ruleset not in SUPPORTED_TRPG_RULESETS:
        ruleset = "generic" if ruleset else ""

    if explicit_kind in SUPPORTED_SCENARIO_KINDS:
        scenario_kind = explicit_kind
    elif ruleset or "trpg" in {tag.lower() for tag in tags}:
        scenario_kind = SCENARIO_KIND_TRPG
    else:
        scenario_kind = SCENARIO_KIND_WRITING

    if scenario_kind == SCENARIO_KIND_TRPG:
        ruleset = ruleset or "generic"
        lower_tags = {tag.lower() for tag in tags}
        if "trpg" not in lower_tags:
            tags.append("trpg")
        if ruleset != "generic" and ruleset not in lower_tags:
            tags.append(ruleset)
    else:
        ruleset = ""

    normalized["scenario_kind"] = scenario_kind
    normalized["ruleset"] = ruleset
    normalized["tags"] = tags
    return normalized


async def list_scenarios(
    genre: Optional[str] = None,
    published_only: bool = False,
) -> List[dict]:
    """シナリオ一覧を取得する。"""
    async with await get_db_session() as session:
        stmt = select(Scenario).order_by(Scenario.updated_at.desc())
        if genre:
            stmt = stmt.where(Scenario.genre == genre)
        if published_only:
            stmt = stmt.where(Scenario.is_published.is_(True))

        result = await session.execute(stmt)
        return [s.to_dict() for s in result.scalars().all()]


async def create_scenario(data: dict) -> dict:
    """シナリオを新規作成する。"""
    if not data.get("title"):
        raise ScenarioError("タイトルは必須です")
    data = normalize_scenario_metadata(data)

    async with await get_db_session() as session:
        scenario = Scenario(id=uuid.uuid4())
        for key in _SCENARIO_UPDATABLE:
            if key in data:
                setattr(scenario, key, data[key])
        scenario.title = data["title"]
        if "created_by" in data and data["created_by"]:
            scenario.created_by = parse_uuid(data["created_by"])

        session.add(scenario)
        await session.commit()
        await session.refresh(scenario)

        logger.info("シナリオを作成しました: %s (%s)", scenario.title, scenario.id)
        return scenario.to_dict()


async def get_scenario(scenario_id: str, include_children: bool = True) -> dict:
    """シナリオを取得する（キャラクター・シーン含む）。"""
    uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        if include_children:
            stmt = (
                select(Scenario)
                .options(
                    selectinload(Scenario.characters),
                    selectinload(Scenario.scenes),
                    selectinload(Scenario.episodes),
                    selectinload(Scenario.trpg_documents),
                )
                .where(Scenario.id == uid)
            )
            result = await session.execute(stmt)
            scenario = result.scalar_one_or_none()
        else:
            scenario = await session.get(Scenario, uid)

        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        data = scenario.to_dict()
        if include_children:
            data["characters"] = sorted(
                [c.to_dict() for c in scenario.characters],
                key=lambda x: x.get("sort_order", 0),
            )
            data["scenes"] = sorted(
                [s.to_dict() for s in scenario.scenes],
                key=lambda x: x.get("sort_order", 0),
            )
            data["episodes"] = sorted(
                [e.to_dict() for e in scenario.episodes],
                key=lambda x: x.get("sort_order", 0),
            )
            data["trpg_documents"] = sorted(
                [d.to_dict() for d in scenario.trpg_documents],
                key=lambda x: x.get("updated_at") or "",
                reverse=True,
            )
        return data


async def update_scenario(scenario_id: str, data: dict) -> dict:
    """シナリオを更新する。"""
    uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        data = normalize_scenario_metadata(data, scenario)
        for key in _SCENARIO_UPDATABLE:
            if key in data:
                setattr(scenario, key, data[key])

        scenario.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(scenario)

        logger.info("シナリオを更新しました: %s (%s)", scenario.title, scenario.id)
        return scenario.to_dict()


async def delete_scenario(scenario_id: str) -> bool:
    """シナリオを削除する（CASCADE で子テーブルも削除）。"""
    uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        title = scenario.title
        await session.execute(
            sa_delete(ScenarioPlaySession).where(ScenarioPlaySession.scenario_id == uid)
        )
        await session.execute(sa_delete(Scenario).where(Scenario.id == uid))
        await session.commit()

        logger.info("シナリオを削除しました: %s (%s)", title, uid)
        return True


# ────────────────────────────────────────────
# TRPGシナリオ本文
# ────────────────────────────────────────────


def _normalize_document_payload(data: dict) -> dict:
    payload = {
        "ruleset": str(data.get("ruleset") or "").strip(),
        "source_label": str(data.get("source_label") or "").strip(),
        "source_text": str(data.get("source_text") or ""),
        "structure": _normalize_trpg_structure(data.get("structure")),
    }
    if not payload["ruleset"]:
        payload["ruleset"] = "generic"
    if payload["ruleset"] == COC_RULESET_TAG:
        payload["ruleset"] = COC6_RULESET_TAG
    _validate_trpg_source_label(payload["source_label"])
    _validate_trpg_structure_runtime_ready(payload["structure"])
    return payload


async def list_trpg_documents(scenario_id: str) -> List[dict]:
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)
        result = await session.execute(
            select(TRPGScenarioDocument)
            .where(TRPGScenarioDocument.scenario_id == scenario_uid)
            .order_by(TRPGScenarioDocument.updated_at.desc())
        )
        return [doc.to_dict() for doc in result.scalars().all()]


async def upsert_trpg_document(scenario_id: str, data: dict) -> dict:
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    document_uid = parse_uuid(data.get("id"))
    payload = _normalize_document_payload(data)
    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)
        scenario_characters = (
            await session.execute(
                select(ScenarioCharacter).where(ScenarioCharacter.scenario_id == scenario_uid)
            )
        ).scalars().all()
        _validate_trpg_character_nodes(payload["structure"], scenario_characters)

        document = None
        if document_uid:
            document = await session.get(TRPGScenarioDocument, document_uid)
            if document is None or document.scenario_id != scenario_uid:
                raise TRPGScenarioDocumentNotFoundError(str(document_uid))
        if document is None:
            document = TRPGScenarioDocument(id=uuid.uuid4(), scenario_id=scenario_uid)
            session.add(document)

        for key, value in payload.items():
            setattr(document, key, value)
        document.updated_at = datetime.utcnow()
        scenario.scenario_kind = SCENARIO_KIND_TRPG
        if payload["ruleset"]:
            scenario.ruleset = payload["ruleset"]
        scenario.tags = normalize_scenario_metadata(
            {
                "scenario_kind": SCENARIO_KIND_TRPG,
                "ruleset": scenario.ruleset or payload["ruleset"],
                "tags": scenario.tags or [],
                "genre": scenario.genre or "",
            }
        )["tags"]
        scenario.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(document)
        return document.to_dict()


async def delete_trpg_document(scenario_id: str, document_id: str) -> bool:
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    document_uid = parse_uuid_strict(document_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    async with await get_db_session() as session:
        document = await session.get(TRPGScenarioDocument, document_uid)
        if document is None or document.scenario_id != scenario_uid:
            raise TRPGScenarioDocumentNotFoundError(document_id)
        await session.delete(document)
        scenario = await session.get(Scenario, scenario_uid)
        if scenario:
            scenario.updated_at = datetime.utcnow()
        await session.commit()
        return True


# ────────────────────────────────────────────
# シナリオキャラクター CRUD
# ────────────────────────────────────────────

_CHAR_UPDATABLE = {
    "character_id",
    "role",
    "name",
    "description",
    "personality_override",
    "appearance_tags_override",
    "sort_order",
    "backstory",
    "psychology",
    "speech_patterns",
    "relationships",
    "character_arc",
    "importance",
    "example_dialogues",
    "trpg_ruleset",
    "trpg_pc_state",
}


def _normalize_character_payload(data: dict) -> dict:
    normalized = dict(data)
    aliases = {
        "speech_pattern": "speech_patterns",
        "arc": "character_arc",
        "dialogue_samples": "example_dialogues",
    }
    for frontend_key, model_key in aliases.items():
        if frontend_key in normalized and model_key not in normalized:
            normalized[model_key] = normalized[frontend_key]
    if "trpg_ruleset" in normalized:
        ruleset = str(normalized.get("trpg_ruleset") or "").strip().lower()
        normalized["trpg_ruleset"] = COC6_RULESET_TAG if ruleset == COC_RULESET_TAG else ruleset
    if isinstance(normalized.get("trpg_pc_state"), dict):
        ruleset = normalized.get("trpg_ruleset") or normalized["trpg_pc_state"].get("ruleset") or COC6_RULESET_TAG
        if ruleset in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
            normalized["trpg_pc_state"] = normalize_coc_state(
                normalized["trpg_pc_state"],
                str(normalized.get("name") or "探索者"),
                ruleset,
            )
    return normalized


async def add_scenario_character(scenario_id: str, data: dict) -> dict:
    """シナリオにキャラクターを追加する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_character_payload(data)

    if not data.get("name"):
        raise ScenarioError("キャラクター名は必須です")

    async with await get_db_session() as session:
        # シナリオ存在確認
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        char = ScenarioCharacter(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
        )
        for key in _CHAR_UPDATABLE:
            if key in data:
                val = data[key]
                if key == "character_id" and val:
                    val = parse_uuid(val)
                setattr(char, key, val)
        char.name = data["name"]

        session.add(char)
        await session.commit()
        await session.refresh(char)

        logger.info("シナリオキャラクターを追加: %s → %s", char.name, scenario.title)
        return char.to_dict()


async def update_scenario_character(scenario_id: str, char_id: str, data: dict) -> dict:
    """シナリオキャラクターを更新する。"""
    parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    char_uid = parse_uuid_strict(char_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_character_payload(data)

    async with await get_db_session() as session:
        char = await session.get(ScenarioCharacter, char_uid)
        if char is None or str(char.scenario_id) != scenario_id:
            raise ScenarioCharacterNotFoundError(char_id)

        for key in _CHAR_UPDATABLE:
            if key in data:
                val = data[key]
                if key == "character_id" and val:
                    val = parse_uuid(val)
                setattr(char, key, val)

        await session.commit()
        await session.refresh(char)

        logger.info("シナリオキャラクターを更新: %s", char.name)
        return char.to_dict()


async def delete_scenario_character(scenario_id: str, char_id: str) -> bool:
    """シナリオキャラクターを削除する。"""
    parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    char_uid = parse_uuid_strict(char_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        char = await session.get(ScenarioCharacter, char_uid)
        if char is None or str(char.scenario_id) != scenario_id:
            raise ScenarioCharacterNotFoundError(char_id)

        name = char.name
        await session.execute(
            sa_delete(ScenarioCharacter).where(ScenarioCharacter.id == char_uid)
        )
        await session.commit()

        logger.info("シナリオキャラクターを削除: %s", name)
        return True


# ────────────────────────────────────────────
# シナリオシーン CRUD
# ────────────────────────────────────────────

_SCENE_UPDATABLE = {
    "title",
    "description",
    "scene_type",
    "gm_instructions",
    "image_prompt",
    "transitions",
    "sort_order",
    "episode_id",
    "content",
    "status",
    "state_snapshot",
}


def _normalize_scene_payload(data: dict) -> dict:
    normalized = dict(data)
    if "order_index" in normalized and "sort_order" not in normalized:
        normalized["sort_order"] = normalized["order_index"]
    if "body" in normalized and "content" not in normalized:
        normalized["content"] = normalized["body"]
    return normalized


async def add_scenario_scene(scenario_id: str, data: dict) -> dict:
    """シナリオにシーンを追加する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_scene_payload(data)

    if not data.get("title"):
        raise ScenarioError("シーンタイトルは必須です")

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        scene = ScenarioScene(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
        )
        for key in _SCENE_UPDATABLE:
            if key in data:
                val = data[key]
                if key == "episode_id" and val:
                    val = parse_uuid(val)
                setattr(scene, key, val)
        scene.title = data["title"]

        session.add(scene)
        await session.commit()
        await session.refresh(scene)

        logger.info("シナリオシーンを追加: %s → %s", scene.title, scenario.title)
        return scene.to_dict()


async def update_scenario_scene(scenario_id: str, scene_id: str, data: dict) -> dict:
    """シナリオシーンを更新する。"""
    parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    scene_uid = parse_uuid_strict(scene_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_scene_payload(data)

    async with await get_db_session() as session:
        scene = await session.get(ScenarioScene, scene_uid)
        if scene is None or str(scene.scenario_id) != scenario_id:
            raise ScenarioSceneNotFoundError(scene_id)

        for key in _SCENE_UPDATABLE:
            if key in data:
                val = data[key]
                if key == "episode_id" and val:
                    val = parse_uuid(val)
                setattr(scene, key, val)

        await session.commit()
        await session.refresh(scene)

        logger.info("シナリオシーンを更新: %s", scene.title)
        return scene.to_dict()


async def delete_scenario_scene(scenario_id: str, scene_id: str) -> bool:
    """シナリオシーンを削除する。"""
    parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    scene_uid = parse_uuid_strict(scene_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scene = await session.get(ScenarioScene, scene_uid)
        if scene is None or str(scene.scenario_id) != scenario_id:
            raise ScenarioSceneNotFoundError(scene_id)

        title = scene.title
        await session.execute(
            sa_delete(ScenarioScene).where(ScenarioScene.id == scene_uid)
        )
        await session.commit()

        logger.info("シナリオシーンを削除: %s", title)
        return True


# ────────────────────────────────────────────
# プレイセッション
# ────────────────────────────────────────────


async def start_play_session(
    scenario_id: str,
    user_id: str = "default_user",
) -> dict:
    """シナリオのプレイセッションを開始する。

    ConversationSession と ScenarioPlaySession を同時に作成する。
    """
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        # シナリオ取得（シーン含む）
        stmt = (
            select(Scenario)
            .options(selectinload(Scenario.scenes))
            .where(Scenario.id == scenario_uid)
        )
        result = await session.execute(stmt)
        scenario = result.scalar_one_or_none()
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        # 会話セッション作成
        conv_session = ConversationSession(
            id=uuid.uuid4(),
            user_id=user_id,
            character_name=f"scenario_{scenario.title}",
            title=f"[シナリオ] {scenario.title}",
        )
        session.add(conv_session)

        # 最初のシーンを特定
        first_scene_id = None
        if scenario.scenes:
            sorted_scenes = sorted(scenario.scenes, key=lambda s: s.sort_order)
            first_scene_id = sorted_scenes[0].id

        # プレイセッション作成
        play_session = ScenarioPlaySession(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
            conversation_session_id=conv_session.id,
            current_scene_id=first_scene_id,
            perspective=scenario.perspective or "first_person",
            player_state={},
            status="in_progress",
        )
        session.add(play_session)

        await session.commit()
        await session.refresh(play_session)

        logger.info(
            "プレイセッションを開始: scenario=%s, session=%s",
            scenario.title,
            play_session.id,
        )

        result_data = play_session.to_dict()
        result_data["scenario"] = scenario.to_dict()
        result_data["conversation_session_id"] = str(conv_session.id)
        return result_data


async def get_play_session(session_id: str) -> dict:
    """プレイセッションを取得する。"""
    uid = parse_uuid_strict(session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, uid)
        if play_session is None:
            raise PlaySessionNotFoundError(session_id)

        return play_session.to_dict()


async def get_play_session_by_conversation_id(conv_session_id: str) -> Optional[dict]:
    """会話セッションIDから対応するプレイセッションを取得する。"""
    uid = parse_uuid_strict(conv_session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        from sqlalchemy import select

        stmt = select(ScenarioPlaySession).where(
            ScenarioPlaySession.conversation_session_id == uid
        )
        result = await session.execute(stmt)
        play_session = result.scalar_one_or_none()

        if play_session is None:
            return None

        # シナリオ情報と現在のシーン情報も結合して返す
        scenario = await session.get(Scenario, play_session.scenario_id)
        scene = None
        if play_session.current_scene_id:
            scene = await session.get(ScenarioScene, play_session.current_scene_id)

        result_data = play_session.to_dict()
        if scenario:
            result_data["scenario"] = scenario.to_dict()
        if scene:
            result_data["current_scene"] = scene.to_dict()

        return result_data


async def update_play_state(session_id: str, updates: dict) -> dict:
    """プレイセッションの状態を更新する。"""
    uid = parse_uuid_strict(session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, uid)
        if play_session is None:
            raise PlaySessionNotFoundError(session_id)

        if "current_scene_id" in updates:
            scene_uid = parse_uuid(updates["current_scene_id"])
            play_session.current_scene_id = scene_uid

        if "player_state" in updates:
            # マージ（上書きではなく追加更新）
            current_state = play_session.player_state or {}
            current_state.update(updates["player_state"])
            play_session.player_state = current_state

        if "status" in updates:
            play_session.status = updates["status"]

        if "perspective" in updates:
            play_session.perspective = updates["perspective"]

        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(play_session)

        logger.info("プレイセッションを更新: %s", uid)
        return play_session.to_dict()


# ────────────────────────────────────────────
# エピソード CRUD
# ────────────────────────────────────────────

_EPISODE_UPDATABLE = {
    "title",
    "synopsis_sentence",
    "synopsis_paragraph",
    "synopsis_full",
    "beat_sheet",
    "status",
    "sort_order",
}


def _normalize_episode_payload(data: dict) -> dict:
    normalized = dict(data)
    aliases = {
        "one_line_summary": "synopsis_sentence",
        "paragraph_summary": "synopsis_paragraph",
        "full_summary": "synopsis_full",
    }
    for frontend_key, model_key in aliases.items():
        if frontend_key in normalized and model_key not in normalized:
            normalized[model_key] = normalized[frontend_key]
    return normalized


async def list_episodes(scenario_id: str) -> List[dict]:
    """シナリオのエピソード一覧を取得する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = (
            select(ScenarioEpisode)
            .where(ScenarioEpisode.scenario_id == scenario_uid)
            .order_by(ScenarioEpisode.sort_order)
        )
        result = await session.execute(stmt)
        return [e.to_dict() for e in result.scalars().all()]


async def create_episode(scenario_id: str, data: dict) -> dict:
    """エピソードを新規作成する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_episode_payload(data)

    if not data.get("title"):
        raise ScenarioError("エピソードタイトルは必須です")

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        episode = ScenarioEpisode(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
        )
        for key in _EPISODE_UPDATABLE:
            if key in data:
                setattr(episode, key, data[key])
        episode.title = data["title"]

        session.add(episode)
        await session.commit()
        await session.refresh(episode)

        logger.info("エピソードを作成: %s → %s", episode.title, scenario.title)
        return episode.to_dict()


async def update_episode(episode_id: str, data: dict) -> dict:
    """エピソードを更新する。"""
    uid = parse_uuid_strict(episode_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
    data = _normalize_episode_payload(data)

    async with await get_db_session() as session:
        episode = await session.get(ScenarioEpisode, uid)
        if episode is None:
            raise EpisodeNotFoundError(episode_id)

        for key in _EPISODE_UPDATABLE:
            if key in data:
                setattr(episode, key, data[key])

        episode.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(episode)

        logger.info("エピソードを更新: %s", episode.title)
        return episode.to_dict()


async def delete_episode(episode_id: str) -> bool:
    """エピソードを削除する。"""
    uid = parse_uuid_strict(episode_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        episode = await session.get(ScenarioEpisode, uid)
        if episode is None:
            raise EpisodeNotFoundError(episode_id)

        title = episode.title
        await session.execute(
            sa_delete(ScenarioEpisode).where(ScenarioEpisode.id == uid)
        )
        await session.commit()

        logger.info("エピソードを削除: %s", title)
        return True


async def reorder_episodes(scenario_id: str, episode_ids: List[str]) -> List[dict]:
    """エピソードの並び順を更新する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        for idx, eid in enumerate(episode_ids):
            uid = parse_uuid_strict(eid, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))
            episode = await session.get(ScenarioEpisode, uid)
            if episode is not None and episode.scenario_id == scenario_uid:
                episode.sort_order = idx

        await session.commit()

        stmt = (
            select(ScenarioEpisode)
            .where(ScenarioEpisode.scenario_id == scenario_uid)
            .order_by(ScenarioEpisode.sort_order)
        )
        result = await session.execute(stmt)
        return [e.to_dict() for e in result.scalars().all()]


# ────────────────────────────────────────────
# Canon CRUD
# ────────────────────────────────────────────

_CANON_UPDATABLE = {"category", "fact", "source_scene_id"}


async def list_canon_entries(
    scenario_id: str,
    category: Optional[str] = None,
) -> List[dict]:
    """シナリオのCanonエントリ一覧を取得する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = (
            select(ScenarioCanonEntry)
            .where(ScenarioCanonEntry.scenario_id == scenario_uid)
            .order_by(ScenarioCanonEntry.created_at)
        )
        if category:
            stmt = stmt.where(ScenarioCanonEntry.category == category)

        result = await session.execute(stmt)
        return [e.to_dict() for e in result.scalars().all()]


async def create_canon_entry(scenario_id: str, data: dict) -> dict:
    """Canonエントリを作成する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    if not data.get("category"):
        raise ScenarioError("カテゴリは必須です")
    if not data.get("fact"):
        raise ScenarioError("事実テキストは必須です")

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        entry = ScenarioCanonEntry(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
            category=data["category"],
            fact=data["fact"],
        )
        if data.get("source_scene_id"):
            entry.source_scene_id = parse_uuid(data["source_scene_id"])

        session.add(entry)
        await session.commit()
        await session.refresh(entry)

        logger.info("Canonエントリを作成: %s (%s)", entry.category, scenario.title)
        return entry.to_dict()


async def update_canon_entry(entry_id: str, data: dict) -> dict:
    """Canonエントリを更新する。"""
    uid = parse_uuid_strict(entry_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        entry = await session.get(ScenarioCanonEntry, uid)
        if entry is None:
            raise CanonEntryNotFoundError(entry_id)

        for key in _CANON_UPDATABLE:
            if key in data:
                val = data[key]
                if key == "source_scene_id" and val:
                    val = parse_uuid(val)
                setattr(entry, key, val)

        await session.commit()
        await session.refresh(entry)

        logger.info("Canonエントリを更新: %s", entry.category)
        return entry.to_dict()


async def delete_canon_entry(entry_id: str) -> bool:
    """Canonエントリを削除する。"""
    uid = parse_uuid_strict(entry_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        entry = await session.get(ScenarioCanonEntry, uid)
        if entry is None:
            raise CanonEntryNotFoundError(entry_id)

        await session.execute(
            sa_delete(ScenarioCanonEntry).where(ScenarioCanonEntry.id == uid)
        )
        await session.commit()

        logger.info("Canonエントリを削除: %s", uid)
        return True


# ────────────────────────────────────────────
# 執筆セッション
# ────────────────────────────────────────────


async def start_writing_session(
    scenario_id: str,
    data: dict,
    user_id: str = "default_user",
) -> dict:
    """執筆セッションを開始する。ConversationSessionも同時に作成する。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        # ターゲットシーン名の取得（タイトル用）
        target_label = "フリー"
        if data.get("target_scene_id"):
            scene = await session.get(
                ScenarioScene, parse_uuid(data["target_scene_id"])
            )
            if scene:
                target_label = scene.title

        # 会話セッション作成
        conv_session = ConversationSession(
            id=uuid.uuid4(),
            user_id=user_id,
            character_name=f"scenario_{scenario.title}",
            title=f"[執筆] {scenario.title} - {target_label}",
        )
        session.add(conv_session)

        # 執筆セッション作成
        ws = ScenarioWritingSession(
            id=uuid.uuid4(),
            scenario_id=scenario_uid,
            conversation_session_id=conv_session.id,
        )
        if data.get("target_episode_id"):
            ws.target_episode_id = parse_uuid(data["target_episode_id"])
        if data.get("target_scene_id"):
            ws.target_scene_id = parse_uuid(data["target_scene_id"])
        if data.get("writing_prompt"):
            ws.writing_prompt = data["writing_prompt"]

        session.add(ws)
        await session.commit()
        await session.refresh(ws)

        logger.info(
            "執筆セッションを開始: scenario=%s, session=%s",
            scenario.title,
            ws.id,
        )

        result_data = ws.to_dict()
        result_data["conversation_session_id"] = str(conv_session.id)
        return result_data


async def get_writing_session(session_id: str) -> dict:
    """執筆セッションを取得する。"""
    uid = parse_uuid_strict(session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        ws = await session.get(ScenarioWritingSession, uid)
        if ws is None:
            raise WritingSessionNotFoundError(session_id)

        return ws.to_dict()


async def get_writing_session_by_conversation(conv_session_id: str) -> Optional[dict]:
    """会話セッションIDから対応する執筆セッションを取得する。"""
    uid = parse_uuid_strict(conv_session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        stmt = select(ScenarioWritingSession).where(
            ScenarioWritingSession.conversation_session_id == uid
        )
        result = await session.execute(stmt)
        ws = result.scalar_one_or_none()

        if ws is None:
            return None

        result_data = ws.to_dict()
        scenario = await session.get(Scenario, ws.scenario_id)
        if scenario:
            result_data["scenario"] = scenario.to_dict()

        return result_data


async def list_scenario_logs(scenario_id: str) -> Dict[str, Any]:
    """シナリオに紐づく執筆・ロールプレイ・TRPGログを共通形式で返す。"""
    scenario_uid = parse_uuid_strict(scenario_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scenario = await session.get(Scenario, scenario_uid)
        if scenario is None:
            raise ScenarioNotFoundError(scenario_id)

        items: List[Dict[str, Any]] = []

        # 執筆ログ: scenario_writing_sessions -> conversation_sessions/messages
        writing_result = await session.execute(
            select(ScenarioWritingSession)
            .where(ScenarioWritingSession.scenario_id == scenario_uid)
            .order_by(ScenarioWritingSession.updated_at.desc())
        )
        for ws in writing_result.scalars().all():
            conv = (
                await session.get(ConversationSession, ws.conversation_session_id)
                if ws.conversation_session_id
                else None
            )
            scene = (
                await session.get(ScenarioScene, ws.target_scene_id)
                if ws.target_scene_id
                else None
            )
            episode = (
                await session.get(ScenarioEpisode, ws.target_episode_id)
                if ws.target_episode_id
                else None
            )
            message_count = (
                await _count_conversation_messages(session, ws.conversation_session_id)
                if ws.conversation_session_id
                else 0
            )
            target_label = scene.title if scene else "フリー"
            if episode and scene:
                target_label = f"{episode.title} / {scene.title}"

            items.append(
                {
                    "id": str(ws.id),
                    "type": "writing",
                    "type_label": "執筆",
                    "scenario_id": str(scenario_uid),
                    "conversation_session_id": (
                        str(ws.conversation_session_id)
                        if ws.conversation_session_id
                        else None
                    ),
                    "room_id": None,
                    "target_id": str(ws.target_scene_id) if ws.target_scene_id else None,
                    "target_label": target_label,
                    "title": conv.title if conv and conv.title else f"執筆 / {target_label}",
                    "status": ws.status or "in_progress",
                    "count": message_count,
                    "created_at": _dt_iso(ws.created_at),
                    "updated_at": _dt_iso(
                        conv.last_activity if conv else ws.updated_at or ws.created_at
                    ),
                    "href": (
                        f"/chat?s={ws.conversation_session_id}"
                        if ws.conversation_session_id
                        else None
                    ),
                }
            )

        # ロールプレイログ: character_name に埋め込まれた scenario_id を正本として拾う
        roleplay_prefix = f"scenario_roleplay:{scenario_uid}:"
        roleplay_result = await session.execute(
            select(ConversationSession)
            .where(
                ConversationSession.character_name.like(f"{roleplay_prefix}%"),
                ConversationSession.deleted_at.is_(None),
            )
            .order_by(ConversationSession.last_activity.desc())
        )
        for conv in roleplay_result.scalars().all():
            character_id = conv.character_name.removeprefix(roleplay_prefix)
            character = parse_uuid(character_id)
            char = await session.get(ScenarioCharacter, character) if character else None
            target_label = char.name if char else "キャラクター"
            message_count = await _count_conversation_messages(session, conv.id)
            items.append(
                {
                    "id": str(conv.id),
                    "type": "roleplay",
                    "type_label": "ロールプレイ",
                    "scenario_id": str(scenario_uid),
                    "conversation_session_id": str(conv.id),
                    "room_id": None,
                    "target_id": character_id or None,
                    "target_label": target_label,
                    "title": conv.title or f"ロールプレイ / {target_label}",
                    "status": "active" if conv.is_active else "inactive",
                    "count": message_count,
                    "created_at": _dt_iso(conv.session_start),
                    "updated_at": _dt_iso(conv.last_activity or conv.session_start),
                    "href": f"/chat?s={conv.id}",
                }
            )

        # TRPGログ: ルーム/プレイセッションを部屋として開く
        play_result = await session.execute(
            select(ScenarioPlaySession)
            .where(ScenarioPlaySession.scenario_id == scenario_uid)
            .order_by(ScenarioPlaySession.updated_at.desc())
        )
        for play in play_result.scalars().all():
            log_count = await _count_play_logs(session, play.id)
            latest_log_at = await _latest_play_log_at(session, play.id)
            title = play.room_title or scenario.title
            items.append(
                {
                    "id": str(play.id),
                    "type": "trpg",
                    "type_label": "TRPG",
                    "scenario_id": str(scenario_uid),
                    "conversation_session_id": (
                        str(play.conversation_session_id)
                        if play.conversation_session_id
                        else None
                    ),
                    "room_id": str(play.id),
                    "target_id": str(play.current_scene_id)
                    if play.current_scene_id
                    else None,
                    "target_label": title,
                    "title": title,
                    "status": play.status or "in_progress",
                    "count": log_count,
                    "created_at": _dt_iso(play.started_at),
                    "updated_at": _dt_iso(latest_log_at or play.updated_at or play.started_at),
                    "href": f"/trpg/rooms/{play.id}",
                }
            )

        items.sort(key=_scenario_log_sort_key, reverse=True)

        return {
            "scenario": {
                "id": str(scenario.id),
                "title": scenario.title,
                "scenario_kind": getattr(scenario, "scenario_kind", "writing"),
            },
            "logs": items,
            "count": len(items),
        }


async def get_scenario_log_context_by_conversation(
    conv_session_id: str,
) -> Optional[Dict[str, Any]]:
    """会話セッションIDがシナリオ由来なら、そのシナリオのログ一覧を返す。"""
    uid = parse_uuid_strict(conv_session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        writing_result = await session.execute(
            select(ScenarioWritingSession).where(
                ScenarioWritingSession.conversation_session_id == uid
            )
        )
        writing_session = writing_result.scalar_one_or_none()
        if writing_session:
            data = await list_scenario_logs(str(writing_session.scenario_id))
            data["active_log_id"] = str(writing_session.id)
            data["active_log_type"] = "writing"
            return data

        play_result = await session.execute(
            select(ScenarioPlaySession).where(
                ScenarioPlaySession.conversation_session_id == uid
            )
        )
        play_session = play_result.scalar_one_or_none()
        if play_session:
            data = await list_scenario_logs(str(play_session.scenario_id))
            data["active_log_id"] = str(play_session.id)
            data["active_log_type"] = "trpg"
            return data

        conv = await session.get(ConversationSession, uid)
        if not conv:
            return None
        roleplay_match = re.fullmatch(
            r"scenario_roleplay:([^:]+):([^:]+)", conv.character_name or ""
        )
        if roleplay_match:
            scenario_id_value, _character_id = roleplay_match.groups()
            data = await list_scenario_logs(scenario_id_value)
            data["active_log_id"] = str(conv.id)
            data["active_log_type"] = "roleplay"
            return data

    return None


async def update_writing_session(session_id: str, data: dict) -> dict:
    """執筆セッションを更新する。"""
    uid = parse_uuid_strict(session_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    _WS_UPDATABLE = {"writing_prompt", "status"}

    async with await get_db_session() as session:
        ws = await session.get(ScenarioWritingSession, uid)
        if ws is None:
            raise WritingSessionNotFoundError(session_id)

        for key in _WS_UPDATABLE:
            if key in data:
                setattr(ws, key, data[key])

        if "target_episode_id" in data:
            ws.target_episode_id = parse_uuid(data["target_episode_id"])
        if "target_scene_id" in data:
            ws.target_scene_id = parse_uuid(data["target_scene_id"])

        ws.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(ws)

        logger.info("執筆セッションを更新: %s", uid)
        return ws.to_dict()


# ────────────────────────────────────────────
# シーン本文操作
# ────────────────────────────────────────────


async def save_scene_content(
    scene_id: str,
    content: str,
    create_version: bool = True,
) -> dict:
    """シーンの本文を保存する。旧バージョンをcontent_versionsに追加。"""
    uid = parse_uuid_strict(scene_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scene = await session.get(ScenarioScene, uid)
        if scene is None:
            raise ScenarioSceneNotFoundError(scene_id)

        if create_version and scene.content:
            versions = list(scene.content_versions or [])
            versions.append(
                {
                    "version": len(versions) + 1,
                    "content": scene.content,
                    "created_at": datetime.utcnow().isoformat(),
                }
            )
            scene.content_versions = versions

        scene.content = content
        scene.word_count = len(content)

        await session.commit()
        await session.refresh(scene)

        logger.info("シーン本文を保存: %s (%d文字)", scene.title, scene.word_count)
        return scene.to_dict()


async def get_scene_content(scene_id: str) -> dict:
    """シーンの本文とバージョン履歴を取得する。"""
    uid = parse_uuid_strict(scene_id, lambda v: ScenarioError(f"無効なUUID形式です: {v}"))

    async with await get_db_session() as session:
        scene = await session.get(ScenarioScene, uid)
        if scene is None:
            raise ScenarioSceneNotFoundError(scene_id)

        return {
            "id": str(scene.id),
            "title": scene.title,
            "content": scene.content or "",
            "content_versions": scene.content_versions or [],
            "word_count": scene.word_count or 0,
            "status": scene.status or "draft",
        }
