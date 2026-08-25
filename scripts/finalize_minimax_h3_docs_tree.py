"""Finalize the user-facing MiniMax H3 Docs tree after the provenance repair.

This is deliberately a second-stage operation.  The historical repair in
``repair_minimax_h3_clip_ingest.py`` is never rewritten and this command does
not reuse its ``--apply`` path.  Without ``--apply`` the command opens a
read-only PostgreSQL transaction, builds a complete snapshot, and writes a
reproducible proposal under ``docs/audits/minimax_h3_semantic_finalize_20260822``.

The apply path is fail-closed: it requires the dry-run manifest, locks the
root and every affected node/wrapper, compares the locked state with the
manifest, performs all changes through ``DocsGraphService`` in one
transaction, and verifies the committed tree in a separate session.  A second
read-only invocation returns ``already_applied`` without creating more nodes.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import multiprocessing
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus
from uuid import UUID, uuid5

from dotenv import load_dotenv
from sqlalchemy import literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if multiprocessing.current_process().name == "MainProcess":
    from src.memory.models import (  # noqa: E402
        DocsLibrary,
        KnowledgeAttachment,
        KnowledgeEdge,
        KnowledgeNode,
        KnowledgeNodePlacement,
        KnowledgeRevision,
    )
    from src.services.docs_acl import can_write_node  # noqa: E402
    from src.services.docs_graph_service import DocsGraphService  # noqa: E402
    # Do not monkeypatch the graph notifier at import time.  Tests and callers
    # may import this module in a long-lived server process; global mutation
    # would silently disable notifications for unrelated Docs writes.


ROOT_ID = UUID("4a3c2921-1a3a-4242-aab3-74b5794e9d7f")
AUDIT_DIR = ROOT_DIR / "docs" / "audits" / "minimax_h3_semantic_finalize_20260822"
DEFAULT_MANIFEST = AUDIT_DIR / "dry_run.json"
NEW_TOPIC_NAMESPACE = ROOT_ID
FINALIZATION_SCHEMA_VERSION = 1

G13_HASHES = (
    "2a6f5e05771afa5560275f066a4ab6624cf0b8e8d6cc58f16a2abb6a968fc224",
    "99d7e6a4dab022a557436f52f10950ad4a8b1b81e722536531fe05ef70cccbfb",
)

# The IDs below are the IDs proven by the historical repair audit.  Keeping
# them in code makes an accidental cross-root or duplicate assignment fail
# closed instead of being inferred from titles or URL slugs.
GROUPS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    (
        (
            "G01",
            {
                "revision_id": "1fce9f89-cdcf-42b7-8d4f-d62fe2acc7a3",
                "topic_id": "71019796-9c90-4f1b-9686-68c7f47cfdb5",
                "title": "Kijai MiniMax H3 / ComfyUIディスカッション",
                "selected_ids": [
                    "00bbfa4e-4be2-45ac-8b2d-466448a38c28",
                    "3670b7cd-1c61-48cc-aef6-da44c9575fb9",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G02",
            {
                "revision_id": "78d06fed-f051-47bd-a49e-f0d06c5aa8a9",
                "topic_id": "e67bfbf2-d3e4-4025-b953-4d1e6044cf1a",
                "title": "MiniMax H3 EZ/Turbo RTXアップスケール・LTX Refineワークフロー",
                "selected_ids": [
                    "17919297-c453-450c-b6b1-65cdcb96dd0d",
                    "c24b9fbb-8fc1-4ecd-a492-6c2e4f1cfa05",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G03",
            {
                "revision_id": "a5ac0653-5785-449d-83d7-0c49e00a403b",
                "topic_id": "2ba28918-e937-426b-85ec-7d256d190e48",
                "title": "H3LT X2 Riding POV I2Vモデル",
                "selected_ids": [
                    "37fd866f-b48a-4a0f-8241-a8e2747a7494",
                    "90fcb1f6-115b-4c78-bc87-4c7f1f1e1737",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
                "verbatim_hashes": [
                    "b4592ec241d852c1d03963011604e7992de5b61696dd54195324519b855c5419"
                ],
            },
        ),
        (
            "G04",
            {
                "revision_id": "98a56fc6-2ed9-40d0-b7e8-e5bf653cebba",
                "topic_id": "dcc72c6b-d026-4851-8084-19b8d095272a",
                "title": "MiniMax H3公式リポジトリ／prompt-writingスキル",
                "selected_ids": ["fcb72e7c-ea29-49bf-800c-7fc60072307c"],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G05",
            {
                "revision_id": "8f2e8768-eb31-4bd2-88a3-39906328e18e",
                "title": "MiniMax H3キャラクター入れ替えテスト",
                "selected_ids": [
                    "9c54a88b-a874-4e7a-a5a6-e9e9ed30acfe",
                    "cd245f05-32a6-48d8-8346-7d28bfc4cec0",
                    "e58f5e1c-d72c-4ba3-8b8c-b89fc3ac63b0",
                    "97337e1e-6b05-4fba-8961-091423722703",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
                "new_topic": True,
                "sort_order": 24.0,
            },
        ),
        (
            "G06",
            {
                "revision_id": "fc932396-c394-40ea-b96d-459400042de5",
                "topic_id": "8b5e5e3d-52b7-489d-ad29-762b94a220e3",
                "title": "ClipProj-MiniMax-H3埋め込みアダプタ",
                "selected_ids": [
                    "d92153ce-f247-4df0-9592-bfc465591120",
                    "bf83c21d-4447-4d47-96ce-c223d0d2a323",
                    "efb7117d-5a22-4f11-bab4-c6273a161102",
                    "5ee90035-f410-4eee-8c57-41e3c2b363d6",
                    "50610985-4885-45cc-8118-92a4ea4bb3ec",
                    "6ad736eb-55db-4edb-9164-2444b1ddb21c",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G07",
            {
                "revision_id": "af36de1f-d43e-45d0-a897-276d085271e5",
                "topic_id": "6ce7dbe4-adef-4cb8-a140-7efa8edbfb08",
                "title": "10eros Max INT8 Ref2VAモデル",
                "selected_ids": [
                    "6ce588d5-315b-4569-8ae6-3b88e338a19c",
                    "38726611-319e-48de-952e-020f1aaeeebb",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G08",
            {
                "revision_id": "e2c973ad-d805-44cc-87d4-b67853320179",
                "topic_id": "5db7f4c2-e7a6-4e3a-972a-c38d4b8b4575",
                "title": "MiniMax H3 Latent Upscaler配布",
                "selected_ids": [
                    "0c03bc37-18d4-456d-9420-b2b88ec207d2",
                    "b597836b-07d4-4fd3-a080-d601d28b81cc",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G09",
            {
                "revision_id": "12c506e1-ef0d-473a-b6b5-dc35e71985c2",
                "topic_id": "ed402bdb-385d-43c0-be1b-69555f40ae5f",
                "title": "DaSiWa MiniMax H3 continue-from-clipワークフロー",
                "selected_ids": [
                    "b00f3de7-ae61-452b-b6a8-435feed81b29",
                    "aef0ffc4-2d28-4b7b-8dcf-9b8f05156639",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G10",
            {
                "revision_id": "9d43c342-2f36-42fd-a38b-c507e40e94b0",
                "topic_id": "6cc990c1-686f-4903-af9d-8d4baea2fe47",
                "title": "MiniMax H3キャラクターLoRA学習（AI Toolkit）",
                "selected_ids": [
                    "d72527b1-1265-4226-9844-f5447cfbdd7b",
                    "332e518b-5d1b-4be2-a874-f73fa6edca1a",
                    "a7d46456-825d-402a-be3b-8de0bdf901e5",
                    "b6f737d3-d67d-409c-a900-85dfcfe0928f",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G11",
            {
                "revision_id": "d5e78d90-5e1a-4a76-a0eb-9a01c9c77868",
                "topic_id": "94b6330a-3073-40a4-a6d3-aed49b9a15e8",
                "title": "ComfyUI-H3-Multishotリポジトリ",
                "selected_ids": [
                    "74c07964-99a2-4492-99df-48f468037aac",
                    "c43d267e-08cc-4c56-86b1-3174e1e47b9e",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "medium",
            },
        ),
        (
            "G12",
            {
                "revision_id": "ae528836-3ea2-4491-99a3-494976265f5f",
                "title": "DaSiWaワークフロー補助リソース（Spectrum／Motion Context／Latent Upscaler）",
                "selected_ids": [
                    "d5336184-da99-4dc5-a6da-d78c463703d4",
                    "bb461cb4-18fc-4dd4-996d-8d1154e081c7",
                    "815e2dc4-bc71-401a-a959-f6da347c5a01",
                    "eb8a6065-b8f4-4c71-b911-3596bf486bfb",
                    "db291b46-5220-4281-8ead-f0cae79c4540",
                ],
                "boundary_confidence": "high",
                "semantic_confidence": "high",
                "new_topic": True,
                "sort_order": 46.0,
            },
        ),
        (
            "G13",
            {
                "revision_id": "08b7a1d3-3108-4ce1-85af-91487e2cd36a",
                "title": "MiniMax H3参照素材プロンプト構文（未整理）",
                "selected_ids": [],
                "boundary_confidence": "high",
                "semantic_confidence": "low",
                "new_topic": True,
                "sort_order": 23.5,
                "verbatim_hashes": list(G13_HASHES),
            },
        ),
        (
            "G14",
            {
                "revision_id": "7c795688-b3fd-425e-a518-26632570ecd3",
                "title": None,
                "selected_ids": [],
                "boundary_confidence": "high",
                "semantic_confidence": "none",
                "no_topic": True,
            },
        ),
    )
)

WRAPPER_IDS: dict[str, list[str]] = {
    "G01": ["7f034735-bfdb-4655-9afb-e9a5353ea62e", "973e01a4-6ed2-494a-8bca-8f0518c57d03"],
    "G02": ["ff3cd58f-51fa-4c48-aee9-05c515f5d882", "581dcc7b-016a-4c7d-a22c-a093e30fe8cd"],
    "G03": ["ce99c299-b02f-42ac-954f-c66782cebae2", "9f54b0b3-95c0-4b13-87b5-a65baa899217"],
    "G04": ["e2e5c724-bfda-498f-b7c4-8ef4318ea16d"],
    "G05": [
        "e46ec92c-83f2-4b20-9983-de609c1f89d5",
        "550a41f4-abcd-4ef0-8103-8cc57f5d2524",
        "a9c18863-89d6-446e-962c-cd435ae22b3d",
        "3116007b-4cf0-46e9-8215-04e1e871ad71",
    ],
    "G06": ["a5668969-8192-47e5-809b-7d7158102bd0", "02f07181-1a4b-4676-b2f1-0b54aa7960cc"],
    "G07": ["9246fa00-6578-4cc4-8861-052c182362aa", "1a6cfe34-4440-4116-b700-03a45922ab73"],
    "G08": ["d9b46785-715c-4bdd-b37f-47ed56576925", "f2a6dfdd-5785-468c-86f4-e34cd9b7919c"],
    "G09": ["fc8270de-5d3e-44cd-9284-32c998d0c8ef", "0e28e5c6-9885-423a-b9b0-180e71ac49e2"],
    "G10": ["985c5899-739a-4ba2-9a60-999993718c61"],
    "G11": ["e3ee3217-42c9-43d7-8d6f-4c560221e71d", "a9dde08d-a87a-407d-a9c5-45cc823f52d6"],
    "G12": [
        "e393e69a-18d2-4b7b-aa28-ac451442197a",
        "ec739db6-8f0a-4127-9f19-efaeae16c042",
        "aa674e28-6712-4762-aad7-614f9a73cdab",
        "7099430e-1e36-4c9b-9af1-809be63ec7a7",
        "fbb9e443-5239-44b6-b073-075f65ca921f",
    ],
}

SNAPSHOT_FIELDS = (
    "id",
    "parent_id",
    "root_page_id",
    "project_id",
    "docs_library_id",
    "system_key",
    "archived_at",
    "title",
    "sort_order",
    "created_at",
    "updated_at",
    "body_text",
    "body_json_digest",
)


def _json_default(value: Any) -> str:
    if isinstance(value, (UUID, datetime)):
        return str(value)
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)


def _uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def deterministic_topic_id(group_id: str) -> UUID:
    return uuid5(NEW_TOPIC_NAMESPACE, f"semantic-finalize:{group_id}")


def _node_snapshot(node: Any, *, include_body: bool = False) -> dict[str, Any]:
    body = copy.deepcopy(getattr(node, "body_json", None) or {})
    snapshot = {
        "id": str(node.id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "docs_library_id": str(node.docs_library_id) if node.docs_library_id else None,
        "system_key": node.system_key,
        "archived_at": _iso(node.archived_at),
        "title": node.title,
        "sort_order": float(node.sort_order or 0),
        "created_at": _iso(node.created_at),
        "updated_at": _iso(node.updated_at),
        "body_text": node.body_text or "",
        "body_json_digest": _digest(body),
    }
    if include_body:
        snapshot["body_json"] = body
    return snapshot


def _compare_snapshot(node: Any, expected: dict[str, Any], *, label: str = "node") -> list[str]:
    errors: list[str] = []
    actual = _node_snapshot(node)
    for key in SNAPSHOT_FIELDS:
        if key not in expected:
            errors.append(f"{label}: snapshot field missing: {key}")
            continue
        if key == "sort_order":
            try:
                equal = float(expected[key]) == float(actual[key])
            except (TypeError, ValueError):
                equal = False
        else:
            equal = expected[key] == actual[key]
        if not equal:
            errors.append(f"{label}: {key} changed")
    return errors


def _is_descendant(node: Any, ancestor_ids: Iterable[UUID], nodes: dict[UUID, Any]) -> bool:
    wanted = set(ancestor_ids)
    current = node
    seen: set[UUID] = set()
    while current is not None and current.parent_id is not None and current.id not in seen:
        if current.parent_id in wanted:
            return True
        seen.add(current.id)
        current = nodes.get(current.parent_id)
    return False


def _repair_marker(group_id: str, revision_id: str) -> dict[str, Any]:
    return {
        "repair_container_is_new": True,
        "original_topic_title_recovered": False,
        "label_semantics": "provenance_only",
        "source_revision_id": revision_id,
        "group_id": group_id,
    }


def _semantic_marker(group_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "group_id": group_id,
        "source_revision_id": str(spec["revision_id"]),
        "original_topic_title_recovered": False,
        "label_semantics": "curated_from_preserved_content",
        "boundary_confidence": spec["boundary_confidence"],
        "semantic_confidence": spec["semantic_confidence"],
    }


def _semantic_body(group_id: str, spec: dict[str, Any], *, verbatim: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "clip_ingest": {
            "schema_version": 4,
            "content_mode": "mixed" if group_id == "G13" else "summary",
            "repair": _repair_marker(group_id, str(spec["revision_id"])),
            "semantic_finalization": _semantic_marker(group_id, spec),
        }
    }
    if verbatim is not None:
        body["verbatim_blocks"] = copy.deepcopy(verbatim)
    return body


def _subtree_node_query(root_id: UUID, library_id: UUID):
    subtree = select(
        KnowledgeNode.id.label("node_id"),
        literal(0).label("depth"),
    ).where(
        KnowledgeNode.id == root_id,
        KnowledgeNode.docs_library_id == library_id,
    ).cte("semantic_finalize_subtree", recursive=True)
    parent_alias = subtree.alias()
    subtree = subtree.union_all(
        select(
            KnowledgeNode.id.label("node_id"),
            (parent_alias.c.depth + 1).label("depth"),
        ).where(
            KnowledgeNode.parent_id == parent_alias.c.node_id,
            KnowledgeNode.docs_library_id == library_id,
            parent_alias.c.depth < 512,
        )
    )
    return select(KnowledgeNode).where(KnowledgeNode.id.in_(select(subtree.c.node_id)))


def _db_url() -> str:
    load_dotenv(ROOT_DIR / ".env")
    user = quote_plus(os.getenv("POSTGRES_USER", "aoitalk"))
    password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = quote_plus(os.getenv("POSTGRES_DB", "aoitalk_memory"))
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


async def _load_state(session: AsyncSession) -> dict[str, Any]:
    root = await session.get(KnowledgeNode, ROOT_ID)
    if root is None or root.archived_at is not None:
        raise RuntimeError("MiniMax H3 rootが見つからないかアーカイブ済みです")
    library = await session.get(DocsLibrary, root.docs_library_id)
    if library is None or library.owner_user_id is None:
        raise RuntimeError("Docs library ownerが解決できません")
    rows = list((await session.execute(_subtree_node_query(root.id, root.docs_library_id))).scalars().all())
    nodes = {row.id: row for row in rows}
    revisions = list(
        (
            await session.execute(
                select(KnowledgeRevision)
                .where(KnowledgeRevision.node_id.in_(list(nodes)))
                .order_by(KnowledgeRevision.created_at, KnowledgeRevision.id)
            )
        ).scalars().all()
    )
    attachments = list(
        (
            await session.execute(
                select(KnowledgeAttachment).where(KnowledgeAttachment.node_id.in_(list(nodes)))
            )
        ).scalars().all()
    )
    placements = list(
        (
            await session.execute(
                select(KnowledgeNodePlacement).where(
                    (KnowledgeNodePlacement.node_id.in_(list(nodes)))
                    | (KnowledgeNodePlacement.parent_node_id.in_(list(nodes)))
                )
            )
        ).scalars().all()
    )
    edges = list(
        (
            await session.execute(
                select(KnowledgeEdge).where(
                    (KnowledgeEdge.source_node_id.in_(list(nodes)))
                    | (KnowledgeEdge.target_node_id.in_(list(nodes)))
                )
            )
        ).scalars().all()
    )
    children: dict[UUID | None, list[Any]] = {}
    for node in rows:
        children.setdefault(node.parent_id, []).append(node)
    for values in children.values():
        values.sort(key=lambda node: (float(node.sort_order or 0), node.created_at or datetime.min, str(node.id)))
    revisions_by_id = {row.id: row for row in revisions}
    return {
        "root": root,
        "library": library,
        "nodes": nodes,
        "children": children,
        "revisions": revisions_by_id,
        "revision_rows": revisions,
        "attachments": attachments,
        "placements": placements,
        "edges": edges,
    }


def _topic_for_group(state: dict[str, Any], group_id: str) -> Any | None:
    spec = GROUPS[group_id]
    topic_id = spec.get("topic_id")
    root = state["root"]
    if topic_id:
        topic = state["nodes"].get(_uuid(topic_id))
        if topic is None:
            raise RuntimeError(f"{group_id}: historical topic node is missing: {topic_id}")
        if (
            topic.docs_library_id != root.docs_library_id
            or topic.parent_id != ROOT_ID
            or topic.archived_at is not None
        ):
            raise RuntimeError(f"{group_id}: historical topic scope/parent/archive precondition failed")
        return topic
    deterministic = deterministic_topic_id(group_id)
    topic = state["nodes"].get(deterministic)
    if topic is not None:
        if (
            topic.docs_library_id != root.docs_library_id
            or topic.parent_id != ROOT_ID
            or topic.archived_at is not None
        ):
            raise RuntimeError(f"{group_id}: deterministic topic scope/parent/archive precondition failed")
        return topic
    for node in state["nodes"].values():
        marker = ((node.body_json or {}).get("clip_ingest") or {}).get("semantic_finalization") or {}
        if (
            marker.get("group_id") == group_id
            and node.docs_library_id == root.docs_library_id
            and node.parent_id == ROOT_ID
            and node.archived_at is None
        ):
            return node
    return None


def _group_source_refs(state: dict[str, Any], group_id: str) -> list[dict[str, Any]]:
    revision = state["revisions"].get(_uuid(GROUPS[group_id]["revision_id"]))
    if revision is None:
        raise RuntimeError(f"{group_id}: source revisionがありません")
    if revision.node_id != ROOT_ID:
        raise RuntimeError(f"{group_id}: source revisionがrootに紐づいていません")
    return copy.deepcopy(revision.source_refs_json or [])


def _validate_group_revision(state: dict[str, Any], group_id: str) -> list[str]:
    spec = GROUPS[group_id]
    revision = state["revisions"].get(_uuid(spec["revision_id"]))
    if revision is None:
        return [f"{group_id}: revision missing"]
    if revision.node_id != ROOT_ID:
        return [f"{group_id}: revision node_id != root"]
    return []


def _snapshot_tree(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _node_snapshot(node)
        for node in sorted(
            state["nodes"].values(),
            key=lambda item: (str(item.parent_id or ""), float(item.sort_order or 0), str(item.id)),
        )
    ]


def _affected_ids() -> set[UUID]:
    ids = {ROOT_ID}
    for group_id, spec in GROUPS.items():
        if spec.get("topic_id"):
            ids.add(_uuid(spec["topic_id"]))
        ids.update(_uuid(value) for value in spec.get("selected_ids", []))
        ids.update(_uuid(value) for value in WRAPPER_IDS.get(group_id, []))
    return ids


def _affected_snapshots(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = state["nodes"]
    result: dict[str, dict[str, Any]] = {}
    for node_id in sorted(_affected_ids(), key=str):
        node = nodes.get(node_id)
        if node is not None:
            result[str(node_id)] = _node_snapshot(node, include_body=True)
    return result


def _wrapper_ancestry(state: dict[str, Any], wrapper_id: str) -> list[str]:
    node = state["nodes"].get(_uuid(wrapper_id))
    if node is None:
        return []
    result: list[str] = []
    seen: set[UUID] = set()
    while node is not None and node.id not in seen:
        result.append(str(node.id))
        seen.add(node.id)
        node = state["nodes"].get(node.parent_id)
    return result


def _extract_verbatim(root: Any) -> list[dict[str, Any]]:
    blocks = (root.body_json or {}).get("verbatim_blocks")
    if not isinstance(blocks, list):
        raise RuntimeError("root verbatim_blocksがlistではありません")
    by_hash = {str(item.get("sha256")): item for item in blocks if isinstance(item, dict)}
    if any(value not in by_hash for value in G13_HASHES):
        raise RuntimeError("G13 input hashがroot verbatim_blocksにありません")
    result = []
    for value in G13_HASHES:
        item = copy.deepcopy(by_hash[value])
        required = ("kind", "label", "source_id", "source_type", "source_url", "start_line", "end_line", "char_count", "line_count", "blank_line_count")
        missing = [key for key in required if key not in item]
        if missing:
            raise RuntimeError(f"G13 provenance field missing: {value}: {','.join(missing)}")
        if item.get("source_id") != "source:0" or item.get("source_type") != "input" or item.get("source_url") not in ("", None):
            raise RuntimeError(f"G13 source provenance mismatch: {value}")
        try:
            start_line = int(item["start_line"])
            end_line = int(item["end_line"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"G13 line range is invalid: {value}") from exc
        if start_line < 1 or end_line < start_line:
            raise RuntimeError(f"G13 line range is invalid: {value}")
        content = str(item.get("content") or "")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != value:
            raise RuntimeError(f"G13 verbatim SHA不一致: {value}")
        line_count = content.count("\n") + 1
        blank_line_count = sum(line == "" for line in content.split("\n"))
        if int(item.get("line_count") or 0) != line_count:
            raise RuntimeError(f"G13 line_count不一致: {value}")
        if int(item.get("blank_line_count") or 0) != blank_line_count:
            raise RuntimeError(f"G13 blank_line_count不一致: {value}")
        if int(item.get("char_count") or 0) != len(content):
            raise RuntimeError(f"G13 char_count不一致: {value}")
        result.append(item)
    return result


def _already_applied(state: dict[str, Any]) -> bool:
    nodes = state["nodes"]
    root = state["root"]
    if "verbatim_blocks" in (root.body_json or {}):
        return False
    for group_id, spec in GROUPS.items():
        if spec.get("no_topic"):
            continue
        topic = _topic_for_group(state, group_id)
        if topic is None or topic.parent_id != ROOT_ID or topic.title != spec["title"]:
            return False
        semantic = ((topic.body_json or {}).get("clip_ingest") or {}).get("semantic_finalization") or {}
        if semantic.get("group_id") != group_id:
            return False
        if group_id in {"G05", "G12"}:
            expected = {_uuid(value) for value in spec.get("selected_ids", [])}
            actual = {node.id for node in nodes.values() if node.parent_id == topic.id and node.archived_at is None}
            if expected != actual:
                return False
        if group_id == "G13":
            blocks = (topic.body_json or {}).get("verbatim_blocks")
            if isinstance(blocks, list):
                if {str(item.get("sha256")) for item in blocks if isinstance(item, dict)} != set(G13_HASHES):
                    return False
            else:
                migration = (topic.body_json or {}).get("migration") or {}
                marker = migration.get("verbatim_content_to_typed_blocks") or {}
                if set(marker.get("block_sha256") or []) != set(G13_HASHES):
                    return False
    # Existing repair topics must no longer expose the UUID title at root.
    if any(
        node.parent_id == ROOT_ID and str(node.title).startswith("ClipIngest repair — revision ")
        for node in nodes.values()
    ):
        return False
    return True


def _plan(state: dict[str, Any]) -> dict[str, Any]:
    root = state["root"]
    nodes = state["nodes"]
    revision_errors = [error for group_id in GROUPS for error in _validate_group_revision(state, group_id)]
    if revision_errors:
        raise RuntimeError("; ".join(revision_errors))
    duplicate_ids: dict[str, str] = {}
    for group_id, spec in GROUPS.items():
        for child_id in spec.get("selected_ids", []):
            if child_id in duplicate_ids:
                previous = duplicate_ids[child_id]
                raise RuntimeError(
                    f"direct childが複数groupまたは重複割り当てされています: {child_id} ({previous},{group_id})"
                )
            duplicate_ids[child_id] = group_id
            if _uuid(child_id) not in nodes:
                raise RuntimeError(f"{group_id}: selected node missing: {child_id}")
    selected_ids = {_uuid(value) for spec in GROUPS.values() for value in spec.get("selected_ids", [])}
    if selected_ids & {ROOT_ID}:
        raise RuntimeError("root自身をsemantic childへ割り当てることはできません")
    before_root = _node_snapshot(root, include_body=True)
    before_tree = _snapshot_tree(state)
    affected = _affected_snapshots(state)
    renames: list[dict[str, Any]] = []
    reparent: list[dict[str, Any]] = []
    proposed_topics: list[dict[str, Any]] = []
    for group_id, spec in GROUPS.items():
        if spec.get("no_topic"):
            continue
        topic = _topic_for_group(state, group_id)
        topic_id = str(topic.id) if topic else str(deterministic_topic_id(group_id))
        if topic is not None and topic.title != spec["title"]:
            renames.append({"group_id": group_id, "node_id": topic_id, "old_title": topic.title, "new_title": spec["title"]})
        if topic is None:
            proposed_topics.append(
                {
                    "group_id": group_id,
                    "node_id": topic_id,
                    "title": spec["title"],
                    "parent_id": str(ROOT_ID),
                    "sort_order": spec.get("sort_order"),
                    "source_revision_id": spec["revision_id"],
                }
            )
        for child_id in spec.get("selected_ids", []):
            child = nodes[_uuid(child_id)]
            if child.parent_id != (topic.id if topic is not None else ROOT_ID):
                # Existing G01-G11 children are already under their repair
                # container; G05/G12 children are still direct root children.
                if topic is None or child.parent_id != ROOT_ID:
                    raise RuntimeError(f"{group_id}: selected child parent is unexpected: {child_id}")
            if topic is None:
                reparent.append(
                    {
                        "group_id": group_id,
                        "node_id": child_id,
                        "current_parent_id": str(child.parent_id) if child.parent_id else None,
                        "proposed_parent_id": topic_id,
                        "pre_sort_order": float(child.sort_order or 0),
                        "created_at": _iso(child.created_at),
                    }
                )
    # G13 is intentionally placed between the existing G04 and G05 root
    # positions without touching unrelated root sort values.
    g13_topic = _topic_for_group(state, "G13")
    g13_verbatim = _extract_verbatim(root)
    after_root_body = copy.deepcopy(root.body_json or {})
    after_root_body.pop("verbatim_blocks", None)
    selected_id_strings = {str(value) for value in selected_ids}
    renamed_ids = {item["node_id"] for item in renames}
    before_counts = {
        "nodes": len(nodes),
        "revisions": len(state["revision_rows"]),
        "attachments": len(state["attachments"]),
        "placements": len(state["placements"]),
        "edges": len(state["edges"]),
    }
    return {
        "root_id": str(ROOT_ID),
        "library_id": str(root.docs_library_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "dry_run_ready",
        "source_revision_ids": {group_id: spec["revision_id"] for group_id, spec in GROUPS.items()},
        "source_refs": {
            group_id: _group_source_refs(state, group_id)
            for group_id in GROUPS
        },
        "before_root": before_root,
        "before_tree": before_tree,
        "affected_snapshots": affected,
        "before_counts": before_counts,
        "before_revision_ids": sorted(str(revision.id) for revision in state["revision_rows"]),
        "latest_db_snapshot": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "root": before_root,
            "counts": before_counts,
            "affected_nodes": affected,
        },
        "rename_mapping": renames,
        "reparent_mapping": reparent,
        "proposed_topics": proposed_topics,
        "g13_verbatim": g13_verbatim,
        "proposed_root_body": {
            "keys": sorted(after_root_body),
            "digest": _digest(after_root_body),
            "verbatim_blocks_key_removed": "verbatim_blocks" not in after_root_body,
        },
        "g05_outcome": "one human-readable topic; four children reparented; Reddit empty_body provenance retained",
        "g12_outcome": "one umbrella topic; five children reparented; supplemental wrappers retained",
        "g13_outcome": "new unresolved topic; two input hashes moved losslessly",
        "g14_outcome": "no independent topic; audit-only fold into G13",
        "unchanged_existing_data": [
            {"id": row["id"], "parent_id": row["parent_id"], "archived_at": row["archived_at"], "body_json_digest": row["body_json_digest"]}
            for row in before_tree
            if row["id"] not in selected_id_strings
            and row["id"] != str(ROOT_ID)
            and row["id"] not in renamed_ids
        ],
        "preconditions": [
            "root_id/library_id/project_id/root_page_id exact",
            "all source revision IDs exist and remain attached to root",
            "all selected IDs exist exactly once",
            "all wrapper IDs exist and retain selected-child ancestry",
            "body_text/body_json/title/URL wrapper/ancestry snapshots compare exactly before lock and after lock",
            "G13 hashes and metrics recompute exactly",
            "transaction rollback on any failed precondition",
        ],
        "unresolved": {"G14": "classification=no_content_delta; folded_into_group=G13; independent_topic_created=false"},
        "attachments": {"count": len(state["attachments"]), "ids": [str(item.id) for item in state["attachments"]]},
    }


def _manifest_validate(manifest: dict[str, Any]) -> None:
    if manifest.get("root_id") != str(ROOT_ID):
        raise RuntimeError("manifest root_idが対象rootと一致しません")
    if manifest.get("status") not in {"dry_run_ready", "already_applied"}:
        raise RuntimeError("dry-run manifestではありません")
    for key in ("before_root", "affected_snapshots", "source_revision_ids", "source_refs"):
        if key not in manifest:
            raise RuntimeError(f"manifestに必須フィールドがありません: {key}")


def _compare_manifest_state(state: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    root_expected = manifest["before_root"]
    errors.extend(_compare_snapshot(state["root"], root_expected, label="root"))
    for node_id, expected in manifest.get("affected_snapshots", {}).items():
        node = state["nodes"].get(_uuid(node_id))
        if node is None:
            errors.append(f"affected node missing: {node_id}")
            continue
        errors.extend(_compare_snapshot(node, expected, label=f"node {node_id}"))
    for group_id, revision_id in manifest.get("source_revision_ids", {}).items():
        revision = state["revisions"].get(_uuid(revision_id))
        if revision is None:
            errors.append(f"{group_id}: source revision missing")
        elif revision.node_id != ROOT_ID:
            errors.append(f"{group_id}: source revision node mismatch")
        else:
            expected_refs = manifest.get("source_refs", {}).get(group_id)
            if _canonical(revision.source_refs_json or []) != _canonical(expected_refs or []):
                errors.append(f"{group_id}: source_refs_json changed")
    return errors


def _canonical_provenance(
    state: dict[str, Any], manifest: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Find the semantic-finalize revision, not merely the newest revision.

    A concurrent no-op Docs edit may append a revision with empty
    ``source_refs_json`` while leaving title/body/marker unchanged.  Such a
    history row is preserved; the canonical revision is the one whose body
    carries the approved semantic marker and whose refs equal the Gate-2
    manifest.
    """

    errors: list[str] = []
    details: dict[str, Any] = {}
    for group_id, spec in GROUPS.items():
        if spec.get("no_topic"):
            continue
        topic = _topic_for_group(state, group_id)
        if topic is None:
            errors.append(f"{group_id}: canonical topic missing")
            continue
        expected_refs = manifest.get("source_refs", {}).get(group_id) or []
        candidates: list[Any] = []
        for revision in state["revision_rows"]:
            if revision.node_id != topic.id or revision.title != spec["title"]:
                continue
            semantic = ((revision.body_json or {}).get("clip_ingest") or {}).get("semantic_finalization") or {}
            if semantic.get("group_id") != group_id:
                continue
            if _canonical(revision.source_refs_json or []) == _canonical(expected_refs):
                candidates.append(revision)
        if not candidates:
            errors.append(f"{group_id}: canonical semantic-finalize revision missing")
            continue
        canonical_revision = min(candidates, key=lambda item: (item.created_at, str(item.id)))
        later_noop: list[str] = []
        post_migration: list[str] = []
        canonical_body_digest = _digest(canonical_revision.body_json or {})
        before_revision_ids = set(manifest.get("before_revision_ids") or [])
        for revision in state["revision_rows"]:
            if revision.node_id != topic.id or (revision.created_at, str(revision.id)) <= (canonical_revision.created_at, str(canonical_revision.id)):
                continue
            if str(revision.id) in before_revision_ids:
                # The read-only manifest captured this history before the
                # semantic transaction.  Its DB timestamp may be timezone-
                # inconsistent with the application writer; preserve it as
                # pre-existing history rather than treating it as a post-apply
                # mutation of the semantic body.
                continue
            migration_marker = ((revision.body_json or {}).get("migration") or {}).get("verbatim_content_to_typed_blocks")
            if migration_marker:
                post_migration.append(str(revision.id))
                continue
            same_content = (
                revision.title == canonical_revision.title
                and (revision.body_text or "") == (canonical_revision.body_text or "")
                and _digest(revision.body_json or {}) == canonical_body_digest
            )
            if not same_content:
                errors.append(f"{group_id}: later revision changed semantic content: {revision.id}")
            else:
                later_noop.append(str(revision.id))
        preexisting_noop = [
            str(revision.id)
            for revision in state["revision_rows"]
            if revision.node_id == topic.id
            and str(revision.id) in before_revision_ids
            and str(revision.change_summary or "") == "nodeを更新"
            and not (revision.source_refs_json or [])
        ]
        details[group_id] = {
            "canonical_finalization_revision_id": str(canonical_revision.id),
            "canonical_source_refs_verified": True,
            "later_noop_revisions": later_noop,
            "post_migration_revisions": post_migration,
            "preexisting_history_revisions": [
                str(revision.id)
                for revision in state["revision_rows"]
                if revision.node_id == topic.id and str(revision.id) in before_revision_ids
            ],
            "preexisting_noop_revisions": preexisting_noop,
            "parallel_noop_history_preserved": bool(later_noop or preexisting_noop),
            "db_repair_required": False,
        }
    return errors, details


