"""クリップ取り込み先として使えるDocsノードのサーバー権威ポリシー。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.models import KnowledgeNode, DocsLibrary


FILM_ROOT_SYSTEM_KEY = "foam_source_grounded_v1:root.Film"
_MAX_ANCESTRY_HOPS = 64


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value or ""))
    except (TypeError, ValueError):
        return None


async def is_film_docs_node(session: Any, node: KnowledgeNode) -> bool:
    """node自身・root_page・親階層のいずれかがFilmルートなら拒否する。"""

    pending: list[KnowledgeNode] = [node]
    seen: set[UUID] = set()
    while pending and len(seen) < _MAX_ANCESTRY_HOPS:
        current = pending.pop()
        if current.id in seen:
            continue
        seen.add(current.id)
        if getattr(current, "system_key", None) == FILM_ROOT_SYSTEM_KEY:
            return True
        related_ids = {
            related_id
            for related_id in (
                getattr(current, "root_page_id", None),
                getattr(current, "parent_id", None),
            )
            if related_id is not None and related_id not in seen
        }
        for related_id in related_ids:
            related = await session.get(KnowledgeNode, related_id)
            if related is None or related.docs_library_id != node.docs_library_id:
                return True
            pending.append(related)
    # 階層上限へ達して判定し切れない場合は、誤許可せずFilm同様に拒否する。
    return bool(pending)


async def _resolve_target_node(
    session: Any,
    docs_library_id: UUID,
    raw: dict[str, Any],
) -> KnowledgeNode | None:
    node_id = _as_uuid(raw.get("node_id"))
    node = await session.get(KnowledgeNode, node_id) if node_id is not None else None
    if node is not None and node.docs_library_id == docs_library_id:
        return node

    system_key = str(raw.get("node_system_key") or "").strip()
    if not system_key:
        return None
    result = await session.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.docs_library_id == docs_library_id,
            KnowledgeNode.system_key == system_key,
        )
    )
    return result.scalar_one_or_none()


async def filter_film_clip_ingest_targets(
    session: Any,
    user_id: UUID,
    raw_targets: Any,
) -> Any:
    """設定配列から、DB上でFilm配下に属する取り込み先だけを除去する。"""

    if not isinstance(raw_targets, list):
        return raw_targets
    workspace_result = await session.execute(
        select(DocsLibrary.id).where(
            DocsLibrary.owner_user_id == user_id
        )
    )
    docs_library_id = workspace_result.scalar_one_or_none()
    if docs_library_id is None:
        return raw_targets

    allowed: list[Any] = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            allowed.append(raw)
            continue
        node = await _resolve_target_node(session, docs_library_id, raw)
        if node is not None and await is_film_docs_node(session, node):
            continue
        allowed.append(raw)
    return allowed


async def sanitize_clip_ingest_settings(
    session: Any,
    user_id: UUID,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """設定保存前にFilm配下のクリップ取り込み先をサーバー側で除去する。"""

    sanitized = deepcopy(settings)
    clip_ingest = sanitized.get("clip_ingest")
    if not isinstance(clip_ingest, dict) or "targets" not in clip_ingest:
        return sanitized
    clip_ingest["targets"] = await filter_film_clip_ingest_targets(
        session,
        user_id,
        clip_ingest.get("targets"),
    )
    return sanitized
