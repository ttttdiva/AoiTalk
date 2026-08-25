#!/usr/bin/env python3
"""Materialize legacy readonly ClipIngest content as editable Docs blocks.

The migration is deliberately dry-run by default::

    venv\\Scripts\\python.exe scripts\\migrations\\migrate_verbatim_content_to_typed_blocks.py
    venv\\Scripts\\python.exe scripts\\migrations\\migrate_verbatim_content_to_typed_blocks.py --apply

``body_json.verbatim_blocks`` and the old ``body_json.verbatim_content`` value
are migration input only.  Each value is copied to an ordinary child
``KnowledgeNode`` with ``format=doc_block`` and a ``markdown``/``code`` block
type.  The multiline text lives in the encrypted ``body_json.content`` value;
``title`` and ``body_text`` remain the label/search mirror.  The old keys are
removed only after every child has been created and verified in the same
transaction.

No SQL data writes are performed here.  Encrypted values are assigned through
the ORM properties and revisions/search indexes are recorded through
``DocsGraphService``.  A malformed, edited, missing, or otherwise conflicting
child fails closed and leaves the legacy value in place.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory.database import get_database_manager  # noqa: E402
from src.memory.models import KnowledgeNode  # noqa: E402
from src.services.docs_graph_service import DocsGraphService  # noqa: E402


MIGRATION_ID = "docs-verbatim-content-to-typed-blocks-v1"
MIGRATION_MARKER_KEY = "verbatim_content_to_typed_blocks"
LEGACY_KEYS = ("verbatim_blocks", "verbatim_content")
_CODE_KINDS = {
    "code",
    "source",
    "source_code",
    "program",
    "json",
    "yaml",
    "toml",
    "shell",
    "bash",
    "python",
    "javascript",
    "typescript",
    "sql",
}
_MARKDOWN_KINDS = {"markdown", "md", "gfm", "formatted", "text", "prose", "prompt"}


class MaterializeConflict(ValueError):
    """A fail-closed legacy/typed-block mismatch."""


@dataclass(frozen=True)
class LegacyBlock:
    """Validated legacy block with the exact content to materialize."""

    index: int
    legacy_key: str
    kind: str
    label: str
    content: str
    sha256: str
    char_count: int
    line_count: int
    blank_line_count: int
    source_id: Any = None
    source_type: Any = None
    source_url: Any = None
    start_line: Any = None
    end_line: Any = None
    legacy_kind: str = "formatted"
    legacy_sha256: str | None = None
    legacy_metrics: dict[str, Any] | None = None


def normalize_newlines(value: str) -> str:
    """Normalize only CRLF/CR separators; all other characters are retained."""

    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def content_metrics(content: str) -> dict[str, int]:
    """Return the ClipIngest-compatible content metrics."""

    return {
        "char_count": len(content),
        "line_count": content.count("\n") + 1,
        "blank_line_count": sum(line == "" for line in content.split("\n")),
    }


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _provided_metrics(raw: Mapping[str, Any]) -> dict[str, Any]:
    nested = raw.get("metrics")
    source = nested if isinstance(nested, Mapping) else raw
    return {
        key: source[key]
        for key in ("char_count", "line_count", "blank_line_count")
        if key in source and source[key] is not None
    }


def _kind_for(raw_kind: str, content: str) -> str:
    lowered = raw_kind.strip().casefold().replace("-", "_")
    if lowered in _MARKDOWN_KINDS:
        return "markdown"
    if lowered in _CODE_KINDS or lowered.startswith("code"):
        return "code"
    # A fenced code block is code even when old writers used ``formatted``.
    first_nonempty = next((line.strip() for line in content.split("\n") if line.strip()), "")
    if first_nonempty.startswith(("```", "~~~")):
        return "code"
    return "markdown"


def _validate_label(value: Any) -> str:
    label = str(value if value is not None else "原文")
    if not label:
        label = "原文"
    # A newline cannot be represented by the title/body_text mirror.  Do not
    # silently rewrite it; an operator can repair the source and rerun.
    if "\r" in label or "\n" in label:
        raise MaterializeConflict("原文block labelに改行があります")
    if len(label) > 500:
        raise MaterializeConflict("原文block labelが500文字を超えています")
    return label


def _validate_block(raw: Any, *, index: int, legacy_key: str) -> LegacyBlock:
    if not isinstance(raw, Mapping):
        raise MaterializeConflict(f"{legacy_key}[{index}]がobjectではありません")
    if "content" not in raw or not isinstance(raw.get("content"), str):
        raise MaterializeConflict(f"{legacy_key}[{index}]のcontentが文字列ではありません")
    raw_content = str(raw["content"])
    normalized = normalize_newlines(raw_content)
    raw_metrics = content_metrics(raw_content)
    normalized_metrics = content_metrics(normalized)
    expected_metrics = _provided_metrics(raw)
    expected_sha = str(raw.get("sha256") or "").strip().lower()
    raw_sha = content_sha256(raw_content)
    normalized_sha = content_sha256(normalized)

    # Existing producers normally persisted LF already.  For old CRLF rows,
    # accept the original digest/metrics or the permitted LF-normalized form,
    # but never accept a mismatch unrelated to line-ending conversion.
    if expected_sha and expected_sha not in {raw_sha, normalized_sha}:
        raise MaterializeConflict(f"{legacy_key}[{index}]のsha256がcontentと一致しません")
    if expected_metrics and any(
        key in expected_metrics
        and expected_metrics[key] not in {raw_metrics[key], normalized_metrics[key]}
        for key in ("char_count", "line_count", "blank_line_count")
    ):
        raise MaterializeConflict(f"{legacy_key}[{index}]のmetricsがcontentと一致しません")

    legacy_kind = str(raw.get("kind") or raw.get("block_type") or "formatted")
    kind = _kind_for(legacy_kind, normalized)
    label = _validate_label(raw.get("label") or "原文")
    return LegacyBlock(
        index=index,
        legacy_key=legacy_key,
        kind=kind,
        label=label,
        content=normalized,
        sha256=normalized_sha,
        char_count=normalized_metrics["char_count"],
        line_count=normalized_metrics["line_count"],
        blank_line_count=normalized_metrics["blank_line_count"],
        source_id=raw.get("source_id"),
        source_type=raw.get("source_type"),
        source_url=raw.get("source_url"),
        start_line=raw.get("start_line"),
        end_line=raw.get("end_line"),
        legacy_kind=legacy_kind,
        legacy_sha256=expected_sha or None,
        legacy_metrics=dict(expected_metrics) if expected_metrics else None,
    )


def extract_legacy_blocks(body_json: Any) -> tuple[str | None, list[LegacyBlock]]:
    """Extract and validate one legacy key without dropping any content."""

    body = _as_mapping(body_json)
    block_value = body.get("verbatim_blocks")
    content_value = body.get("verbatim_content")
    has_blocks = "verbatim_blocks" in body
    has_content = "verbatim_content" in body

    # Two non-empty representations are ambiguous.  Refuse to guess whether
    # one is an old duplicate or an independently edited source.
    if has_blocks and has_content:
        blocks_nonempty = bool(block_value)
        content_nonempty = content_value not in (None, "")
        if blocks_nonempty and content_nonempty:
            raise MaterializeConflict(
                "verbatim_blocksとverbatim_contentが同時に存在します"
            )
        if blocks_nonempty:
            has_content = False
        elif content_nonempty:
            has_blocks = False

    if has_blocks:
        if not isinstance(block_value, list):
            raise MaterializeConflict("verbatim_blocksがlistではありません")
        return (
            "verbatim_blocks",
            [
                _validate_block(item, index=index, legacy_key="verbatim_blocks")
                for index, item in enumerate(block_value)
            ],
        )
    if has_content:
        if not isinstance(content_value, str):
            raise MaterializeConflict("verbatim_contentが文字列ではありません")
        # Legacy single-string content has no metadata.  Treat it as exactly
        # one markdown block, preserving the full string and its line metrics.
        return (
            "verbatim_content",
            [
                _validate_block(
                    {"content": content_value, "kind": "formatted", "label": "原文"},
                    index=0,
                    legacy_key="verbatim_content",
                )
            ],
        )
    return None, []


def _marker(body_json: Any) -> Mapping[str, Any] | None:
    body = _as_mapping(body_json)
    migrations = body.get("migration")
    if not isinstance(migrations, Mapping):
        return None
    value = migrations.get(MIGRATION_MARKER_KEY)
    return value if isinstance(value, Mapping) else None


def _node_id(value: Any) -> str:
    return str(getattr(value, "id", value))


def _body(node: Any) -> Mapping[str, Any]:
    value = getattr(node, "body_json", None)
    return value if isinstance(value, Mapping) else {}


def _child_marker(child: Any) -> Mapping[str, Any] | None:
    # Parent markers are nested below ``migration[MARKER_KEY]``.  Typed child
    # markers are the direct ``migration`` object so the child remains
    # independently identifiable after the parent's legacy keys are removed.
    value = _body(child).get("migration")
    return value if isinstance(value, Mapping) else None


def _expected_child_json(
    parent: Any,
    block: LegacyBlock,
    *,
    migration_id: str = MIGRATION_ID,
) -> dict[str, Any]:
    migration = {
        "migration_id": migration_id,
        "source_node_id": _node_id(parent),
        "source_index": block.index,
        "legacy_key": block.legacy_key,
        "content_sha256": block.sha256,
    }
    clip_ingest = {
        # ``source_id`` + flat metrics are the canonical new writer shape.
        # The legacy-prefixed id and nested metrics remain as migration
        # provenance so old exports can be audited without loss.
        "source_id": block.source_id,
        "legacy_source_id": block.source_id,
        "source_type": block.source_type,
        "source_url": block.source_url,
        "start_line": block.start_line,
        "end_line": block.end_line,
        "sha256": block.sha256,
        "char_count": block.char_count,
        "line_count": block.line_count,
        "blank_line_count": block.blank_line_count,
        "metrics": {
            "char_count": block.char_count,
            "line_count": block.line_count,
            "blank_line_count": block.blank_line_count,
        },
    }
    if block.legacy_sha256 and block.legacy_sha256 != block.sha256:
        clip_ingest["legacy_sha256"] = block.legacy_sha256
    if block.legacy_metrics and block.legacy_metrics != clip_ingest["metrics"]:
        clip_ingest["legacy_metrics"] = dict(block.legacy_metrics)
    return {
        "format": "doc_block",
        "block_type": block.kind,
        "label": block.label,
        "content": block.content,
        "clip_ingest": clip_ingest,
        "migration": migration,
        # Keep the old kind for provenance without making it a display mode.
        "legacy_kind": block.legacy_kind,
    }


def _sort_value(node: Any) -> float:
    try:
        value = float(getattr(node, "sort_order", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _base_sort_order(
    parent: Any,
    children: Sequence[Any],
    marker: Mapping[str, Any] | None,
    *,
    migration_id: str = MIGRATION_ID,
) -> float:
    if marker is not None and marker.get("base_sort_order") is not None:
        try:
            value = float(marker["base_sort_order"])
        except (TypeError, ValueError):
            raise MaterializeConflict("migration markerのbase_sort_orderが不正です")
        if not math.isfinite(value):
            raise MaterializeConflict("migration markerのbase_sort_orderが不正です")
        return value
    tagged = [
        child
        for child in children
        if (
            (_child_marker(child) or {}).get("migration_id") == migration_id
            and (_child_marker(child) or {}).get("source_node_id") == _node_id(parent)
        )
    ]
    if tagged:
        candidates = [
            _sort_value(child) - int((_child_marker(child) or {}).get("source_index", 0))
            for child in tagged
        ]
        if max(candidates) - min(candidates) > 1e-9:
            raise MaterializeConflict("既存typed childのblock順序が一致しません")
        return candidates[0]
    others = [
        _sort_value(child)
        for child in children
        if (_child_marker(child) or {}).get("migration_id") != migration_id
    ]
    return (max(others) if others else 0.0) + 1.0


def _child_matches(
    child: Any,
    parent: Any,
    block: LegacyBlock,
    *,
    sort_order: float,
    migration_id: str = MIGRATION_ID,
) -> bool:
    expected = _expected_child_json(parent, block, migration_id=migration_id)
    actual = _body(child)
    return (
        getattr(child, "parent_id", None) == getattr(parent, "id", None)
        and str(getattr(child, "title", "")) == block.label
        and str(getattr(child, "body_text", "")) == block.label
        and abs(_sort_value(child) - sort_order) <= 1e-9
        and dict(actual) == expected
    )


def _repair_tree(parent: Any, expected: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "parent": {"id": _node_id(parent), "title": str(getattr(parent, "title", ""))},
        "children": [
            {
                "index": item["index"],
                "label": item["block"].label,
                "block_type": item["block"].kind,
                "sort_order": item["sort_order"],
                "sha256": item["block"].sha256,
                "char_count": item["block"].char_count,
                "line_count": item["block"].line_count,
                "blank_line_count": item["block"].blank_line_count,
                "content": item["block"].content,
            }
            for item in expected
        ],
    }


def plan_parent(
    parent: Any,
    children: Sequence[Any],
    *,
    migration_id: str = MIGRATION_ID,
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free repair plan for one parent."""

    body = _body(parent)
    if "migration" in body and body.get("migration") is not None and not isinstance(
        body.get("migration"), Mapping
    ):
        return {
            "node_id": _node_id(parent),
            "title": str(getattr(parent, "title", "")),
            "status": "conflict",
            "legacy_key": None,
            "block_count": 0,
            "issues": ["既存body_json.migrationがobjectではありません"],
            "repair_tree": None,
        }
    marker = _marker(body)
    try:
        legacy_key, blocks = extract_legacy_blocks(body)
    except MaterializeConflict as exc:
        return {
            "node_id": _node_id(parent),
            "title": str(getattr(parent, "title", "")),
            "status": "conflict",
            "legacy_key": None,
            "block_count": 0,
            "issues": [str(exc)],
            "repair_tree": None,
        }

    if legacy_key is None:
        if marker is None:
            return {
                "node_id": _node_id(parent),
                "title": str(getattr(parent, "title", "")),
                "status": "no_legacy",
                "legacy_key": None,
                "block_count": 0,
                "issues": [],
                "repair_tree": None,
            }
        marker_issues: list[str] = []
        if marker.get("migration_id") != migration_id:
            marker_issues.append("migration markerのmigration_idが一致しません")
        if marker.get("source_node_id") != _node_id(parent):
            marker_issues.append("migration markerのsource_node_idが一致しません")
        child_ids = [str(value) for value in marker.get("child_ids", []) if value]
        if len(set(child_ids)) != len(child_ids):
            marker_issues.append("migration markerのchild_idsが重複しています")
        by_id = {_node_id(child): child for child in children}
        missing = [value for value in child_ids if value not in by_id]
        issues = [*marker_issues]
        if missing:
            issues.append(f"migration marker childが見つかりません: {missing}")
        expected_hashes = [
            str(value) for value in marker.get("block_sha256", []) if value
        ]
        if expected_hashes and len(expected_hashes) != len(child_ids):
            issues.append("migration markerのblock_sha256数がchild数と一致しません")
        marker_blocks = marker.get("blocks")
        if not missing and isinstance(marker_blocks, list):
            if len(marker_blocks) != len(child_ids):
                issues.append("migration markerのblocks数がchild数と一致しません")
            for index, raw_block in enumerate(marker_blocks):
                if not isinstance(raw_block, Mapping) or index >= len(child_ids):
                    issues.append(f"migration markerのblock {index}が不正です")
                    continue
                child = by_id[child_ids[index]]
                body = _body(child)
                child_marker = _child_marker(child) or {}
                content = body.get("content")
                expected_sort = raw_block.get("sort_order")
                try:
                    sort_matches = abs(_sort_value(child) - float(expected_sort)) <= 1e-9
                except (TypeError, ValueError):
                    sort_matches = False
                expected_label = str(raw_block.get("label") or "")
                expected_type = str(raw_block.get("block_type") or "")
                expected_digest = str(raw_block.get("sha256") or "")
                if (
                    child_marker.get("migration_id") != marker.get("migration_id")
                    or child_marker.get("source_node_id") != _node_id(parent)
                    or child_marker.get("source_index") != raw_block.get("source_index", index)
                    or str(getattr(child, "title", "")) != expected_label
                    or str(getattr(child, "body_text", "")) != expected_label
                    or body.get("block_type") != expected_type
                    or not isinstance(content, str)
                    or content_sha256(content) != expected_digest
                    or not sort_matches
                ):
                    issues.append(f"migration marker child {child_ids[index]}が編集または改変されています")
        if not missing and expected_hashes:
            for index, child_id in enumerate(child_ids):
                child = by_id[child_id]
                child_marker = _child_marker(child) or {}
                content = _body(child).get("content")
                actual_hash = content_sha256(str(content)) if isinstance(content, str) else ""
                if (
                    child_marker.get("migration_id") != marker.get("migration_id", MIGRATION_ID)
                    or child_marker.get("source_node_id") != _node_id(parent)
                    or child_marker.get("source_index") != index
                    or index >= len(expected_hashes)
                    or actual_hash != expected_hashes[index]
                ):
                    issues.append(f"migration marker child {child_id}が編集または改変されています")
        status = "already_migrated" if not issues else "conflict"
        return {
            "node_id": _node_id(parent),
            "title": str(getattr(parent, "title", "")),
            "status": status,
            "legacy_key": None,
            "block_count": int(marker.get("block_count") or len(child_ids)),
            "issues": issues,
            "repair_tree": None,
        }

    marker_issues: list[str] = []
    if marker is not None:
        if marker.get("migration_id") != migration_id:
            marker_issues.append("migration markerのmigration_idが一致しません")
        if marker.get("source_node_id") != _node_id(parent):
            marker_issues.append("migration markerのsource_node_idが一致しません")

    try:
        base = _base_sort_order(parent, children, marker, migration_id=migration_id)
    except MaterializeConflict as exc:
        return {
            "node_id": _node_id(parent),
            "title": str(getattr(parent, "title", "")),
            "status": "conflict",
            "legacy_key": legacy_key,
            "block_count": len(blocks),
            "issues": [str(exc)],
            "repair_tree": None,
        }

    expected: list[dict[str, Any]] = []
    issues: list[str] = [*marker_issues]
    candidates_by_index: dict[int, list[Any]] = {}
    for child in children:
        child_marker = _child_marker(child) or {}
        if (
            child_marker.get("migration_id") == migration_id
            and child_marker.get("source_node_id") == _node_id(parent)
        ):
            try:
                index = int(child_marker.get("source_index"))
            except (TypeError, ValueError):
                issues.append(f"typed child {child.id}のsource_indexが不正です")
                continue
            candidates_by_index.setdefault(index, []).append(child)

    for block in blocks:
        sort_order = base + block.index
        candidates = candidates_by_index.get(block.index, [])
        if len(candidates) > 1:
            issues.append(f"block index {block.index}にtyped childが重複しています")
            continue
        existing = candidates[0] if candidates else None
        if existing is not None and not _child_matches(
            existing,
            parent,
            block,
            sort_order=sort_order,
            migration_id=migration_id,
        ):
            issues.append(f"block index {block.index}のtyped childが編集または改変されています")
        expected.append({"index": block.index, "block": block, "sort_order": sort_order, "existing": existing})

    # A tagged child with an index outside the legacy source is never silently
    # removed.  It may be a user-created block or evidence of a partial edit.
    expected_indices = {block.index for block in blocks}
    for index in candidates_by_index:
        if index not in expected_indices:
            issues.append(f"legacy sourceにないtyped child index {index}があります")

    marker_child_ids = [str(value) for value in (marker or {}).get("child_ids", []) if value]
    if marker_child_ids:
        actual_ids = {
            _node_id(item["existing"])
            for item in expected
            if item.get("existing") is not None
        }
        if set(marker_child_ids) != actual_ids:
            issues.append("migration markerとtyped child IDが一致しません")

    status = "conflict" if issues else "ready"
    return {
        "node_id": _node_id(parent),
        "title": str(getattr(parent, "title", "")),
        "status": status,
        "legacy_key": legacy_key,
        "block_count": len(blocks),
        "base_sort_order": base,
        "issues": issues,
        "expected": expected,
        "repair_tree": _repair_tree(parent, expected),
    }