def _post_verify(state: dict[str, Any], manifest: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    if not _already_applied(state):
        errors.append("post-state is not already_applied")
    root = state["root"]
    if manifest is not None:
        expected_root_digest = ((manifest.get("proposed_root_body") or {}).get("digest"))
        if expected_root_digest and _digest(root.body_json or {}) != expected_root_digest:
            errors.append("root body_json digest mismatch")
        root_scope = (str(root.docs_library_id), str(root.root_page_id), str(root.project_id))
        for node_id, expected in (manifest.get("affected_snapshots") or {}).items():
            node = state["nodes"].get(_uuid(node_id))
            if node is None:
                continue
            if (str(node.docs_library_id), str(node.root_page_id), str(node.project_id)) != root_scope:
                errors.append(f"{node_id}: library/root_page/project drift")
            # Existing children and wrappers are immutable apart from the
            # explicitly approved parent/sort/timestamp changes.
            if node_id not in {str(ROOT_ID), *[str(spec.get("topic_id")) for spec in GROUPS.values() if spec.get("topic_id")]}:
                for key in ("archived_at", "created_at", "body_text", "body_json_digest", "system_key"):
                    if expected.get(key) != _node_snapshot(node).get(key):
                        errors.append(f"{node_id}: immutable {key} changed")
    for group_id, spec in GROUPS.items():
        if spec.get("no_topic"):
            continue
        topic = _topic_for_group(state, group_id)
        if topic is None:
            errors.append(f"{group_id}: topic missing")
            continue
        expected_children = {_uuid(value) for value in spec.get("selected_ids", [])}
        actual_children = {node.id for node in state["nodes"].values() if node.parent_id == topic.id and node.archived_at is None}
        if group_id in {"G05", "G12"} and expected_children != actual_children:
            errors.append(f"{group_id}: selected child set mismatch")
        elif group_id not in {"G05", "G12", "G13"} and not expected_children.issubset(actual_children):
            errors.append(f"{group_id}: selected child set mismatch")
        marker = ((topic.body_json or {}).get("clip_ingest") or {}).get("semantic_finalization") or {}
        if marker.get("group_id") != group_id or marker.get("original_topic_title_recovered") is not False:
            errors.append(f"{group_id}: semantic marker mismatch")
        for wrapper_id in WRAPPER_IDS.get(group_id, []):
            wrapper = state["nodes"].get(_uuid(wrapper_id))
            if wrapper is None or not _is_descendant(wrapper, expected_children or {topic.id}, state["nodes"]):
                errors.append(f"{group_id}: wrapper ancestry mismatch: {wrapper_id}")
    if "verbatim_blocks" in (root.body_json or {}):
        errors.append("root verbatim_blocks key was not removed")
    if manifest is not None:
        provenance_errors, _ = _canonical_provenance(state, manifest)
        errors.extend(provenance_errors)
    return errors


async def _lock_nodes(session: AsyncSession, ids: set[UUID]) -> dict[UUID, Any]:
    if not ids:
        return {}
    rows = list(
        (
            await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id.in_(sorted(ids, key=str)))
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(rows) != len(ids):
        missing = sorted(str(value) for value in ids - {row.id for row in rows})
        raise RuntimeError("lock対象nodeがありません: " + ",".join(missing))
    return {row.id: row for row in rows}


async def _apply(session: AsyncSession, state: dict[str, Any], manifest: dict[str, Any]) -> None:
    expected_ids = set(_affected_ids())
    # Deterministic new IDs are also locked when a partial apply left a row;
    # absent IDs are new rows and cannot be locked yet.
    expected_ids.update(
        deterministic_topic_id(group_id)
        for group_id in ("G05", "G12", "G13")
        if deterministic_topic_id(group_id) in state["nodes"]
    )
    await _lock_nodes(session, {ROOT_ID})
    await _lock_nodes(session, expected_ids - {ROOT_ID})
    session.expire_all()
    locked_state = await _load_state(session)
    if _already_applied(locked_state):
        return
    errors = _compare_manifest_state(locked_state, manifest)
    if errors:
        raise RuntimeError("lock後precondition不一致: " + "; ".join(errors))
    root = locked_state["root"]
    library = locked_state["library"]
    if not await can_write_node(session, root, library.owner_user_id, library=library):
        raise RuntimeError("semantic finalization actorにroot write ACLがありません")
    docs = DocsGraphService(session)
    topic_by_group: dict[str, Any] = {}
    for group_id, spec in GROUPS.items():
        if spec.get("no_topic"):
            continue
        source_refs = _group_source_refs(locked_state, group_id)
        topic = _topic_for_group(locked_state, group_id)
        if topic is None:
            body = _semantic_body(group_id, spec, verbatim=_extract_verbatim(root) if group_id == "G13" else None)
            topic = await docs.create_node(
                docs_library_id=root.docs_library_id,
                user_id=library.owner_user_id,
                title=spec["title"],
                parent=root,
                project_id=root.project_id,
                system_key=f"clip_ingest_semantic_finalize:{spec['revision_id']}",
                body_json=body,
                source_refs=source_refs,
                sort_order=spec.get("sort_order"),
                node_id=deterministic_topic_id(group_id),
            )
        else:
            body = copy.deepcopy(topic.body_json or {})
            clip = body.setdefault("clip_ingest", {})
            if clip.get("repair", {}).get("label_semantics") != "provenance_only":
                raise RuntimeError(f"{group_id}: repair label_semanticsが変更されています")
            clip["semantic_finalization"] = _semantic_marker(group_id, spec)
            topic = await docs.update_node(
                node=topic,
                user_id=library.owner_user_id,
                title=spec["title"],
                body_json=body,
                source_refs=source_refs,
                change_summary=f"semantic-finalize: curated title; group={group_id}",
            )
        topic_by_group[group_id] = topic
        if group_id in {"G05", "G12"}:
            current_children = [locked_state["nodes"][_uuid(value)] for value in spec["selected_ids"]]
            current_children.sort(key=lambda node: (float(node.sort_order or 0), node.created_at or datetime.min, str(node.id)))
            for child in current_children:
                if child.parent_id == topic.id:
                    continue
                if child.parent_id != ROOT_ID:
                    raise RuntimeError(f"{group_id}: child parent changed before move: {child.id}")
                await docs.move_node(node=child, new_parent=topic, user_id=library.owner_user_id)
    if "G13" in topic_by_group:
        new_root_body = copy.deepcopy(root.body_json or {})
        new_root_body.pop("verbatim_blocks", None)
        await docs.update_node(
            node=root,
            user_id=library.owner_user_id,
            body_json=new_root_body,
            source_refs=[],
            change_summary="semantic-finalize: G13 verbatim blocks moved; G14 folded into G13",
        )
    await session.flush()


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MiniMax H3 semantic finalization audit",
        f"- mode: `{report.get('mode', 'dry_run')}`",
        f"- status: `{report.get('status')}`",
        f"- root: `{report.get('root_id')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        "- historical audit: preserved unchanged",
        "- original topic title recovered: false",
        "",
        "## Rename mapping",
    ]
    for item in report.get("rename_mapping", []):
        lines.append(f"- {item['group_id']}: `{item['node_id']}` {item['old_title']} -> {item['new_title']}")
    lines.append("\n## Proposed topics")
    for item in report.get("proposed_topics", []):
        lines.append(f"- {item['group_id']}: `{item['node_id']}` {item['title']} sort={item.get('sort_order')}")
    lines.append("\n## Reparent mapping")
    for item in report.get("reparent_mapping", []):
        lines.append(f"- {item['group_id']}: `{item['node_id']}` -> `{item['proposed_parent_id']}` (pre-sort={item['pre_sort_order']})")
    lines.extend(
        [
            "\n## Group outcomes",
            f"- G05: {report.get('g05_outcome')}",
            f"- G12: {report.get('g12_outcome')}",
            f"- G13: {report.get('g13_outcome')}",
            f"- G14: {report.get('g14_outcome')}",
            "\n## Preconditions",
        ]
    )
    lines.extend(f"- {item}" for item in report.get("preconditions", []))
    lines.append("\n## Counts")
    lines.append(json.dumps(report.get("before_counts", {}), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def _write_audit(report: dict[str, Any], *, mode: str) -> tuple[Path, Path]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    if report.get("status") == "already_applied":
        json_path = AUDIT_DIR / "already_applied_audit.json"
        md_path = AUDIT_DIR / "already_applied_audit.md"
    elif mode == "dry_run":
        json_path = AUDIT_DIR / "dry_run.json"
        md_path = AUDIT_DIR / "dry_run.md"
    else:
        json_path = AUDIT_DIR / "apply_audit.json"
        md_path = AUDIT_DIR / "apply_audit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    if mode == "dry_run":
        readme = AUDIT_DIR / "README.md"
        if not readme.exists():
            readme.write_text(
                "# MiniMax H3 semantic finalization\n\n"
                "This is a second-stage audit. The historical repair audit is immutable.\n"
                "The dry-run is read-only; `--apply` requires Director Gate 2 approval.\n",
                encoding="utf-8",
            )
    return json_path, md_path


async def run(*, apply: bool, manifest_path: Path) -> dict[str, Any]:
    engine = create_async_engine(_db_url(), poolclass=NullPool, connect_args={"server_settings": {"jit": "off"}})
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if not apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            state = await _load_state(session)
            if _already_applied(state):
                existing_manifest: dict[str, Any] | None = None
                if manifest_path.exists():
                    try:
                        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        _manifest_validate(existing_manifest)
                    except Exception:
                        existing_manifest = None
                report = {
                    "mode": "dry_run",
                    "status": "already_applied",
                    "root_id": str(ROOT_ID),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "before_counts": {
                        "nodes": len(state["nodes"]),
                        "revisions": len(state["revision_rows"]),
                        "attachments": len(state["attachments"]),
                    },
                    "post_verify": "already_applied",
                }
                if existing_manifest is not None:
                    errors = _post_verify(state, existing_manifest)
                    if errors:
                        raise RuntimeError("already_applied post-verification失敗: " + "; ".join(errors))
                    _, details = _canonical_provenance(state, existing_manifest)
                    report["canonical_finalization_revisions"] = details
                    report["post_counts"] = {
                        "nodes": len(state["nodes"]),
                        "revisions": len(state["revision_rows"]),
                        "attachments": len(state["attachments"]),
                    }
                await session.rollback()
                return report
            if not apply:
                report = _plan(state)
                report["mode"] = "dry_run"
                await session.rollback()
                return report
            if not manifest_path.exists():
                raise RuntimeError(f"applyにはdry-run manifestが必要です: {manifest_path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _manifest_validate(manifest)
            errors = _compare_manifest_state(state, manifest)
            if errors:
                raise RuntimeError("apply前precondition不一致: " + "; ".join(errors))
            try:
                await _apply(session, state, manifest)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        async with AsyncSession(engine, expire_on_commit=False) as verify:
            verified = await _load_state(verify)
            errors = _post_verify(verified, manifest)
            if errors:
                raise RuntimeError("apply後verification失敗: " + "; ".join(errors))
            _, canonical_details = _canonical_provenance(verified, manifest)
            report = dict(manifest)
            report.update(
                {
                    "mode": "apply",
                    "status": "applied",
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                    "post_counts": {
                        "nodes": len(verified["nodes"]),
                        "revisions": len(verified["revision_rows"]),
                        "attachments": len(verified["attachments"]),
                    },
                    "post_verify": "pass",
                    "canonical_finalization_revisions": canonical_details,
                }
            )
            return report
    except Exception:
        # The caller prints the error; this explicit rollback is important for
        # failures after a flush but before commit.
        raise
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Director Gate 2承認後だけtransactionをcommitする")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(apply=args.apply, manifest_path=args.manifest))
        json_path, md_path = _write_audit(report, mode="apply" if args.apply else "dry_run")
        print(json.dumps({"status": report.get("status"), "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
