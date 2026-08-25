"""Dry-run/apply provenance-backed MiniMax H3 ClipIngest repair.

The repair creates deterministic, non-semantic containers keyed by the
historical ClipIngest revision UUID and reparents only children proven to
belong to that event.  It never reconstructs an original topic title or
regenerates knowledge text.  Running without ``--apply`` is read-only.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID

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
        KnowledgeNode,
        KnowledgeRevision,
        Project,
    )
    from src.services.docs_acl import can_write_node  # noqa: E402
    from src.services.docs_graph_service import DocsGraphService  # noqa: E402
    import src.services.docs_graph_service as _docs_graph_module  # noqa: E402

    # A repair audit is a database-only structural operation.  Do not start
    # the optional local Qdrant/RAG worker from record_node_change while the
    # batch is running; the separate post-apply verification is the evidence.
    _docs_graph_module._notify_docs_node_changed = lambda *_args, **_kwargs: None


ROOT_ID = UUID("4a3c2921-1a3a-4242-aab3-74b5794e9d7f")
APPROVED_GROUP_IDS = frozenset(
    {"G01", "G02", "G03", "G04", "G06", "G07", "G08", "G09", "G10", "G11"}
)
VERBATIM_MOVE_GROUPS = frozenset({"G03", "G04", "G11"})
TEMP_DIR = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
DEFAULT_MANIFEST = TEMP_DIR / "aoitalk_minimax_h3_repair_dry_run.json"
DEFAULT_DRY_RUN_RESULT = TEMP_DIR / "aoitalk_minimax_h3_repair_dry_run_result.json"
DEFAULT_DRY_RUN_MARKDOWN = TEMP_DIR / "aoitalk_minimax_h3_repair_dry_run_result.md"
PERMANENT_AUDIT_DIR = ROOT_DIR / "docs" / "audits" / "minimax_h3_repair_20260822"
DEFAULT_ALREADY_APPLIED_RESULT = PERMANENT_AUDIT_DIR / "already_applied_audit.json"
DEFAULT_ALREADY_APPLIED_MARKDOWN = PERMANENT_AUDIT_DIR / "already_applied_audit.md"

RETAINED_ROOT_VERBATIM_HASHES = frozenset(
    {
        "2a6f5e05771afa5560275f066a4ab6624cf0b8e8d6cc58f16a2abb6a968fc224",
        "99d7e6a4dab022a557436f52f10950ad4a8b1b81e722536531fe05ef70cccbfb",
    }
)
SNAPSHOT_FIELDS = (
    "parent_id",
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
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)


def _db_url() -> str:
    load_dotenv(ROOT_DIR / ".env")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = quote_plus(os.getenv("POSTGRES_USER", "aoitalk"))
    password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    database = quote_plus(os.getenv("POSTGRES_DB", "aoitalk_memory"))
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("root_id") != str(ROOT_ID):
        raise RuntimeError("dry-run manifestのroot_idが対象と一致しません")
    groups = {str(item.get("group_id")): item for item in payload.get("groups", [])}
    missing = sorted(APPROVED_GROUP_IDS - groups.keys())
    if missing:
        raise RuntimeError(f"approved groupがmanifestにありません: {', '.join(missing)}")
    return payload


def _repair_marker(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "repair_container_is_new": True,
        "original_topic_title_recovered": False,
        "label_semantics": "provenance_only",
        "source_revision_id": str(group["revision_id"]),
        "group_id": str(group["group_id"]),
    }


def _repair_title(group: dict[str, Any]) -> str:
    return f"ClipIngest repair — revision {group['revision_id']}"


def _node_snapshot(node: KnowledgeNode) -> dict[str, Any]:
    body = copy.deepcopy(node.body_json or {})
    return {
        "id": str(node.id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "archived_at": _iso(node.archived_at),
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "title": node.title,
        "sort_order": float(node.sort_order or 0),
        "created_at": _iso(node.created_at),
        "updated_at": _iso(node.updated_at),
        "body_text": node.body_text or "",
        # Keep the audit snapshot bounded while still making body_json edits
        # fail closed.  _digest uses canonical JSON ordering and UTF-8.
        "body_json_digest": _digest(body),
    }


def _manifest_snapshot_items(group: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return direct-child and URL-wrapper snapshots from either manifest shape.

    The original evidence manifest used ``selected_nodes`` and
    ``url_wrapper_nodes``.  New dry-run reports use explicit snapshot keys so
    that the immutable body/timestamp evidence is not confused with the
    planning-only tree metadata.  Keeping both readers lets an idempotent
    rerun inspect an old manifest while making a fresh apply reject it below.
    """
    selected = group.get("selected_node_snapshots")
    if selected is None:
        selected = group.get("selected_nodes") or []
    wrappers = group.get("url_wrapper_snapshots")
    if wrappers is None:
        wrappers = group.get("url_wrapper_nodes") or []
    return list(selected), list(wrappers)


