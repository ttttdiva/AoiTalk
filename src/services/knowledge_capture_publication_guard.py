"""Durable guard for Knowledge Capture managed Docs subtrees.

A published Resolution Knowledge document is a root plus a fixed set of
managed section children. Root-only revision checks are insufficient because
human edits to a section child do not necessarily revise the root. This module
captures and validates the live managed subtree while holding row locks so a
publish cannot race a concurrent human edit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import KnowledgeNode, KnowledgeRevision
from .docs_acl import can_read_node
from .knowledge_capture_evidence import _iso, content_hash

PUBLICATION_GUARD_SCHEMA = "knowledge-capture-subtree-guard-v1"
MANAGED_SECTION_LABELS = frozenset(
    {
        "Problem",
        "Preconditions",
        "Symptoms",
        "Root cause",
        "Resolution",
        "Procedure",
        "Verification",
        "Important pitfalls",
        "Environment / version constraints",
        "Known uncertainty",
    }
)
_CONTENT_HASH_RE = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")


class KnowledgeCapturePublicationGuardError(RuntimeError):
    """The live managed Docs subtree no longer matches its publication guard."""


def _uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise KnowledgeCapturePublicationGuardError("invalid publication guard node id") from exc


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, depth=depth + 1) for item in list(value)]
    iso = _iso(value)
    return iso if iso is not None else str(value)


async def _latest_revision_id(session: AsyncSession, node_id: UUID) -> UUID | None:
    result = await session.execute(
        select(KnowledgeRevision.id)
        .where(KnowledgeRevision.node_id == node_id)
        .order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def build_publication_guard_entry(node: Any, revision_id: Any) -> dict[str, Any]:
    """Build one deterministic, content-sensitive node guard entry."""

    node_id = str(getattr(node, "id", "") or "")
    if not node_id:
        raise KnowledgeCapturePublicationGuardError("publication guard node has no id")
    parent_id = getattr(node, "parent_id", None)
    source_refs = getattr(node, "source_refs_json", None)
    if source_refs is None:
        source_refs = getattr(node, "source_refs", None)
    payload = {
        "id": node_id,
        "parent_id": str(parent_id) if parent_id is not None else None,
        "project_id": str(getattr(node, "project_id", None)) if getattr(node, "project_id", None) is not None else None,
        "title": str(getattr(node, "title", "") or ""),
        "description": str(getattr(node, "description", "") or ""),
        "body_text": str(getattr(node, "body_text", "") or ""),
        "body_json": _jsonable(getattr(node, "body_json", None) or {}),
        "source_refs": _jsonable(source_refs or []),
        "sort_order": _jsonable(getattr(node, "sort_order", None)),
        "updated_at": _iso(getattr(node, "updated_at", None)),
        "revision_id": str(revision_id) if revision_id is not None else None,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "id": node_id,
        "parent_id": payload["parent_id"],
        "title": payload["title"],
        "updated_at": payload["updated_at"],
        "revision_id": payload["revision_id"],
        "content_hash": content_hash(serialized),
    }


async def capture_publication_subtree_guard(
    session: AsyncSession,
    root_id: UUID | str,
    *,
    lock: bool = False,
    adoption_actor_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Capture root + managed section children, optionally locking them.

    ``adoption_actor_id`` is intentionally opt-in.  Legacy publication guard
    adoption must validate the complete live subtree while its rows are locked,
    but normal post-publish capture is not an ACL operation and keeps the
    historical read path.
    """

    if adoption_actor_id is not None and not lock:
        raise KnowledgeCapturePublicationGuardError(
            "adoption subtree validation requires locked Docs rows"
        )
    adoption_actor = _uuid(adoption_actor_id) if adoption_actor_id is not None else None

    root_uuid = _uuid(root_id)
    root_statement = select(KnowledgeNode).where(
        KnowledgeNode.id == root_uuid,
        KnowledgeNode.archived_at.is_(None),
    )
    if lock:
        root_statement = root_statement.with_for_update()
    root_result = await session.execute(root_statement)
    root = root_result.scalar_one_or_none()
    if root is None:
        raise KnowledgeCapturePublicationGuardError("publication guard root is unavailable")

    children_statement = (
        select(KnowledgeNode)
        .where(
            KnowledgeNode.parent_id == root_uuid,
            KnowledgeNode.archived_at.is_(None),
            KnowledgeNode.title.in_(tuple(sorted(MANAGED_SECTION_LABELS))),
        )
        .order_by(KnowledgeNode.id)
    )
    if lock:
        children_statement = children_statement.with_for_update()
    children_result = await session.execute(children_statement)
    children = list(children_result.scalars().all())
    labels = [str(getattr(child, "title", "") or "") for child in children]
    if len(labels) != len(set(labels)):
        raise KnowledgeCapturePublicationGuardError("publication guard has duplicate managed section titles")
    missing_labels = MANAGED_SECTION_LABELS.difference(labels)
    if missing_labels:
        missing = ", ".join(sorted(missing_labels))
        raise KnowledgeCapturePublicationGuardError(
            f"publication guard is missing managed section children: {missing}"
        )

    if adoption_actor is not None:
        root_project_id = getattr(root, "project_id", None)
        root_library_id = getattr(root, "docs_library_id", None)
        for child in children:
            if (
                str(getattr(child, "project_id", None))
                != str(root_project_id)
                or str(getattr(child, "docs_library_id", None))
                != str(root_library_id)
            ):
                raise KnowledgeCapturePublicationGuardError(
                    "publication guard child is outside the root Docs scope"
                )
            try:
                readable = await can_read_node(session, child, adoption_actor)
            except Exception as exc:
                raise KnowledgeCapturePublicationGuardError(
                    "publication guard child read permission could not be verified"
                ) from exc
            if not readable:
                raise KnowledgeCapturePublicationGuardError(
                    "publication guard child read permission denied"
                )

    entries: list[dict[str, Any]] = []
    for node in [root, *children]:
        revision_id = await _latest_revision_id(session, _uuid(node.id))
        entries.append(build_publication_guard_entry(node, revision_id))
    entries.sort(key=lambda item: item["id"])
    return {
        "schema_version": PUBLICATION_GUARD_SCHEMA,
        "root_id": str(root_uuid),
        "nodes": entries,
    }


