#!/usr/bin/env python3
"""旧 Docs シナリオを Story Studio v1 へ片道移行する管理スクリプト。

既定は dry-run で、旧 SQL の ``scenario_*`` 本文ではなく active Docs ノードを
本文ソースとして件数・文字数・sha256 を報告する。旧テーブルの削除や Docs の
物理削除はこのスクリプトの責務に含めない。

運用手順は以下の 3 段階に固定する。``--apply`` / ``--verify`` /
``--archive-docs`` は**同時指定できず**、必ずこの順に 1 つずつ実行する。

1. ``--apply``        story_* へ書き込む
2. ``--verify``       件数・文字数・sha を read-only 突合する
3. ``--archive-docs`` 移行済み Docs subtree を論理 archive する

``--verify`` は「active Docs ノード」を期待値の唯一のソースにするため、
``--archive-docs`` を先に走らせると期待値を再構成できなくなる。この順序を
守れなかった場合、``--verify`` は ``docs_root_archived`` を issue として報告
して黙って通さない。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid5

from sqlalchemy import Column, JSON, MetaData, String, Table, Text, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.memory.database import get_db_session  # noqa: E402
from src.memory.models import KnowledgeEdge, KnowledgeNode  # noqa: E402
from src.memory.models.story import (  # noqa: E402
    StoryCharacter,
    StoryEpisode,
    StoryEpisodeRevision,
    StoryLink,
    StoryNote,
    StorySearchIndex,
    StoryWork,
    StoryWorkCharacter,
)
from src.services.story_studio import StoryRevisionService  # noqa: E402


BODY_FORMATS = {"docs_paragraph", "scenario_paragraph", "doc_block"}
DOCS_MEMBER_RELATION = "scenario_member"
_OPENING_NOTE_NAMESPACE = UUID("cf0e4e47-5f91-42a8-8d82-2f2cc50b46bc")

# §5.3 の許容値。旧 status はここへ正規化してから書き込む。
EPISODE_STATUSES = ("unwritten", "draft", "revising", "done", "on_hold")
_EPISODE_STATUS_ALIASES = {
    "unwritten": "unwritten",
    "planned": "unwritten",
    "planning": "unwritten",
    "todo": "unwritten",
    "draft": "draft",
    "wip": "draft",
    "writing": "draft",
    "in_progress": "draft",
    "revising": "revising",
    "revision": "revising",
    "review": "revising",
    "reviewing": "revising",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "published": "done",
    "on_hold": "on_hold",
    "hold": "on_hold",
    "paused": "on_hold",
}
# 作品 status（§5.2）を「完成」とみなす旧値。
_WORK_COMPLETE_STATUSES = {"done", "complete", "completed"}
# 旧 scenarios の自動充填デフォルト。§11.3 によりデフォルト一致は移行しない。
_LEGACY_GENRE_DEFAULT = ""
_LEGACY_PERSPECTIVE_DEFAULT = "first_person"
_LEGACY_TAGS_DEFAULT: list[str] = []

# 旧テーブルは削除前のDBにだけ残る移行入力であり、ORM正本ではない。
# ForeignKey/relationshipを持たない軽量Table定義にすることで、ecc_modelsの
# 旧Scenario系クラスをimportせずにdry-run/verifyを実行できるようにする。
_LEGACY_METADATA = MetaData()
_LEGACY_SCENARIOS = Table(
    "scenarios",
    _LEGACY_METADATA,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("title", String(200)),
    Column("scenario_kind", String(20)),
    Column("description", Text),
    Column("genre", String(100)),
    Column("perspective", String(50)),
    Column("tags", JSON),
    Column("opening_text", Text),
    Column("knowledge_node_id", PGUUID(as_uuid=True)),
    Column("created_by", PGUUID(as_uuid=True)),
    Column("voice_tone", Text),
    Column("voice_tense_rules", Text),
    Column("voice_vocabulary_register", Text),
    Column("voice_banned_expressions", JSON),
    Column("voice_example_passages", Text),
)
_LEGACY_SCENARIO_EPISODES = Table(
    "scenario_episodes",
    _LEGACY_METADATA,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("scenario_id", PGUUID(as_uuid=True)),
    Column("knowledge_node_id", PGUUID(as_uuid=True)),
    Column("synopsis_sentence", Text),
    Column("synopsis_paragraph", Text),
    Column("status", String(20)),
)
_LEGACY_SCENARIO_CANON = Table(
    "scenario_canon_entries",
    _LEGACY_METADATA,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("scenario_id", PGUUID(as_uuid=True)),
    Column("category", String(50)),
    Column("fact", Text),
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _etag(text: str) -> str:
    return f"sha256:{_sha256(text)}"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _meta(node: KnowledgeNode) -> dict[str, Any]:
    value = node.body_json
    return value if isinstance(value, dict) else {}


def _legacy_rows(result: Any) -> list[SimpleNamespace]:
    return [SimpleNamespace(**dict(row)) for row in result.mappings().all()]


def _owner_id(scenario: Any, root: KnowledgeNode | None) -> UUID | None:
    for value in (
        getattr(scenario, "created_by", None),
        getattr(root, "created_by", None) if root is not None else None,
    ):
        if value:
            try:
                return UUID(str(value))
            except (TypeError, ValueError):
                continue
    return None


def _category_for(node: KnowledgeNode, by_id: dict[UUID, KnowledgeNode], root_id: UUID) -> str | None:
    """親カテゴリ metadata を辿って Docs asset の分類を決める。"""

    current: KnowledgeNode | None = node
    visited: set[UUID] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        value = _meta(current).get("category_key")
        if value in {"episodes", "characters", "settings", "materials"}:
            return str(value)
        if current.id == root_id:
            break
        current = by_id.get(current.parent_id) if current.parent_id else None
    return None


def _is_under_root(node: KnowledgeNode, by_id: dict[UUID, KnowledgeNode], root_id: UUID) -> bool:
    current: KnowledgeNode | None = node
    visited: set[UUID] = set()
    while current is not None and current.id not in visited:
        if current.id == root_id:
            return True
        visited.add(current.id)
        current = by_id.get(current.parent_id) if current.parent_id else None
    return False


def _body_nodes(
    node: KnowledgeNode,
    by_id: dict[UUID, KnowledgeNode],
    *,
    children_by_parent: dict[UUID, list[KnowledgeNode]] | None = None,
) -> list[KnowledgeNode]:
    """本文を構成する paragraph/text ノードを順序付きで返す。

    子孫を全階層フラット化すると、入れ子の資産ノード配下の段落まで親の本文へ
    混入し、同じ段落を親子両方の本文として二重に取り込む。本文は資産ノード
    直下 1 階層に限定して、静かに壊れる経路を塞ぐ。
    """

    children = children_by_parent
    if children is None:
        children = defaultdict(list)
        for child in by_id.values():
            if child.archived_at is None:
                children[child.parent_id].append(child)
    paragraphs = [
        child
        for child in children.get(node.id, [])
        if _meta(child).get("format") in BODY_FORMATS
        or str(child.node_type or "").lower() in {"paragraph", "text"}
    ]
    paragraphs.sort(
        key=lambda item: (
            float(item.sort_order or 0),
            item.created_at or datetime.min,
            str(item.id),
        )
    )
    return paragraphs


def _node_body(
    node: KnowledgeNode,
    by_id: dict[UUID, KnowledgeNode],
    *,
    children_by_parent: dict[UUID, list[KnowledgeNode]] | None = None,
) -> str:
    """本文 paragraph ノードがあれば順序を保って連結する。"""

    paragraphs = _body_nodes(node, by_id, children_by_parent=children_by_parent)
    if paragraphs:
        return "\n".join(str(item.body_text or "") for item in paragraphs)
    return str(node.body_text or "")


async def _docs_assets(
    session: Any,
    scenario: Any,
    *,
    nodes_cache: dict[str, list[KnowledgeNode]] | None = None,
) -> tuple[
    KnowledgeNode | None,
    dict[str, list[KnowledgeNode]],
    set[UUID],
    dict[UUID, KnowledgeNode],
]:
    root = None
    if scenario.knowledge_node_id:
        root = await session.get(KnowledgeNode, scenario.knowledge_node_id)
    if root is None:
        return (
            None,
            {key: [] for key in ("episodes", "characters", "settings", "materials")},
            set(),
            {},
        )

    # 期待値のソースは常に active Docs ノードだけにする。archived を読み戻すと
    # 移行前から archive 済みの旧段落まで本文へ混ざり、--verify が全エピソードで
    # sha 不一致を出す（偽陽性）。archive は移行検証の後段に固定する（§11.4）。
    cache_key = str(root.workspace_id)
    if nodes_cache is not None and cache_key in nodes_cache:
        nodes = nodes_cache[cache_key]
    else:
        nodes = list(
            (
                await session.scalars(
                    select(KnowledgeNode).where(
                        KnowledgeNode.workspace_id == root.workspace_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
            ).all()
        )
        if nodes_cache is not None:
            nodes_cache[cache_key] = nodes
    by_id = {node.id: node for node in nodes}
    assets: dict[str, list[KnowledgeNode]] = {
        key: [] for key in ("episodes", "characters", "settings", "materials")
    }
    archived_or_migrated_ids: set[UUID] = {root.id}
    for node in nodes:
        if node.id == root.id or not _is_under_root(node, by_id, root.id):
            continue
        metadata = _meta(node)
        if metadata.get("format") == "docs_scenario_category":
            archived_or_migrated_ids.add(node.id)
            continue
        category = _category_for(node, by_id, root.id)
        if category is None:
            continue
        # Paragraphノードは親の本文へ含め、独立作品ノードだけを移行単位にする。
        if metadata.get("format") in BODY_FORMATS:
            continue
        assets[category].append(node)
        archived_or_migrated_ids.add(node.id)

    # 旧サービスが member edge を正本としている場合、直接所属している asset も
    # category metadata が欠けていて拾えるようにする。
    member_ids = set(
        (
            await session.scalars(
                select(KnowledgeEdge.target_node_id).where(
                    KnowledgeEdge.source_node_id == root.id,
                    KnowledgeEdge.relation_type == DOCS_MEMBER_RELATION,
                )
            )
        ).all()
    )
    for node_id in member_ids:
        node = by_id.get(node_id)
        if node is None or _meta(node).get("format") == "docs_scenario_category":
            continue
        category = _category_for(node, by_id, root.id)
        if category and node not in assets[category]:
            assets[category].append(node)
            archived_or_migrated_ids.add(node.id)

    # --archive-docs は移行単位だけでなく Docs subtree 全体を archive する。
    # 本文 paragraph descendants も含めることで、本文だけ active のまま残る
    # 中途半端な移行状態を防ぐ。
    archived_or_migrated_ids = {
        node.id for node in nodes if _is_under_root(node, by_id, root.id)
    }

    for rows in assets.values():
        rows.sort(key=lambda item: (float(item.sort_order or 0), item.created_at or datetime.min, str(item.id)))
    return root, assets, archived_or_migrated_ids, by_id


def _style_guide(scenario: Any) -> str | None:
    parts: list[str] = []
    labels = (
        ("トーン", getattr(scenario, "voice_tone", "")),
        ("時制ルール", getattr(scenario, "voice_tense_rules", "")),
        ("語彙レベル", getattr(scenario, "voice_vocabulary_register", "")),
        ("禁止表現", getattr(scenario, "voice_banned_expressions", [])),
        ("例文", getattr(scenario, "voice_example_passages", "")),
    )
    for label, value in labels:
        if value in (None, "", [], {}):
            continue
        rendered = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        parts.append(f"{label}: {rendered}")
    return "\n".join(parts) or None


def _meta_line(scenario: Any) -> str | None:
    """genre / perspective / tags の非デフォルト値だけを 1 行へまとめる（§11.2）。

    自動充填のデフォルト（``genre=''`` / ``perspective='first_person'`` /
    ``tags=[]``）はユーザー設定ではないため移さない（§11.3）。
    """

    parts: list[str] = []
    genre = str(getattr(scenario, "genre", "") or "").strip()
    if genre and genre != _LEGACY_GENRE_DEFAULT:
        parts.append(f"ジャンル: {genre}")
    perspective = str(getattr(scenario, "perspective", "") or "").strip()
    if perspective and perspective != _LEGACY_PERSPECTIVE_DEFAULT:
        parts.append(f"視点: {perspective}")
    raw_tags = getattr(scenario, "tags", None)
    tags = (
        [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if isinstance(raw_tags, list)
        else []
    )
    if tags and tags != _LEGACY_TAGS_DEFAULT:
        parts.append("タグ: " + ", ".join(tags))
    return " / ".join(parts) or None


def _synopsis(scenario: Any) -> str | None:
    """``description`` 末尾へ genre / perspective / tags のメタ行を足す（§11.2）。"""

    parts = [
        value
        for value in (
            str(getattr(scenario, "description", "") or "").strip(),
            _meta_line(scenario),
        )
        if value
    ]
    return "\n".join(parts) or None


def _episode_status(raw: Any, *, body: str) -> str:
    """旧 status を §5.3 の許容値へ正規化する。

    列挙外・空の値は本文の有無から推定する（本文あり = ``draft``、
    本文なし = ``unwritten``）。
    """

    normalized = _EPISODE_STATUS_ALIASES.get(str(raw or "").strip().lower())
    if normalized:
        return normalized
    return "draft" if body else "unwritten"


def _canon_note_title(category: Any, fact: Any, *, limit: int = 30) -> str:
    """category だけでは「source」「entity」等が重複するため fact 冒頭で一意化する。"""

    label = str(category or "").strip() or "設定"
    summary = " ".join(str(fact or "").split())
    if not summary:
        return label
    if len(summary) > limit:
        summary = summary[: limit - 1] + "…"
    return f"{label}: {summary}"


def _work_status(episode_rows: Iterable[dict[str, Any]]) -> str:
    rows = list(episode_rows)
    if not rows:
        return "planning"
    complete = sum(
        1
        for row in rows
        if str(row.get("status") or "").lower() in _WORK_COMPLETE_STATUSES
    )
    if complete == len(rows):
        return "complete"
    return "writing" if complete else "planning"


async def build_migration_plan(
    session: Any,
    *,
    work_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """旧シナリオと active Docs を書き込まずに移行計画へ変換する。"""

    query = select(_LEGACY_SCENARIOS).order_by(
        _LEGACY_SCENARIOS.c.title,
        _LEGACY_SCENARIOS.c.id,
    )
    if work_id is not None:
        query = query.where(_LEGACY_SCENARIOS.c.id == work_id)
    scenarios = _legacy_rows(await session.execute(query))
    plans: list[dict[str, Any]] = []
    nodes_cache: dict[str, list[KnowledgeNode]] = {}
    for scenario in scenarios:
        root, assets, archive_ids, node_index = await _docs_assets(
            session,
            scenario,
            nodes_cache=nodes_cache,
        )
        children_by_parent: dict[UUID, list[KnowledgeNode]] = defaultdict(list)
        for child in node_index.values():
            if child.archived_at is None:
                children_by_parent[child.parent_id].append(child)
        owner = _owner_id(scenario, root)
        old_episodes = _legacy_rows(
            await session.execute(
                select(_LEGACY_SCENARIO_EPISODES).where(
                    _LEGACY_SCENARIO_EPISODES.c.scenario_id == scenario.id
                )
            )
        )
        old_by_node = {row.knowledge_node_id: row for row in old_episodes if row.knowledge_node_id}
        episodes: list[dict[str, Any]] = []
        for position, node in enumerate(assets["episodes"]):
            old = old_by_node.get(node.id)
            body = _node_body(
                node,
                node_index,
                children_by_parent=children_by_parent,
            )
            episodes.append(
                {
                    "id": str(node.id),
                    "title": node.title,
                    "body": body,
                    "sort_hint": float(node.sort_order or position),
                    "plot": old.synopsis_paragraph if old else None,
                    "summary": old.synopsis_sentence if old else None,
                    "status": _episode_status(old.status if old else None, body=body),
                    "body_sha256": _sha256(body),
                    "char_count": len(body),
                    "source_node_id": str(node.id),
                }
            )

        characters: list[dict[str, Any]] = []
        for node in assets["characters"]:
            body = _node_body(
                node,
                node_index,
                children_by_parent=children_by_parent,
            )
            characters.append(
                {
                    "id": str(node.id),
                    "name": node.title,
                    "description": body,
                    "position": float(node.sort_order or 0),
                    "source_node_id": str(node.id),
                }
            )

        notes: list[dict[str, Any]] = []
        for category in ("settings", "materials"):
            for node in assets[category]:
                body = _node_body(
                    node,
                    node_index,
                    children_by_parent=children_by_parent,
                )
                notes.append(
                    {
                        "id": str(node.id),
                        "title": node.title,
                        "content": body,
                        "ai_mode": "keyword",
                        "source_node_id": str(node.id),
                    }
                )
        canon_rows = _legacy_rows(
            await session.execute(
                select(_LEGACY_SCENARIO_CANON).where(
                    _LEGACY_SCENARIO_CANON.c.scenario_id == scenario.id
                )
            )
        )
        for row in canon_rows:
            notes.append(
                {
                    "id": str(row.id),
                    "title": _canon_note_title(row.category, row.fact),
                    "content": str(row.fact or ""),
                    "ai_mode": "keyword",
                    "source_node_id": None,
                }
            )

        opening = str(getattr(scenario, "opening_text", "") or "")
        if opening:
            opening_id = uuid5(_OPENING_NOTE_NAMESPACE, str(scenario.id))
            notes.append(
                {
                    "id": str(opening_id),
                    "title": "オープニングテキスト",
                    "content": opening,
                    # §11.2: オープニングテキストだけは常に文脈へ入れる。
                    "ai_mode": "always",
                    "source_node_id": None,
                }
            )

        active_descendant_count = sum(
            1
            for node in node_index.values()
            if node.id != root.id
            and node.archived_at is None
            and _is_under_root(node, node_index, root.id)
        ) if root is not None else 0

        plans.append(
            {
                "scenario_id": str(scenario.id),
                "owner_id": str(owner) if owner else None,
                "root_node_id": str(root.id) if root else None,
                "root_archived": bool(root.archived_at) if root else False,
                "active_descendant_count": active_descendant_count,
                "title": scenario.title,
                "synopsis": _synopsis(scenario),
                "style_guide": _style_guide(scenario),
                "kind": "trpg" if scenario.scenario_kind == "trpg" else "novel",
                "status": _work_status(episodes),
                "episodes": episodes,
                "characters": characters,
                "notes": notes,
                "docs_node_ids": [str(item) for item in sorted(archive_ids, key=str)],
                "source_counts": {
                    "episodes": len(episodes),
                    "characters": len(characters),
                    "notes": len(notes),
                    "body_chars": sum(item["char_count"] for item in episodes),
                },
            }
        )
    return plans


def _plan_report(plan: dict[str, Any]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for row in plan["episodes"]:
        key = str(row["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    ai_mode_counts: dict[str, int] = {}
    for row in plan["notes"]:
        key = str(row.get("ai_mode") or "keyword")
        ai_mode_counts[key] = ai_mode_counts.get(key, 0) + 1
    return {
        "scenario_id": plan["scenario_id"],
        "owner_id": plan["owner_id"],
        "title": plan["title"],
        "kind": plan["kind"],
        "work_status": plan["status"],
        "synopsis": plan["synopsis"],
        "episode_status_counts": dict(sorted(status_counts.items())),
        "note_ai_modes": dict(sorted(ai_mode_counts.items())),
        "episodes": plan["source_counts"]["episodes"],
        "characters": plan["source_counts"]["characters"],
        "notes": plan["source_counts"]["notes"],
        "body_chars": plan["source_counts"]["body_chars"],
        "episode_sha256": {
            row["id"]: row["body_sha256"] for row in plan["episodes"]
        },
        "docs_nodes": len(plan["docs_node_ids"]),
        "root_archived": bool(plan.get("root_archived")),
        "active_descendants": int(plan.get("active_descendant_count") or 0),
        "warning": "owner_id がないため --apply ではスキップ" if not plan["owner_id"] else None,
    }


async def _ensure_revision(session: Any, episode: StoryEpisode) -> None:
    existing = await session.scalar(
        select(StoryEpisodeRevision).where(
            StoryEpisodeRevision.episode_id == episode.id,
            StoryEpisodeRevision.origin == "import",
        )
    )
    if existing is None:
        await StoryRevisionService(session).create_revision(
            episode,
            origin="import",
            message="Docsから移行",
            created_by="user",
            force=True,
        )


async def _apply_plan(session: Any, plan: dict[str, Any]) -> dict[str, Any]:
    if not plan["owner_id"]:
        return {"scenario_id": plan["scenario_id"], "status": "skipped", "reason": "owner_id_missing"}
    owner_id = UUID(plan["owner_id"])
    scenario_id = UUID(plan["scenario_id"])
    work = await session.get(StoryWork, scenario_id)
    if work is None:
        work = StoryWork(
            id=scenario_id,
            user_id=owner_id,
            title=plan["title"],
            synopsis=plan["synopsis"],
            style_guide=plan["style_guide"],
            kind=plan["kind"],
            status=plan["status"],
        )
        session.add(work)
    else:
        work.title = plan["title"]
        work.synopsis = plan["synopsis"]
        work.style_guide = plan["style_guide"]
        work.kind = plan["kind"]
        work.status = plan["status"]
    await session.flush()

    episode_ids: list[UUID] = []
    for item in plan["episodes"]:
        episode_id = UUID(item["id"])
        episode = await session.get(StoryEpisode, episode_id)
        if episode is None:
            episode = StoryEpisode(id=episode_id, work_id=work.id, title=item["title"])
            session.add(episode)
        episode.work_id = work.id
        episode.title = item["title"]
        episode.body = item["body"]
        episode.body_etag = _etag(item["body"])
        episode.char_count = item["char_count"]
        episode.sort_hint = item["sort_hint"]
        episode.plot = item["plot"]
        episode.summary = item["summary"]
        episode.status = item["status"] or "unwritten"
        await session.flush()
        index = await session.get(StorySearchIndex, episode.id)
        if index is None:
            session.add(
                StorySearchIndex(
                    episode_id=episode.id,
                    work_id=work.id,
                    title=episode.title,
                    body_plain=item["body"],
                )
            )
        else:
            index.title = episode.title
            index.body_plain = item["body"]
        await _ensure_revision(session, episode)
        episode_ids.append(episode.id)

    # 既存の移行線形リンクだけを再構成し、他の手動分岐は残す。
    for left, right in zip(episode_ids, episode_ids[1:]):
        link = await session.scalar(
            select(StoryLink).where(
                StoryLink.from_episode_id == left,
                StoryLink.to_episode_id == right,
            )
        )
        if link is None:
            session.add(
                StoryLink(
                    work_id=work.id,
                    from_episode_id=left,
                    to_episode_id=right,
                    position=0,
                    is_primary=True,
                )
            )
    work.start_episode_id = episode_ids[0] if episode_ids else None

    for item in plan["characters"]:
        character_id = UUID(item["id"])
        character = await session.get(StoryCharacter, character_id)
        if character is None:
            character = StoryCharacter(
                id=character_id,
                user_id=owner_id,
                name=item["name"],
                description=item["description"],
                ai_mode="keyword",
                keywords=[item["name"]],
            )
            session.add(character)
        relation = await session.scalar(
            select(StoryWorkCharacter).where(
                StoryWorkCharacter.work_id == work.id,
                StoryWorkCharacter.character_id == character.id,
            )
        )
        if relation is None:
            session.add(
                StoryWorkCharacter(
                    work_id=work.id,
                    character_id=character.id,
                    position=item["position"],
                )
            )

    for item in plan["notes"]:
        note_id = UUID(item["id"])
        ai_mode = str(item.get("ai_mode") or "keyword")
        note = await session.get(StoryNote, note_id)
        if note is None:
            session.add(
                StoryNote(
                    id=note_id,
                    work_id=work.id,
                    title=item["title"],
                    content=item["content"],
                    ai_mode=ai_mode,
                    keywords=[item["title"]],
                )
            )
        else:
            note.work_id = work.id
            note.title = item["title"]
            note.content = item["content"]
            note.ai_mode = ai_mode

    await session.flush()
    return {"scenario_id": plan["scenario_id"], "status": "applied", "episodes": len(episode_ids)}


async def migrate(*, apply: bool, work_id: UUID | None = None) -> dict[str, Any]:
    async with await get_db_session() as session:
        plans = await build_migration_plan(session, work_id=work_id)
        if not apply:
            await session.rollback()
            return {
                "mode": "dry-run",
                "works": [_plan_report(plan) for plan in plans],
            }
        results: list[dict[str, Any]] = []
        try:
            for plan in plans:
                results.append(await _apply_plan(session, plan))
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return {
            "mode": "apply",
            "next_step": "--verify を実行し、通ってから --archive-docs を実行する",
            "works": results,
        }


async def archive_docs_only(*, work_id: UUID | None = None) -> dict[str, Any]:
    """移行済み Docs subtree を archive する（``--apply`` → ``--verify`` の後段）。

    ``--apply`` と同時に実行しないのは、archive すると ``--verify`` が期待値を
    再構成できなくなるため（§11.4）。ここでは story 側に対応する作品と章が
    揃っていることを確認し、1 件でも不足していれば何も archive せずに終える。
    """

    async with await get_db_session() as session:
        plans = await build_migration_plan(session, work_id=work_id)
        archived: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        try:
            now = datetime.utcnow()
            for plan in plans:
                work = await session.get(StoryWork, UUID(plan["scenario_id"]))
                if work is None:
                    blocked.append(
                        {"scenario_id": plan["scenario_id"], "reason": "work_missing"}
                    )
                    continue
                story_episodes = int(
                    await session.scalar(
                        select(func.count(StoryEpisode.id)).where(
                            StoryEpisode.work_id == work.id,
                            StoryEpisode.archived_at.is_(None),
                        )
                    )
                    or 0
                )
                if story_episodes != len(plan["episodes"]):
                    blocked.append(
                        {
                            "scenario_id": plan["scenario_id"],
                            "reason": "episode_count_mismatch",
                            "expected": len(plan["episodes"]),
                            "actual": story_episodes,
                        }
                    )
                    continue
                count = 0
                for raw_id in plan["docs_node_ids"]:
                    node = await session.get(KnowledgeNode, UUID(raw_id))
                    if node is not None and node.archived_at is None:
                        node.archived_at = now
                        count += 1
                archived.append(
                    {
                        "scenario_id": plan["scenario_id"],
                        "title": plan["title"],
                        "status": "archived",
                        "nodes": count,
                    }
                )
            if blocked:
                await session.rollback()
                return {
                    "mode": "archive-docs",
                    "ok": False,
                    "hint": "先に --apply と --verify を通す",
                    "archived": [],
                    "blocked": blocked,
                }
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return {"mode": "archive-docs", "ok": True, "archived": archived, "blocked": []}


async def verify(*, work_id: UUID | None = None) -> dict[str, Any]:
    """移行後の件数、文字数、sha、リンク、revision、index を突合する。"""

    async with await get_db_session() as session:
        # 期待値は active Docs ノードだけから作る。--archive-docs を先に走らせると
        # 期待値を再構成できないため、その場合は黙って通さず issue を立てる。
        plans = await build_migration_plan(session, work_id=work_id)
        issues: list[dict[str, Any]] = []
        reports: list[dict[str, Any]] = []
        source_count_query = select(func.count()).select_from(_LEGACY_SCENARIOS)
        story_count_query = select(func.count()).select_from(StoryWork).where(
            StoryWork.archived_at.is_(None)
        )
        if work_id is not None:
            source_count_query = source_count_query.where(_LEGACY_SCENARIOS.c.id == work_id)
            story_count_query = story_count_query.where(StoryWork.id == work_id)
        source_work_count = int(await session.scalar(source_count_query) or 0)
        story_work_count = int(await session.scalar(story_count_query) or 0)
        if source_work_count != story_work_count:
            issues.append(
                {
                    "kind": "work_count",
                    "expected": source_work_count,
                    "actual": story_work_count,
                }
            )
        for plan in plans:
            scenario_id = UUID(plan["scenario_id"])
            work = await session.get(StoryWork, scenario_id)
            report = _plan_report(plan)
            if plan.get("root_archived"):
                # archive 済み root からは active な期待値を組み立てられない。
                # ここで比較を続けると全エピソードが偽の sha 不一致になる。
                issues.append(
                    {
                        "scenario_id": plan["scenario_id"],
                        "kind": "docs_root_archived",
                        "hint": "--verify は --archive-docs より前に実行する",
                    }
                )
                report["story_work"] = bool(work)
                reports.append(report)
                continue
            report.update(
                {
                    "story_work": bool(work),
                    "story_episodes": 0,
                    "story_body_chars": 0,
                    "story_links": 0,
                    "story_characters": 0,
                    "story_revisions": 0,
                    "story_search_index": 0,
                    "start_episode_id": str(work.start_episode_id) if work and work.start_episode_id else None,
                }
            )
            if work is None:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "work_missing"})
                reports.append(report)
                continue
            episodes = list(
                (
                    await session.scalars(
                        select(StoryEpisode).where(
                            StoryEpisode.work_id == work.id,
                            StoryEpisode.archived_at.is_(None),
                        )
                    )
                ).all()
            )
            report["story_episodes"] = len(episodes)
            report["story_body_chars"] = sum(int(row.char_count or 0) for row in episodes)
            expected_episode_count = len(plan["episodes"])
            expected_body_chars = int(plan["source_counts"]["body_chars"])
            if len(episodes) != len(plan["episodes"]):
                issues.append({"scenario_id": plan["scenario_id"], "kind": "episode_count", "expected": expected_episode_count, "actual": len(episodes)})
            if report["story_body_chars"] != expected_body_chars:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "body_char_count", "expected": expected_body_chars, "actual": report["story_body_chars"]})
            for item in plan["episodes"]:
                episode = await session.get(StoryEpisode, UUID(item["id"]))
                if episode is None or episode.archived_at is not None:
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "episode_missing"})
                    continue
                if episode.char_count != item["char_count"]:
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "episode_char_count", "expected": item["char_count"], "actual": episode.char_count})
                if episode.body_etag != _etag(item["body"]):
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "body_sha", "expected": _etag(item["body"]), "actual": episode.body_etag})
                revisions = list(
                    (
                        await session.scalars(
                            select(StoryEpisodeRevision).where(
                                StoryEpisodeRevision.episode_id == episode.id,
                                StoryEpisodeRevision.origin == "import",
                            )
                        )
                    ).all()
                )
                if len(revisions) != 1:
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "import_revision_count", "actual": len(revisions)})
                elif revisions[0].body_sha256 != item["body_sha256"]:
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "import_revision_sha", "expected": item["body_sha256"], "actual": revisions[0].body_sha256})
                index = await session.get(StorySearchIndex, episode.id)
                if index is None:
                    issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "search_index_missing"})
                else:
                    if index.title != episode.title or index.body_plain != item["body"]:
                        issues.append({"scenario_id": plan["scenario_id"], "episode_id": item["id"], "kind": "search_index_content"})
            import_revision_count = int(
                await session.scalar(
                    select(func.count(StoryEpisodeRevision.id))
                    .join(StoryEpisode, StoryEpisode.id == StoryEpisodeRevision.episode_id)
                    .where(
                        StoryEpisode.work_id == work.id,
                        StoryEpisodeRevision.origin == "import",
                    )
                )
                or 0
            )
            report["story_revisions"] = import_revision_count
            if import_revision_count != expected_episode_count:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "import_revision_total", "expected": expected_episode_count, "actual": import_revision_count})
            index_count = int(
                await session.scalar(
                    select(func.count(StorySearchIndex.episode_id)).where(
                        StorySearchIndex.work_id == work.id
                    )
                )
                or 0
            )
            report["story_search_index"] = index_count
            if index_count != len(episodes):
                issues.append({"scenario_id": plan["scenario_id"], "kind": "search_index_count", "expected": len(episodes), "actual": index_count})
            character_links = list(
                (
                    await session.scalars(
                        select(StoryWorkCharacter).where(
                            StoryWorkCharacter.work_id == work.id
                        )
                    )
                ).all()
            )
            report["story_characters"] = len(character_links)
            expected_characters = len(plan["characters"])
            if len(character_links) != expected_characters:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "character_count", "expected": expected_characters, "actual": len(character_links)})
            links = list(
                (
                    await session.scalars(
                        select(StoryLink).where(StoryLink.work_id == work.id)
                    )
                ).all()
            )
            report["story_links"] = len(links)
            expected_links = max(0, len(plan["episodes"]) - 1)
            if len(links) != expected_links:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "link_count", "expected": expected_links, "actual": len(links)})
            if expected_episode_count:
                if work.start_episode_id is None:
                    issues.append({"scenario_id": plan["scenario_id"], "kind": "start_missing"})
                else:
                    incoming = int(
                        await session.scalar(
                            select(func.count(StoryLink.id)).where(
                                StoryLink.work_id == work.id,
                                StoryLink.to_episode_id == work.start_episode_id,
                            )
                        )
                        or 0
                    )
                    if incoming != 0:
                        issues.append({"scenario_id": plan["scenario_id"], "kind": "start_has_incoming", "actual": incoming})
            elif work.start_episode_id is not None:
                issues.append({"scenario_id": plan["scenario_id"], "kind": "unexpected_start", "actual": str(work.start_episode_id)})
            reports.append(report)
        await session.rollback()
        return {
            "ok": not issues,
            "source_work_count": source_work_count,
            "story_work_count": story_work_count,
            "issues": issues,
            "works": reports,
        }


#: 排他モード。1 つずつ、この順で実行する。
EXCLUSIVE_MODES = ("--apply", "--verify", "--archive-docs")
PROCEDURE_HINT = (
    "手順は --apply → --verify → --archive-docs の順に 1 つずつ実行する。"
    "--archive-docs を先に走らせると --verify が期待値（active Docs ノード）を"
    "再構成できなくなるため、同時指定は禁止する。"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=PROCEDURE_HINT,
    )
    parser.add_argument("--apply", action="store_true", help="story_* へ書き込む（省略時は dry-run）")
    parser.add_argument("--verify", action="store_true", help="移行後の件数・文字数・sha を read-only 検証する（--apply の後、--archive-docs の前）")
    parser.add_argument(
        "--archive-docs",
        action="store_true",
        help="--verify を通した後に移行済み Docs subtree を archive する（--apply / --verify とは同時指定不可）",
    )
    parser.add_argument("--work-id", type=UUID, default=None, help="対象作品 UUID（省略時は全作品）")
    return parser.parse_args(argv)


def conflicting_modes(args: argparse.Namespace) -> list[str]:
    """同時指定された排他モードを返す（1 つ以下なら空リスト）。"""

    selected = [
        name
        for name, enabled in (
            ("--apply", bool(args.apply)),
            ("--verify", bool(args.verify)),
            ("--archive-docs", bool(args.archive_docs)),
        )
        if enabled
    ]
    return selected if len(selected) > 1 else []


async def main_async(args: argparse.Namespace) -> int:
    conflicts = conflicting_modes(args)
    if conflicts:
        print(
            f"{' と '.join(conflicts)} は同時指定できません。{PROCEDURE_HINT}",
            file=sys.stderr,
        )
        return 2
    if args.verify:
        report = await verify(work_id=args.work_id)
    elif args.archive_docs:
        report = await archive_docs_only(work_id=args.work_id)
    else:
        report = await migrate(apply=args.apply, work_id=args.work_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.verify or args.archive_docs:
        return 0 if report.get("ok", False) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
