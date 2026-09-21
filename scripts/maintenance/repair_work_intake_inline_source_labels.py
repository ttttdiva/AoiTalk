"""Repair generated Work Intake inline-source labels without changing edges.

The default operation is a read-only plan.  Applying a plan requires both
``--apply`` and the SHA-256 digest printed by the matching dry-run.  The
mutation path deliberately updates the inline node columns directly instead
of calling ``DocsGraphService.update_node``; that service method synchronizes
reference edges and would violate the identity-preservation contract.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory.database import get_database_manager
from src.memory.models import KnowledgeEdge, KnowledgeNode
from src.services.docs_graph_service import DocsGraphService
from src.services.work_intake_docs_service import _inline_source_display_label


WORK_INTAKE_INLINE_FORMAT = "work_intake_inline_source"
WORK_INTAKE_SYSTEM_PREFIX = "project_inbox_item:"
INLINE_REF_RELATION = "inline_ref"
MAX_LIMIT = 10_000
_UUID_TEXT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
_ITEM_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _generated_work_intake_key_parts(value: object) -> dict[str, object] | None:
    """Parse the exact key grammar emitted by WorkIntakeDocsService."""

    parts = str(value or "").strip().split(":")
    # project_inbox_item:<project UUID>:<item sha256>:document:<generation UUID>:
    # section:<n>:block:<n>[:child:<n>...]:source:<n>
    if len(parts) < 11 or parts[0] != "project_inbox_item" or parts[3] != "document":
        return None
    try:
        project_id = UUID(parts[1])
        generation_id = UUID(parts[4])
    except (IndexError, ValueError):
        return None
    if not _ITEM_HASH_RE.fullmatch(parts[2].lower()):
        return None
    if parts[5] != "section" or not parts[6].isdigit() or parts[7] != "block" or not parts[8].isdigit():
        return None
    path_parts = parts[9:-2]
    if len(path_parts) % 2 != 0 or any(
        path_parts[index] != "child" or not path_parts[index + 1].isdigit()
        for index in range(0, len(path_parts), 2)
    ):
        return None
    if parts[-2] != "source" or not parts[-1].isdigit():
        return None
    return {
        "project_id": str(project_id),
        "item_hash": parts[2].lower(),
        "generation_id": str(generation_id),
        "source_index": int(parts[-1]),
    }


def is_generated_work_intake_inline_node(node: Any) -> bool:
    """Return whether a node has the generated Work Intake source identity."""

    return _generated_work_intake_key_parts(getattr(node, "system_key", None)) is not None


def matches_repair_candidate(
    node: Any,
    edges: Sequence[Any],
    source: Any | None,
) -> bool:
    """Pure matcher used by the dry-run plan and unit tests.

    A candidate is repairable only when its generated identity, format, one
    live inline reference, and live source can all be established.  Anything
    ambiguous is intentionally excluded from the plan.
    """

    body_json = getattr(node, "body_json", None)
    if getattr(node, "archived_at", None) is not None:
        return False
    key_parts = _generated_work_intake_key_parts(getattr(node, "system_key", None))
    if key_parts is None:
        return False
    node_project_id = getattr(node, "project_id", None)
    if node_project_id is not None and str(node_project_id) != key_parts["project_id"]:
        return False
    if not isinstance(body_json, dict):
        return False
    if body_json.get("format") != WORK_INTAKE_INLINE_FORMAT:
        return False
    if len(edges) != 1:
        return False
    edge = edges[0]
    if (
        getattr(edge, "source_node_id", None) != getattr(node, "id", None)
        or getattr(edge, "relation_type", None) != INLINE_REF_RELATION
    ):
        return False
    if source is None or getattr(source, "archived_at", None) is not None:
        return False
    return getattr(edge, "target_node_id", None) == getattr(source, "id", None)


def expected_inline_source_title(source: Any, *, limit: int = 120) -> str:
    """Build the bounded display-only title while preserving source identity."""

    return f"[[node:{source.id}|根拠: {_inline_source_display_label(source, limit=limit)}]]"


def _identity_snapshot(
    *,
    node: Any,
    edges: Sequence[Any],
    source: Any,
) -> dict[str, Any]:
    return {
        "inline": {
            "id": str(node.id),
            "project_id": str(node.project_id) if getattr(node, "project_id", None) is not None else None,
            "system_key": str(node.system_key or ""),
            "body_json": copy.deepcopy(node.body_json),
            "archived_at": node.archived_at,
        },
        "edges": [
            {
                "id": str(edge.id),
                "source_node_id": str(edge.source_node_id),
                "target_node_id": str(edge.target_node_id),
                "relation_type": str(edge.relation_type),
            }
            for edge in sorted(edges, key=lambda item: str(item.id))
        ],
        "source": {
            "id": str(source.id),
            "title": str(source.title or ""),
            "body_json": copy.deepcopy(source.body_json),
        },
    }


def _plan_before_state(*, node: Any, edges: Sequence[Any], source: Any) -> dict[str, Any]:
    """Return JSON-safe evidence for Director review before any apply."""

    identity = _identity_snapshot(node=node, edges=edges, source=source)
    inline = identity["inline"]
    inline.update(
        {
            "title": str(node.title or ""),
            "body_text": str(node.body_text or ""),
            "created_by": str(node.created_by) if getattr(node, "created_by", None) is not None else None,
            "updated_by": str(node.updated_by) if getattr(node, "updated_by", None) is not None else None,
        }
    )
    inline["archived_at"] = str(inline["archived_at"]) if inline["archived_at"] is not None else None
    return identity


def _plan_digest(changes: Sequence[dict[str, Any]]) -> str:
    payload = {
        "version": 1,
        "changes": list(changes),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _load_edges(
    session: Any,
    node_id: UUID,
    *,
    for_update: bool = False,
) -> list[KnowledgeEdge]:
    query = select(KnowledgeEdge).where(
        KnowledgeEdge.source_node_id == node_id,
        KnowledgeEdge.relation_type == INLINE_REF_RELATION,
    ).order_by(KnowledgeEdge.id)
    if for_update:
        query = query.with_for_update()
    result = await session.execute(query)
    return list(result.scalars().all())


async def build_plan(session: Any, *, limit: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if not isinstance(limit, int) or limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    stats = {"scanned": 0, "proposed": 0, "changed": 0, "skipped": 0}
    changes: list[dict[str, Any]] = []
    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.archived_at.is_(None),
            KnowledgeNode.system_key.like("project_inbox_item:%:document:%:source:%"),
        )
        .order_by(KnowledgeNode.updated_at, KnowledgeNode.id)
        .limit(limit)
    )
    nodes = list(result.scalars().all())
    for node in nodes:
        edges = await _load_edges(session, node.id)
        source = None
        if len(edges) == 1:
            source = await session.get(KnowledgeNode, edges[0].target_node_id)
        if not matches_repair_candidate(node, edges, source):
            stats["skipped"] += 1
            continue
        stats["scanned"] += 1
        expected = expected_inline_source_title(source)
        if str(node.title or "") == expected:
            continue
        actor = getattr(node, "updated_by", None) or getattr(node, "created_by", None)
        if actor is None:
            stats["skipped"] += 1
            continue
        stats["proposed"] += 1
        changes.append(
            {
                "node_id": str(node.id),
                "source_node_id": str(source.id),
                "actor_id": str(actor),
                "old_title": str(node.title or ""),
                "old_body_text": str(node.body_text or ""),
                "new_title": expected,
                "new_body_text": expected,
                "before": _plan_before_state(node=node, edges=edges, source=source),
                "invariants": {
                    "inline_node_id": str(node.id),
                    "edge_identity": [
                        "id",
                        "source_node_id",
                        "target_node_id",
                        "relation_type",
                    ],
                    "source_identity": ["id", "title", "body_json"],
                    "no_edge_delete_or_recreate": True,
                },
            }
        )
    return stats, changes


async def _apply_change(session: Any, change: dict[str, Any]) -> None:
    node_id = UUID(change["node_id"])
    source_node_id = UUID(change["source_node_id"])
    actor_id = UUID(change["actor_id"])
    result = await session.execute(
        select(KnowledgeNode)
        .where(KnowledgeNode.id == node_id, KnowledgeNode.archived_at.is_(None))
        .with_for_update()
    )
    node = result.scalar_one_or_none()
    if node is None or str(node.title or "") != change["old_title"]:
        raise RuntimeError(f"repair plan is stale for inline node {node_id}")

    actor = getattr(node, "updated_by", None) or getattr(node, "created_by", None)
    if actor is None or str(actor) != change["actor_id"]:
        raise RuntimeError(f"repair plan actor is stale for inline node {node_id}")

    edges = await _load_edges(session, node_id, for_update=True)
    source_result = await session.execute(
        select(KnowledgeNode)
        .where(KnowledgeNode.id == source_node_id, KnowledgeNode.archived_at.is_(None))
        .with_for_update()
    )
    source = source_result.scalar_one_or_none()
    if not matches_repair_candidate(node, edges, source):
        raise RuntimeError(f"repair candidate identity changed for inline node {node_id}")
    expected = expected_inline_source_title(source)
    if expected != change["new_title"]:
        raise RuntimeError(f"repair plan title changed for inline node {node_id}")

    before = _identity_snapshot(node=node, edges=edges, source=source)
    node.title = expected
    node.body_text = expected
    node.updated_at = datetime.utcnow()
    node.updated_by = actor_id
    await session.flush()
    await DocsGraphService(session).upsert_search_index(node)
    await session.flush()

    after_edges = await _load_edges(session, node_id, for_update=True)
    after_source = await session.get(KnowledgeNode, source_node_id)
    if after_source is None:
        raise RuntimeError(f"source node disappeared for inline node {node_id}")
    after = _identity_snapshot(node=node, edges=after_edges, source=after_source)
    if after != before:
        raise RuntimeError(f"edge/source identity changed for inline node {node_id}")


async def run(
    *,
    apply: bool,
    expected_plan_sha256: str | None,
    limit: int,
) -> int:
    if apply and not expected_plan_sha256:
        raise SystemExit("--apply requires --expected-plan-sha256")
    if not apply and expected_plan_sha256:
        raise SystemExit("--expected-plan-sha256 requires --apply")
    session = await get_database_manager().get_session()
    try:
        stats, changes = await build_plan(session, limit=limit)
        plan_sha256 = _plan_digest(changes)
        if apply:
            expected = str(expected_plan_sha256 or "").strip().lower()
            if expected != plan_sha256:
                raise SystemExit(
                    f"plan digest mismatch: expected {expected}, actual {plan_sha256}"
                )
            for change in changes:
                await _apply_change(session, change)
                stats["changed"] += 1
            if changes:
                await session.commit()
        print(
            json.dumps(
                {
                    "apply": apply,
                    "plan_sha256": plan_sha256,
                    "stats": stats,
                    "changes": changes[:100],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    return asyncio.run(
        run(
            apply=args.apply,
            expected_plan_sha256=args.expected_plan_sha256,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