def extract_publication_subtree_guard(candidate: Any) -> dict[str, Any] | None:
    draft = candidate.get("draft_json") if isinstance(candidate, Mapping) else getattr(candidate, "draft_json", None)
    if not isinstance(draft, Mapping):
        return None
    publication = draft.get("publication")
    if not isinstance(publication, Mapping):
        return None
    guard = publication.get("subtree_guard")
    return dict(guard) if isinstance(guard, Mapping) else None


def _normalize_expected_guard(guard: Mapping[str, Any], root_id: UUID) -> dict[str, Any]:
    if guard.get("schema_version") != PUBLICATION_GUARD_SCHEMA:
        raise KnowledgeCapturePublicationGuardError("publication guard schema is unavailable")
    try:
        declared_root_id = _uuid(guard.get("root_id"))
    except KnowledgeCapturePublicationGuardError as exc:
        raise KnowledgeCapturePublicationGuardError(
            "publication guard root binding changed"
        ) from exc
    if declared_root_id != root_id:
        raise KnowledgeCapturePublicationGuardError("publication guard root binding changed")
    rows = guard.get("nodes")
    if not isinstance(rows, list) or not rows:
        raise KnowledgeCapturePublicationGuardError("publication guard nodes are unavailable")
    normalized: list[dict[str, Any]] = []
    node_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise KnowledgeCapturePublicationGuardError("publication guard node is invalid")
        try:
            node_id = str(_uuid(row.get("id")))
        except KnowledgeCapturePublicationGuardError as exc:
            raise KnowledgeCapturePublicationGuardError(
                "publication guard node id is invalid"
            ) from exc
        digest = row.get("content_hash")
        if not isinstance(digest, str) or _CONTENT_HASH_RE.fullmatch(digest) is None:
            raise KnowledgeCapturePublicationGuardError("publication guard node hash is invalid")
        node_ids.append(node_id)
        parent_id = row.get("parent_id")
        if node_id != str(root_id):
            try:
                normalized_parent_id = str(_uuid(parent_id))
            except KnowledgeCapturePublicationGuardError as exc:
                raise KnowledgeCapturePublicationGuardError(
                    "publication guard child parent is invalid"
                ) from exc
            if normalized_parent_id != str(root_id):
                raise KnowledgeCapturePublicationGuardError(
                    "publication guard child parent binding changed"
                )
        else:
            normalized_parent_id = (
                str(parent_id) if parent_id is not None else None
            )
        normalized.append(
            {
                "id": node_id,
                "parent_id": normalized_parent_id,
                "title": str(row.get("title") or ""),
                "updated_at": str(row.get("updated_at")) if row.get("updated_at") is not None else None,
                "revision_id": str(row.get("revision_id")) if row.get("revision_id") is not None else None,
                "content_hash": digest.lower(),
            }
        )
    if len(node_ids) != len(set(node_ids)):
        raise KnowledgeCapturePublicationGuardError("publication guard has duplicate node ids")
    root_rows = [row for row in normalized if row["id"] == str(root_id)]
    if len(root_rows) != 1:
        raise KnowledgeCapturePublicationGuardError("publication guard root entry is unavailable")
    child_titles = [row["title"] for row in normalized if row["id"] != str(root_id)]
    if len(child_titles) != len(set(child_titles)):
        raise KnowledgeCapturePublicationGuardError("publication guard has duplicate managed section titles")
    missing_labels = MANAGED_SECTION_LABELS.difference(child_titles)
    extra_labels = set(child_titles).difference(MANAGED_SECTION_LABELS)
    if missing_labels or extra_labels or len(normalized) != len(MANAGED_SECTION_LABELS) + 1:
        raise KnowledgeCapturePublicationGuardError(
            "publication guard does not contain exactly the managed section set"
        )
    normalized.sort(key=lambda item: item["id"])
    return {
        "schema_version": PUBLICATION_GUARD_SCHEMA,
        "root_id": str(root_id),
        "nodes": normalized,
    }