def _source_ref(parent: Any, block: LegacyBlock | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "verbatim_content_materialization",
        "migration_id": MIGRATION_ID,
        "source_node_id": _node_id(parent),
    }
    if block is not None:
        value.update(
            {
                "source_index": block.index,
                "legacy_key": block.legacy_key,
                "sha256": block.sha256,
            }
        )
    return value


async def _children(
    session: Any,
    parent: Any,
    *,
    for_update: bool = False,
) -> list[KnowledgeNode]:
    stmt = (
        select(KnowledgeNode)
        .where(
            KnowledgeNode.parent_id == parent.id,
            KnowledgeNode.docs_library_id == parent.docs_library_id,
            KnowledgeNode.archived_at.is_(None),
        )
        .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at, KnowledgeNode.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _candidate_nodes(session: Any, node_ids: set[uuid.UUID] | None = None) -> list[KnowledgeNode]:
    stmt = select(KnowledgeNode).order_by(KnowledgeNode.created_at, KnowledgeNode.id)
    if node_ids:
        stmt = stmt.where(KnowledgeNode.id.in_(node_ids))
    result = await session.execute(stmt)
    nodes = list(result.scalars().all())
    candidates: list[KnowledgeNode] = []
    for node in nodes:
        body = _body(node)
        if any(key in body for key in LEGACY_KEYS) or _marker(body) is not None:
            candidates.append(node)
    return candidates


async def collect_dry_run(
    session: Any,
    *,
    node_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Read-only inventory with repaired tree and integrity details."""

    await session.execute(text("SET TRANSACTION READ ONLY"))
    try:
        nodes = await _candidate_nodes(session, node_ids)
        reports: list[dict[str, Any]] = []
        for parent in nodes:
            reports.append(plan_parent(parent, await _children(session, parent)))
        return {
            "schema_version": 1,
            "migration_id": MIGRATION_ID,
            "mode": "read_only_dry_run",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidate_count": len(reports),
            "ready_count": sum(item["status"] == "ready" for item in reports),
            "conflict_count": sum(item["status"] == "conflict" for item in reports),
            "already_migrated_count": sum(
                item["status"] == "already_migrated" for item in reports
            ),
            "candidates": reports,
        }
    finally:
        await session.rollback()


def _make_typed_child(parent: Any, item: Mapping[str, Any], *, user_id: Any) -> KnowledgeNode:
    block: LegacyBlock = item["block"]
    child_json = _expected_child_json(parent, block, migration_id=MIGRATION_ID)
    child = KnowledgeNode(
        id=uuid.uuid4(),
        docs_library_id=parent.docs_library_id,
        parent_id=parent.id,
        root_page_id=parent.root_page_id or parent.id,
        project_id=parent.project_id,
        title=block.label,
        body_text=block.label,
        body_json=child_json,
        node_type="node",
        sort_order=float(item["sort_order"]),
        created_by=user_id,
        updated_by=user_id,
    )
    return child


async def materialize_parent(
    session: Any,
    parent_id: uuid.UUID,
    *,
    migration_id: str = MIGRATION_ID,
) -> dict[str, Any]:
    """Materialize one parent under a row lock and a transaction."""

    await session.rollback()
    try:
        async with session.begin():
            locked_result = await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id == parent_id)
                .with_for_update()
            )
            parent = locked_result.scalar_one_or_none()
            if parent is None:
                raise MaterializeConflict(f"対象parentが見つかりません: {parent_id}")
            children = await _children(session, parent, for_update=True)
            plan = plan_parent(parent, children, migration_id=migration_id)
            if plan["status"] in {"no_legacy", "already_migrated"}:
                return plan
            if plan["status"] == "conflict":
                raise MaterializeConflict("; ".join(plan["issues"]))

            actor_id = parent.updated_by or parent.created_by
            graph = DocsGraphService(session)
            existing_by_index = {
                int((_child_marker(child) or {}).get("source_index")): child
                for child in children
                if (
                    (_child_marker(child) or {}).get("migration_id") == migration_id
                    and (_child_marker(child) or {}).get("source_node_id") == _node_id(parent)
                    and str((_child_marker(child) or {}).get("source_index", "")).isdigit()
                )
            }
            created_ids: list[str] = []
            for item in plan["expected"]:
                index = int(item["index"])
                child = existing_by_index.get(index)
                if child is None:
                    child = _make_typed_child(parent, item, user_id=actor_id)
                    session.add(child)
                    await session.flush()
                    await graph.record_node_change(
                        child,
                        actor_id,
                        "旧verbatim本文を編集可能typed blockへ移行",
                        [_source_ref(parent, item["block"])],
                    )
                    await session.flush()
                    created_ids.append(_node_id(child))
                elif not _child_matches(
                    child,
                    parent,
                    item["block"],
                    sort_order=float(item["sort_order"]),
                    migration_id=migration_id,
                ):
                    raise MaterializeConflict(f"block index {index}のtyped childが一致しません")

            # Reload and verify all children before touching the legacy keys.
            verified_children = await _children(session, parent, for_update=True)
            verified_plan = plan_parent(parent, verified_children, migration_id=migration_id)
            if verified_plan["status"] != "ready":
                raise MaterializeConflict(
                    "typed child完全性検証に失敗しました: "
                    + "; ".join(verified_plan.get("issues", []))
                )

            body_json = dict(_body(parent))
            body_json.pop("verbatim_blocks", None)
            body_json.pop("verbatim_content", None)
            child_ids = [
                _node_id(item["existing"])
                for item in verified_plan["expected"]
                if item.get("existing") is not None
            ]
            migration_data = dict(body_json.get("migration") or {})
            migration_data[MIGRATION_MARKER_KEY] = {
                "migration_id": migration_id,
                "status": "materialized",
                "source_node_id": _node_id(parent),
                "legacy_key": plan["legacy_key"],
                "base_sort_order": verified_plan["base_sort_order"],
                "block_count": verified_plan["block_count"],
                "child_ids": child_ids,
                "block_sha256": [
                    item["block"].sha256 for item in verified_plan["expected"]
                ],
                "blocks": [
                    {
                        "source_index": item["index"],
                        "child_id": _node_id(item["existing"]),
                        "label": item["block"].label,
                        "block_type": item["block"].kind,
                        "sha256": item["block"].sha256,
                        "sort_order": item["sort_order"],
                    }
                    for item in verified_plan["expected"]
                ],
            }
            body_json["migration"] = migration_data
            await graph.update_node(
                node=parent,
                user_id=actor_id,
                body_json=body_json,
                source_refs=[
                    _source_ref(parent),
                    *[_source_ref(parent, item["block"]) for item in verified_plan["expected"]],
                ],
                change_summary="旧verbatim本文の編集可能typed block移行を完了",
            )
            return {
                **verified_plan,
                "status": "applied",
                "created_child_ids": created_ids,
                "removed_legacy_keys": list(LEGACY_KEYS),
            }
    except Exception:
        await session.rollback()
        raise


async def apply_migration(
    session: Any,
    *,
    node_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Apply each candidate independently so one conflict cannot remove another key."""

    # Candidate discovery is read-only and deliberately repeated under the
    # current snapshot before each locked parent transaction.
    candidates = await _candidate_nodes(session, node_ids)
    # ``rollback`` expires ORM instances.  Capture scalar IDs before the
    # rollback so the subsequent per-parent transactions never trigger an
    # implicit async refresh (which raises MissingGreenlet in async SQLAlchemy).
    candidate_ids = [node.id for node in candidates]
    candidate_titles = {node.id: str(getattr(node, "title", "")) for node in candidates}
    await session.rollback()
    results: list[dict[str, Any]] = []
    for parent_id in candidate_ids:
        try:
            results.append(await materialize_parent(session, parent_id))
        except MaterializeConflict as exc:
            results.append(
                {
                    "node_id": str(parent_id),
                    "title": candidate_titles.get(parent_id, ""),
                    "status": "conflict",
                    "issues": [str(exc)],
                }
            )
        except Exception:
            await session.rollback()
            raise
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "mode": "apply",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(results),
        "applied_count": sum(item.get("status") == "applied" for item in results),
        "conflict_count": sum(item.get("status") == "conflict" for item in results),
        "results": results,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    return await migrate(
        apply=bool(args.apply),
        node_ids={uuid.UUID(value) for value in args.node_id} if args.node_id else None,
    )


async def migrate(
    *,
    apply: bool = False,
    node_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Run the migration with a fresh application database session."""

    manager = get_database_manager()
    # Open the configured session factory directly.  ``get_session()`` runs
    # startup/Alembic initialization, which could write schema state during a
    # supposedly read-only dry-run.  Operators should run schema migrations
    # separately before invoking ``--apply``.
    session = manager.SessionLocal()
    try:
        if not apply:
            return await collect_dry_run(session, node_ids=node_ids)
        return await apply_migration(session, node_ids=node_ids)
    finally:
        await session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the idempotent materialization. Without this flag the DB is read-only.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="明示的にread-only dry-runを実行する（既定値）。",
    )
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        help="対象parent UUID（省略時はlegacy keyを持つ全Docs node）。",
    )
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    if args.format == "jsonl":
        rows = report.get("candidates") or report.get("results") or []
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, default=str))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not report.get("conflict_count") else 2


if __name__ == "__main__":
    raise SystemExit(main())