def _snapshot_id(item: dict[str, Any]) -> str | None:
    value = item.get("id") or item.get("node_id")
    return str(value) if value is not None else None


def _group_snapshot_payload(
    group: dict[str, Any],
    nodes: dict[UUID, KnowledgeNode],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build current snapshots for direct children and URL-wrapper descendants."""
    selected_rows: list[dict[str, Any]] = []
    for node_id in group.get("selected_direct_child_ids", []):
        node = nodes.get(UUID(str(node_id)))
        if node is not None:
            selected_rows.append(_node_snapshot(node))

    wrapper_rows: list[dict[str, Any]] = []
    # The manifest's URL-wrapper list is the provenance boundary.  Do not
    # infer additional wrappers from titles or body text during a repair.
    _, manifest_wrappers = _manifest_snapshot_items(group)
    for item in manifest_wrappers:
        wrapper_id = _snapshot_id(item)
        if wrapper_id is None:
            continue
        node = nodes.get(UUID(wrapper_id))
        if node is None:
            continue
        snapshot = _node_snapshot(node)
        snapshot["url"] = str(item.get("url") or node.title or "")
        wrapper_rows.append(snapshot)
    return selected_rows, wrapper_rows


def _root_precondition(root: KnowledgeNode, manifest: dict[str, Any]) -> list[str]:
    plan = manifest.get("global_root_body_plan") or {}
    body = root.body_json or {}
    errors: list[str] = []
    if sorted(body.keys()) != sorted(plan.get("root_current_body_keys") or []):
        errors.append("root body_json keysがdry-runと一致しません")
    if _canonical(body.get("clip_ingest") or {}) != _canonical(plan.get("root_current_clip_ingest") or {}):
        errors.append("root clip_ingest metadataがdry-runと一致しません")
    blocks = body.get("verbatim_blocks") or []
    actual_hashes = [str(item.get("sha256") or "") for item in blocks if isinstance(item, dict)]
    if actual_hashes != list(plan.get("root_verbatim_hashes") or []):
        errors.append("root verbatim hash/orderがdry-runと一致しません")
    return errors


def _group_preconditions(
    group: dict[str, Any],
    *,
    root: KnowledgeNode,
    nodes: dict[UUID, KnowledgeNode],
    revisions: dict[UUID, KnowledgeRevision],
) -> list[str]:
    errors: list[str] = []
    revision = revisions.get(UUID(str(group["revision_id"])))
    if revision is None:
        errors.append("revisionが存在しません")
    else:
        if revision.node_id != root.id:
            errors.append("revisionのnode_idがrootではありません")
        if _canonical(revision.source_refs_json or []) != _canonical(group.get("source_refs") or []):
            errors.append("revision source_refs_jsonがdry-runと一致しません")
    selected_snapshots, wrapper_snapshots = _manifest_snapshot_items(group)
    expected_by_id: dict[str, dict[str, Any]] = {}
    for item in selected_snapshots + wrapper_snapshots:
        item_id = _snapshot_id(item)
        if item_id is None:
            errors.append("node snapshotにidがありません")
            continue
        if item_id in expected_by_id:
            errors.append(f"node snapshotが重複しています: {item_id}")
        expected_by_id[item_id] = item
    # A legacy manifest can prove title/parent/created_at but not the body or
    # wrapper state.  Applying from it is therefore unsafe and must fail
    # closed.  The already_applied branch intentionally does not call this
    # function so an idempotent read-only rerun can still verify post-state.
    for item_id, expected in expected_by_id.items():
        missing = [key for key in SNAPSHOT_FIELDS if key not in expected]
        if missing:
            errors.append(
                f"node snapshot {item_id} に必須フィールドがありません: {', '.join(missing)}"
            )
    ids = [UUID(str(value)) for value in group.get("selected_direct_child_ids", [])]
    if len(ids) != len(set(ids)):
        errors.append("direct child IDが重複しています")
    for node_id in ids:
        node = nodes.get(node_id)
        if node is None:
            errors.append(f"direct child {node_id} が存在しません")
            continue
        expected = expected_by_id.get(str(node_id))
        if expected is None:
            errors.append(f"direct child {node_id} のsnapshotがありません")
            continue
        if node.parent_id != root.id or node.archived_at is not None:
            errors.append(f"direct child {node_id} のparent/archiveがdry-runと違います")
        errors.extend(_compare_snapshot(node, expected, label=f"child {node_id}"))

    expected_wrapper_ids = {_snapshot_id(item) for item in wrapper_snapshots}
    if None in expected_wrapper_ids:
        expected_wrapper_ids.discard(None)
    for wrapper_id in sorted(expected_wrapper_ids):
        node = nodes.get(UUID(wrapper_id))
        expected = expected_by_id.get(wrapper_id)
        if node is None:
            errors.append(f"URL wrapper {wrapper_id} が存在しません")
            continue
        errors.extend(_compare_snapshot(node, expected or {}, label=f"URL wrapper {wrapper_id}"))
        expected_url = str((expected or {}).get("url") or (expected or {}).get("title") or "")
        if not expected_url:
            errors.append(f"URL wrapper {wrapper_id} にurl/titleがありません")
        elif node.title != expected_url:
            errors.append(f"URL wrapper {wrapper_id} のURL/titleがdry-runと違います")
        if not _is_descendant_of_selected(node, ids, nodes):
            errors.append(f"URL wrapper {wrapper_id} がselected direct childの子孫ではありません")
    return errors


def _compare_snapshot(
    node: KnowledgeNode,
    expected: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    """Compare every immutable repair snapshot field exactly."""
    errors: list[str] = []
    actual = _node_snapshot(node)
    for key in SNAPSHOT_FIELDS:
        if key not in expected:
            # The caller normally reports this as a manifest error as well;
            # keep this guard local so direct unit tests remain fail-closed.
            errors.append(f"{label} snapshotに{key}がありません")
            continue
        expected_value = expected.get(key)
        actual_value = actual.get(key)
        if key == "sort_order":
            try:
                matches = float(expected_value) == float(actual_value)
            except (TypeError, ValueError):
                matches = False
        else:
            matches = expected_value == actual_value
        if not matches:
            errors.append(f"{label} の{key}がdry-runと違います")
    return errors


def _is_descendant_of_selected(
    node: KnowledgeNode,
    selected_ids: list[UUID],
    nodes: dict[UUID, KnowledgeNode],
) -> bool:
    selected = set(selected_ids)
    current = node
    seen: set[UUID] = set()
    while current.parent_id is not None and current.id not in seen:
        if current.parent_id in selected:
            return True
        seen.add(current.id)
        parent = nodes.get(current.parent_id)
        if parent is None:
            return False
        current = parent
    return False


def _verbatim_map(root: KnowledgeNode) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("sha256")): item
        for item in (root.body_json or {}).get("verbatim_blocks", [])
        if isinstance(item, dict) and item.get("sha256")
    }


def _group_verbatim(group: dict[str, Any], root_blocks: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    introduced = list(group.get("verbatim_introduced") or [])
    if not introduced:
        return None, []
    if len(introduced) != 1:
        return None, ["verbatim候補が1件ではありません"]
    expected = introduced[0]
    actual = root_blocks.get(str(expected.get("sha256")))
    if actual is None:
        return None, ["root verbatim hashがありません"]
    for key in ("kind", "source_id", "source_type", "source_url", "start_line", "end_line", "char_count", "label", "content"):
        if actual.get(key) != expected.get(key):
            return None, [f"verbatim {key}が一致しません"]
    lines = str(actual.get("content") or "").splitlines()
    if int(actual.get("line_count") or 0) != len(lines):
        return None, ["verbatim line_countを再計算できません"]
    if int(actual.get("blank_line_count") or 0) != sum(1 for line in lines if not line.strip()):
        return None, ["verbatim blank_line_countを再計算できません"]
    if hashlib.sha256(str(actual.get("content") or "").encode("utf-8")).hexdigest() != str(expected.get("sha256")):
        return None, ["verbatim SHA-256再計算不一致"]
    return copy.deepcopy(actual), []


def _subtree_node_query(root_id: UUID, library_id: UUID):
    subtree = select(
        KnowledgeNode.id.label("node_id"),
        literal(0).label("depth"),
    ).where(
        KnowledgeNode.id == root_id,
        KnowledgeNode.docs_library_id == library_id,
    ).cte("repair_subtree", recursive=True)
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


async def _load_state(session: AsyncSession, manifest: dict[str, Any]):
    root = await session.get(KnowledgeNode, ROOT_ID)
    if root is None or root.archived_at is not None:
        raise RuntimeError("MiniMax H3 rootが見つからないかアーカイブ済みです")
    if root.docs_library_id != UUID(str(manifest.get("library_id"))):
        raise RuntimeError("Docs libraryがdry-runと一致しません")
    library = await session.get(DocsLibrary, root.docs_library_id)
    if library is None or library.owner_user_id is None:
        raise RuntimeError("Docs library ownerが解決できません")
    rows = list((await session.execute(_subtree_node_query(root.id, root.docs_library_id))).scalars().all())
    nodes = {row.id: row for row in rows}
    children_by_parent: dict[UUID | None, list[UUID]] = {}
    for row in rows:
        children_by_parent.setdefault(row.parent_id, []).append(row.id)
    subtree_ids: set[UUID] = {root.id}
    pending = [root.id]
    while pending:
        parent_id = pending.pop()
        for child_id in children_by_parent.get(parent_id, []):
            if child_id not in subtree_ids:
                subtree_ids.add(child_id)
                pending.append(child_id)
    ids = [UUID(str(group["revision_id"])) for group in manifest.get("groups", []) if str(group.get("group_id")) in APPROVED_GROUP_IDS]
    revisions = {
        row.id: row
        for row in (await session.execute(select(KnowledgeRevision).where(KnowledgeRevision.id.in_(ids)))).scalars().all()
    }
    attachments = list((await session.execute(select(KnowledgeAttachment).where(KnowledgeAttachment.node_id.in_(list(subtree_ids))))).scalars().all())
    return root, library, nodes, revisions, attachments, subtree_ids


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MiniMax H3 ClipIngest repair audit",
        f"- mode: `{report.get('mode')}`",
        f"- status: `{report.get('status')}`",
        f"- root: `{report.get('root_id')}`",
        f"- actor: `{report.get('actor_user_id') or ''}`",
        f"- root body before: `{report.get('root_before_body_hash') or ''}`",
        f"- root body after: `{report.get('root_after_body_hash') or ''}`",
        f"- verified at: `{report.get('verified_at') or ''}`",
        f"- post node count: `{report.get('post_node_count') if report.get('post_node_count') is not None else report.get('node_count', '')}`",
        f"- post attachment count: `{report.get('post_attachment_count') if report.get('post_attachment_count') is not None else report.get('attachment_count', '')}`",
        f"- retained root verbatim hashes: `{', '.join(report.get('retained_root_verbatim_hashes') or [])}`",
        "- original semantic topic title recovered: `false`",
        "- label semantics: `provenance_only`",
        "",
        "## Groups",
    ]
    for group in report.get("groups", []):
        lines.extend([
            f"### {group.get('group_id')} — {group.get('status', 'ready')}",
            f"- revision: `{group.get('revision_id')}`",
            f"- repair label: `{group.get('repair_title')}`",
            f"- topic id: `{group.get('topic_id') or ''}`",
            f"- selected children: `{', '.join(group.get('selected_child_ids') or [])}`",
            f"- moved verbatim: `{', '.join(group.get('moved_verbatim_sha256') or [])}`",
            f"- selected snapshots: `{len(group.get('selected_node_snapshots') or [])}`",
            f"- URL wrapper snapshots: `{len(group.get('url_wrapper_snapshots') or [])}`",
        ])
        if group.get("errors"):
            lines.append(f"- errors: {'; '.join(group['errors'])}")
    return "\n".join(lines) + "\n"


async def run(*, apply: bool = False, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    engine = create_async_engine(_db_url(), poolclass=NullPool)
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "apply" if apply else "dry_run",
        "status": "started",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root_id": str(ROOT_ID),
        "manifest_path": str(manifest_path),
        "approved_groups": sorted(APPROVED_GROUP_IDS),
        "groups": [],
    }
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if not apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            root, library, nodes, revisions, attachments, subtree_ids = await _load_state(session, manifest)
            report["library_id"] = str(library.id)
            report["actor_user_id"] = str(library.owner_user_id)
            if root.project_id is not None:
                project = await session.get(Project, root.project_id)
                if project is None or project.owner_id != library.owner_user_id:
                    raise RuntimeError("repair actorがDocs library/project ownerと一致しません")
            report["attachment_count_before"] = len(attachments)
            groups = sorted(
                [group for group in manifest.get("groups", []) if str(group.get("group_id")) in APPROVED_GROUP_IDS],
                key=lambda item: int(item.get("chronology_index") or 0),
            )
            if apply:
                legacy_snapshot_groups = [
                    str(group.get("group_id"))
                    for group in groups
                    if any(
                        key not in item
                        for item in sum(_manifest_snapshot_items(group), [])
                        for key in SNAPSHOT_FIELDS
                    )
                ]
                if legacy_snapshot_groups:
                    raise RuntimeError(
                        "snapshotフィールドのない旧manifestは--applyできません: "
                        + ", ".join(legacy_snapshot_groups)
                    )
            marker_topics = {
                str(((node.body_json or {}).get("clip_ingest") or {}).get("repair", {}).get("source_revision_id")): node
                for node in nodes.values()
                if ((node.body_json or {}).get("clip_ingest") or {}).get("repair", {}).get("source_revision_id")
            }
            if marker_topics:
                if set(marker_topics) != {str(group["revision_id"]) for group in groups}:
                    raise RuntimeError("repair topicが一部だけ存在するため、冪等再実行を安全に判定できません")
                remaining_hashes = [
                    str(item.get("sha256") or "")
                    for item in (root.body_json or {}).get("verbatim_blocks", [])
                    if isinstance(item, dict)
                ]
                if set(remaining_hashes) != RETAINED_ROOT_VERBATIM_HASHES:
                    raise RuntimeError("既存repair topicはあるがroot保留verbatimが期待値と一致しません")
                for group in groups:
                    topic = marker_topics[str(group["revision_id"])]
                    expected_children = {str(value) for value in group.get("selected_direct_child_ids", [])}
                    actual_children = {str(node.id) for node in nodes.values() if node.parent_id == topic.id and node.archived_at is None}
                    if actual_children != expected_children or topic.parent_id != ROOT_ID:
                        raise RuntimeError(f"{group['group_id']}: 既存repair topicのchild/parentが不一致です")
                    _, manifest_wrappers = _manifest_snapshot_items(group)
                    selected_ids = [UUID(str(value)) for value in group.get("selected_direct_child_ids", [])]
                    for wrapper in manifest_wrappers:
                        wrapper_id = _snapshot_id(wrapper)
                        wrapper_node = nodes.get(UUID(wrapper_id)) if wrapper_id else None
                        expected_url = str(wrapper.get("url") or wrapper.get("title") or "")
                        if (
                            wrapper_node is None
                            or not expected_url
                            or wrapper_node.title != expected_url
                            or not _is_descendant_of_selected(wrapper_node, selected_ids, nodes)
                        ):
                            raise RuntimeError(
                                f"{group['group_id']}: URL wrapperのpost-state/所属がmanifestと不一致です"
                            )
                    selected_snapshots, wrapper_snapshots = _group_snapshot_payload(group, nodes)
                    report["groups"].append({
                        "group_id": str(group["group_id"]),
                        "revision_id": str(group["revision_id"]),
                        "repair_title": _repair_title(group),
                        "repair_container_is_new": True,
                        "original_topic_title_recovered": False,
                        "label_semantics": "provenance_only",
                        "source_revision_id": str(group["revision_id"]),
                        "topic_id": str(topic.id),
                        "selected_child_ids": sorted(expected_children),
                        "moved_child_ids": sorted(expected_children),
                        "moved_verbatim_sha256": [str(item.get("sha256")) for item in (group.get("verbatim_introduced") or []) if str(group["group_id"]) in VERBATIM_MOVE_GROUPS],
                        "selected_node_snapshots": selected_snapshots,
                        "url_wrapper_snapshots": wrapper_snapshots,
                        "errors": [],
                    })
                report["root_before_body_hash"] = _digest(root.body_json or {})
                report["root_after_body_hash"] = report["root_before_body_hash"]
                report["post_root_body_hash"] = report["root_before_body_hash"]
                report["post_node_count"] = len(nodes)
                report["post_attachment_count"] = len(attachments)
                report["retained_root_verbatim_hashes"] = sorted(RETAINED_ROOT_VERBATIM_HASHES)
                report["topic_ids"] = sorted(item["topic_id"] for item in report["groups"])
                report["selected_child_ids"] = sorted(
                    child_id for item in report["groups"] for child_id in item["selected_child_ids"]
                )
                report["selected_child_count"] = len(report["selected_child_ids"])
                report["url_wrapper_count"] = sum(
                    len(item.get("url_wrapper_snapshots") or []) for item in report["groups"]
                )
                report["verified_at"] = datetime.now(timezone.utc).isoformat()
                report["status"] = "already_applied"
                await session.rollback()
                return report
            root_errors = _root_precondition(root, manifest)
            report["root_before_body_hash"] = _digest(root.body_json or {})
            if attachments:
                root_errors.append("approved repair対象subtreeにattachmentがあります")
            if root_errors:
                raise RuntimeError("root precondition不一致: " + "; ".join(root_errors))
            root_blocks = _verbatim_map(root)
            used_children: set[UUID] = set()
            for group in groups:
                group_id = str(group["group_id"])
                errors = _group_preconditions(group, root=root, nodes=nodes, revisions=revisions)
                child_ids = [str(value) for value in group.get("selected_direct_child_ids", [])]
                parsed_ids = [UUID(value) for value in child_ids]
                overlap = used_children.intersection(parsed_ids)
                if overlap:
                    errors.append(f"direct child重複所属: {sorted(map(str, overlap))}")
                used_children.update(parsed_ids)
                moved, verbatim_errors = _group_verbatim(group, root_blocks)
                if group_id not in VERBATIM_MOVE_GROUPS:
                    moved, verbatim_errors = None, []
                errors.extend(verbatim_errors)
                selected_snapshots, wrapper_snapshots = _group_snapshot_payload(group, nodes)
                existing_topic = next(
                    (
                        node for node in nodes.values()
                        if ((node.body_json or {}).get("clip_ingest") or {}).get("repair", {}).get("source_revision_id") == str(group["revision_id"])
                    ),
                    None,
                )
                report["groups"].append({
                    "group_id": group_id,
                    "revision_id": str(group["revision_id"]),
                    "repair_title": _repair_title(group),
                    "repair_container_is_new": True,
                    "original_topic_title_recovered": False,
                    "label_semantics": "provenance_only",
                    "source_revision_id": str(group["revision_id"]),
                    "topic_id": str(existing_topic.id) if existing_topic else None,
                    "selected_child_ids": child_ids,
                    "moved_verbatim_sha256": [str(moved.get("sha256"))] if moved else [],
                    "selected_node_snapshots": selected_snapshots,
                    "url_wrapper_snapshots": wrapper_snapshots,
                    "errors": errors,
                    "precondition_digest": _digest({"root": _node_snapshot(root), "group": group}),
                })
            invalid = [item for item in report["groups"] if item["errors"]]
            if invalid:
                raise RuntimeError("repair precondition不一致: " + "; ".join(f"{item['group_id']}: {', '.join(item['errors'])}" for item in invalid))
            if not apply:
                report["status"] = "dry_run_ready"
                report["root_after_body_hash"] = report["root_before_body_hash"]
                await session.rollback()
            else:
                locked_root = (await session.execute(select(KnowledgeNode).where(KnowledgeNode.id == ROOT_ID).with_for_update())).scalar_one()
                await session.refresh(locked_root)
                locked_root_errors = _root_precondition(locked_root, manifest)
                if locked_root_errors:
                    raise RuntimeError("lock後root precondition不一致: " + "; ".join(locked_root_errors))
                if not await can_write_node(session, locked_root, library.owner_user_id, library=library):
                    raise RuntimeError("repair actorにroot write ACLがありません")
                lock_ids: list[UUID] = []
                for group in groups:
                    direct_ids = [UUID(str(value)) for value in group.get("selected_direct_child_ids", [])]
                    _, wrapper_snapshots = _manifest_snapshot_items(group)
                    wrapper_ids = [UUID(str(_snapshot_id(item))) for item in wrapper_snapshots if _snapshot_id(item)]
                    lock_ids.extend(direct_ids + wrapper_ids)
                lock_ids = list(dict.fromkeys(lock_ids))
                locked = list((await session.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(lock_ids)).with_for_update())).scalars().all())
                if len(locked) != len(set(lock_ids)):
                    raise RuntimeError("移動対象child/wrapperのlock取得数が一致しません")
                # Keep the full subtree map for ancestry checks while
                # replacing every locked target with its FOR UPDATE instance.
                locked_nodes = dict(nodes)
                locked_nodes.update({node.id: node for node in locked})
                for group in groups:
                    lock_errors = _group_preconditions(group, root=locked_root, nodes=locked_nodes, revisions=revisions)
                    if lock_errors:
                        raise RuntimeError(f"{group['group_id']}: lock後child precondition不一致: {'; '.join(lock_errors)}")
                docs = DocsGraphService(session)

                async def _propagate_repair_root_page(node: KnowledgeNode) -> None:
                    new_root = node.root_page_id or node.id
                    descendants = list((await session.execute(_subtree_node_query(node.id, node.docs_library_id))).scalars().all())
                    for descendant in descendants:
                        if descendant.id != node.id:
                            descendant.root_page_id = new_root

                # The normal move service preserves the same invariant but
                # scans an entire large library.  This repair is intentionally
                # scoped to the locked MiniMax H3 subtree.
                docs._propagate_root_page = _propagate_repair_root_page
                topics: dict[str, KnowledgeNode] = {}
                for group in groups:
                    marker = _repair_marker(group)
                    body_json: dict[str, Any] = {
                        "clip_ingest": {
                            "schema_version": 4,
                            "content_mode": (group.get("clip_ingest") or {}).get("content_mode") or "summary",
                            "repair": marker,
                        }
                    }
                    moved, errors = _group_verbatim(group, root_blocks)
                    if str(group["group_id"]) in VERBATIM_MOVE_GROUPS:
                        if errors or moved is None:
                            raise RuntimeError(f"{group['group_id']}: verbatim precondition不一致")
                        body_json["verbatim_blocks"] = [copy.deepcopy(moved)]
                    manifest_selected, _ = _manifest_snapshot_items(group)
                    selected = [
                        item for item in manifest_selected
                        if _snapshot_id(item) in set(group.get("selected_direct_child_ids") or [])
                    ]
                    sort_order = min(float(item.get("sort_order") or 0) for item in selected)
                    topic = await docs.create_node(
                        docs_library_id=locked_root.docs_library_id,
                        user_id=library.owner_user_id,
                        title=_repair_title(group),
                        parent=locked_root,
                        project_id=locked_root.project_id,
                        system_key=f"clip_ingest_repair:{group['revision_id']}",
                        body_json=body_json,
                        source_refs=copy.deepcopy(group.get("source_refs") or []),
                        sort_order=sort_order,
                    )
                    await docs.record_node_change(
                        topic,
                        library.owner_user_id,
                        "repair: ClipIngest event boundary; original semantic topic title unavailable; "
                        f"source_revision={group['revision_id']}",
                        copy.deepcopy(group.get("source_refs") or []),
                    )
                    topics[str(group["group_id"])] = topic
                    for child_id in group.get("selected_direct_child_ids", []):
                        child = await session.get(KnowledgeNode, UUID(str(child_id)))
                        if child is None or child.parent_id != locked_root.id:
                            raise RuntimeError(f"{group['group_id']}: child parent precondition不一致")
                        await docs.move_node(node=child, new_parent=topic, user_id=library.owner_user_id)
                        await docs.record_node_change(
                            child,
                            library.owner_user_id,
                            "repair: ClipIngest child reparent; "
                            f"source_revision={group['revision_id']}",
                            [],
                        )
                moved_hashes = {
                    str(item.get("sha256"))
                    for group in groups if str(group["group_id"]) in VERBATIM_MOVE_GROUPS
                    for item in (group.get("verbatim_introduced") or [])
                }
                new_body = copy.deepcopy(locked_root.body_json or {})
                new_body["verbatim_blocks"] = [
                    item for item in (new_body.get("verbatim_blocks") or [])
                    if str(item.get("sha256")) not in moved_hashes
                ]
                if len(new_body["verbatim_blocks"]) != len(RETAINED_ROOT_VERBATIM_HASHES):
                    raise RuntimeError("root verbatim残存件数がG13/G14の2件ではありません")
                await docs.update_node(
                    node=locked_root,
                    user_id=library.owner_user_id,
                    body_json=new_body,
                    source_refs=[],
                    change_summary=("repair: ClipIngest event boundary; "
                                    "original semantic topic title unavailable; "
                                    "approved groups=" + ",".join(sorted(APPROVED_GROUP_IDS))),
                )
                await session.flush()
                report["root_after_body_hash"] = _digest(locked_root.body_json or {})
                for item in report["groups"]:
                    item["topic_id"] = str(topics[item["group_id"]].id)
                    item["moved_child_ids"] = item["selected_child_ids"]
                await session.commit()
                report["status"] = "applied"
        if apply:
            async with AsyncSession(engine, expire_on_commit=False) as verify:
                rows = list((await verify.execute(_subtree_node_query(ROOT_ID, UUID(str(manifest.get("library_id")))))).scalars().all())
                by_id = {row.id: row for row in rows}
                root_after = by_id.get(ROOT_ID)
                if root_after is None:
                    raise RuntimeError("apply後rootがありません")
                report["post_node_count"] = len(rows)
                post_children_by_parent: dict[UUID | None, list[UUID]] = {}
                for row in rows:
                    post_children_by_parent.setdefault(row.parent_id, []).append(row.id)
                post_subtree_ids: set[UUID] = {ROOT_ID}
                pending = [ROOT_ID]
                while pending:
                    parent_id = pending.pop()
                    for child_id in post_children_by_parent.get(parent_id, []):
                        if child_id not in post_subtree_ids:
                            post_subtree_ids.add(child_id)
                            pending.append(child_id)
                report["post_attachment_count"] = len(list((await verify.execute(select(KnowledgeAttachment).where(KnowledgeAttachment.node_id.in_(list(post_subtree_ids))))).scalars().all()))
                report["post_root_body_hash"] = _digest(root_after.body_json or {})
                post_root_hashes = {
                    str(item.get("sha256") or "")
                    for item in (root_after.body_json or {}).get("verbatim_blocks", [])
                    if isinstance(item, dict)
                }
                if post_root_hashes != RETAINED_ROOT_VERBATIM_HASHES:
                    raise RuntimeError("apply後root保留verbatimがG13/G14と一致しません")
                for item in report["groups"]:
                    topic = by_id.get(UUID(str(item["topic_id"]))) if item.get("topic_id") else None
                    if topic is None or topic.parent_id != ROOT_ID:
                        raise RuntimeError(f"apply後topic検証失敗: {item['group_id']}")
                    marker = ((topic.body_json or {}).get("clip_ingest") or {}).get("repair", {})
                    if marker.get("source_revision_id") != item["revision_id"] or marker.get("label_semantics") != "provenance_only":
                        raise RuntimeError(f"apply後repair marker検証失敗: {item['group_id']}")
                    item["post_child_ids"] = [str(row.id) for row in rows if row.parent_id == topic.id]
                    manifest_group = next(
                        group for group in groups if str(group.get("group_id")) == item["group_id"]
                    )
                    post_selected, post_wrappers = _group_snapshot_payload(manifest_group, by_id)
                    item["post_selected_node_snapshots"] = post_selected
                    item["post_url_wrapper_snapshots"] = post_wrappers
    finally:
        await engine.dispose()
    return report


def _write_report(report: dict[str, Any]) -> tuple[Path, Path]:
    if report.get("mode") == "dry_run":
        if report.get("status") == "already_applied":
            # Idempotent verification is an audit event, not another
            # transient dry-run result.  Keep the path deterministic so the
            # evidence can be committed without copying a timestamped file.
            json_path, md_path = DEFAULT_ALREADY_APPLIED_RESULT, DEFAULT_ALREADY_APPLIED_MARKDOWN
            json_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            json_path, md_path = DEFAULT_DRY_RUN_RESULT, DEFAULT_DRY_RUN_MARKDOWN
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = TEMP_DIR / f"aoitalk_minimax_h3_repair_apply_{stamp}.json"
        md_path = TEMP_DIR / f"aoitalk_minimax_h3_repair_apply_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="approved 10 groupsを1 transactionで適用")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(apply=args.apply, manifest_path=args.manifest))
        json_path, md_path = _write_report(report)
        print(json.dumps({"status": report.get("status"), "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