def validate_publication_subtree_guard(
    guard: Any,
    root_id: UUID | str,
) -> bool:
    """Return whether ``guard`` is a structurally valid guard for ``root_id``.

    This is deliberately a pure, fail-closed wrapper around the same
    normalization used by the live subtree comparison.  It performs no Docs
    or database reads, and it never repairs or returns a caller-owned guard.
    Callers that need the normalized value should use the comparison path,
    which still raises the detailed guard error.
    """

    if not isinstance(guard, Mapping):
        return False
    try:
        root_uuid = _uuid(root_id)
        _normalize_expected_guard(guard, root_uuid)
    except KnowledgeCapturePublicationGuardError:
        return False
    return True


async def assert_publication_subtree_unchanged(
    session: AsyncSession,
    root_id: UUID | str,
    expected_guard: Mapping[str, Any],
) -> None:
    """Fail closed if any generated root/section changed since publication."""

    root_uuid = _uuid(root_id)
    expected = _normalize_expected_guard(expected_guard, root_uuid)
    current = await capture_publication_subtree_guard(session, root_uuid, lock=True)
    if current != expected:
        raise KnowledgeCapturePublicationGuardError(
            "managed Docs subtree changed after Knowledge Capture publication"
        )


__all__ = [
    "KnowledgeCapturePublicationGuardError",
    "MANAGED_SECTION_LABELS",
    "PUBLICATION_GUARD_SCHEMA",
    "assert_publication_subtree_unchanged",
    "build_publication_guard_entry",
    "capture_publication_subtree_guard",
    "extract_publication_subtree_guard",
    "validate_publication_subtree_guard",
]
