"""Project context pack storage and prompt rendering."""

from __future__ import annotations

import json
import hashlib
import uuid
from copy import deepcopy
from datetime import date
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func, select, text, update

from ..memory.database import get_db_session
from ..memory.models import (
    ContextMemory,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeRevision,
    ProjectContextPack,
    ProjectContextPackRevision,
    Project,
    ProjectKnowledgeRef,
    ProjectQaEntry,
)
from .docs_acl import can_read_node
from .project_qa_candidate_service import _normalized_question_hash


def _coerce_uuid(value: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _append_json_or_text(lines: list[str], value: Any, prefix: str = "  ") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lines.append(f"{prefix}- {key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}- {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"{prefix}- {item}")
    elif value not in (None, ""):
        lines.append(f"{prefix}{value}")


_PACK_CONTENT_FIELDS = (
    "summary_md",
    "goals",
    "constraints",
    "current_status",
    "active_task_snapshot",
    "decisions",
    "open_questions",
    "manual_notes",
)
_TERMINAL_QUESTION_STATES = {
    "answered",
    "resolved",
    "cancelled",
    "canceled",
    "archived",
    "rejected",
    "stale",
}
PROJECT_CONTEXT_PACK_STATUSES = frozenset({"fresh", "stale", "building", "failed"})
PROJECT_CONTEXT_PACK_REBUILD_STATUSES = frozenset(
    {"pending", "running", "completed", "failed"}
)
PROJECT_CONTEXT_PACK_GENERATION_SOURCE = "project_context_pack_rebuild"
PROJECT_CONTEXT_PACK_GENERATION_MODE = "metadata_only"


def _validate_pack_status(value: Any) -> str:
    """Normalize and validate the projection lifecycle status."""

    normalized = str(value or "fresh").strip().casefold()
    if normalized not in PROJECT_CONTEXT_PACK_STATUSES:
        allowed = ", ".join(sorted(PROJECT_CONTEXT_PACK_STATUSES))
        raise ValueError(f"Project context pack status must be one of: {allowed}")
    return normalized


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON-compatible value without leaking source text."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _stable_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _structured_metadata(node: KnowledgeNode) -> dict[str, Any]:
    """Collect only bounded node metadata; body/content is intentionally absent."""

    return {
        "aliases_hash": _stable_sha256(node.aliases or []),
        "archived_at": _iso(node.archived_at),
        "day_date": _iso(node.day_date),
        "node_type": node.node_type,
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "sort_order": node.sort_order,
        "system_key": node.system_key,
        "updated_at": _iso(node.updated_at),
    }


async def _field_hashes(session: Any, node_id: uuid.UUID) -> tuple[str, list[dict[str, Any]]]:
    """Hash typed field values while keeping their raw values out of the digest."""

    result = await session.execute(
        select(KnowledgeFieldValue)
        .where(KnowledgeFieldValue.node_id == node_id)
        .order_by(KnowledgeFieldValue.field_id)
    )
    entries: list[dict[str, Any]] = []
    for value in result.scalars().all():
        raw_value = {
            "value_datetime": _iso(value.value_datetime),
            "value_json": value.value_json,
            "value_number": value.value_number,
            "value_text": value.value_text,
            "target_node_id": (
                str(value.target_node_id) if value.target_node_id else None
            ),
        }
        entries.append(
            {
                "field_id": str(value.field_id),
                "updated_at": _iso(value.updated_at),
                "value_hash": _stable_sha256(raw_value),
            }
        )
    return _stable_sha256(entries), entries


async def _build_project_context_pack_metadata(
    session: Any, project_id: str | uuid.UUID
) -> tuple[str, dict[str, Any]]:
    """Build bounded metadata and its stable digest for a project."""

    project_uuid = _coerce_uuid(project_id)
    project = await session.get(Project, project_uuid)
    if project is None:
        raise ValueError("Project not found")

    canonical_node: KnowledgeNode | None = None
    if project.knowledge_node_id:
        canonical_node = await session.get(KnowledgeNode, project.knowledge_node_id)

    canonical_payload: dict[str, Any] | None = None
    if canonical_node is not None:
        latest_revision = await session.scalar(
            select(KnowledgeRevision)
            .where(KnowledgeRevision.node_id == canonical_node.id)
            .order_by(
                KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc()
            )
            .limit(1)
        )
        fields_hash, field_entries = await _field_hashes(session, canonical_node.id)
        structured_metadata = _structured_metadata(canonical_node)
        canonical_payload = {
            "node_id": str(canonical_node.id),
            "title": canonical_node.title or "",
            "updated_at": _iso(canonical_node.updated_at),
            "structured_metadata_hash": _stable_sha256(structured_metadata),
            "fields_hash": fields_hash,
            "fields": field_entries,
            "latest_revision": (
                {
                    "id": str(latest_revision.id),
                    "created_at": _iso(latest_revision.created_at),
                    "title": latest_revision.title or "",
                    "structured_metadata_hash": _stable_sha256(
                        {
                            "node_id": str(latest_revision.node_id),
                            "created_at": _iso(latest_revision.created_at),
                        }
                    ),
                }
                if latest_revision is not None
                else None
            ),
        }

    refs_result = await session.execute(
        select(ProjectKnowledgeRef, KnowledgeNode)
        .join(KnowledgeNode, KnowledgeNode.id == ProjectKnowledgeRef.knowledge_node_id)
        .where(ProjectKnowledgeRef.project_id == project_uuid)
        .order_by(
            ProjectKnowledgeRef.priority,
            ProjectKnowledgeRef.knowledge_node_id,
            ProjectKnowledgeRef.id,
        )
    )
    references = [
        {
            "id": str(ref.id),
            "knowledge_node_id": str(ref.knowledge_node_id),
            "relation_type": ref.relation_type or "related",
            "priority": int(ref.priority or 0),
            "updated_at": _iso(ref.updated_at),
            "node_title": node.title or "",
            "node_updated_at": _iso(node.updated_at),
        }
        for ref, node in refs_result.all()
    ]

    memories_result = await session.execute(
        select(ContextMemory)
        .where(
            ContextMemory.project_id == project_uuid,
            ContextMemory.status == "active",
        )
        .order_by(ContextMemory.id)
    )
    active_memories = [
        {
            "id": str(memory.id),
            "updated_at": _iso(memory.updated_at),
            "importance": int(memory.importance or 0),
            "confidence": memory.confidence,
            "projection_metadata_hash": _stable_sha256(
                memory.projection_metadata or {}
            ),
        }
        for memory in memories_result.scalars().all()
    ]

    metadata = {
        "version": 1,
        "project_id": str(project_uuid),
        "canonical_node": canonical_payload,
        "project_knowledge_refs": references,
        "active_context_memories": active_memories,
    }
    digest_material = {
        "version": 1,
        "project_id": str(project_uuid),
        "canonical_node": (
            {
                "node_id": canonical_payload["node_id"],
                "title_hash": _stable_sha256(canonical_payload["title"]),
                "updated_at": canonical_payload["updated_at"],
                "structured_metadata_hash": canonical_payload[
                    "structured_metadata_hash"
                ],
                "fields_hash": canonical_payload["fields_hash"],
                "latest_revision": (
                    {
                        "id": canonical_payload["latest_revision"]["id"],
                        "created_at": canonical_payload["latest_revision"][
                            "created_at"
                        ],
                        "title_hash": _stable_sha256(
                            canonical_payload["latest_revision"]["title"]
                        ),
                        "structured_metadata_hash": canonical_payload[
                            "latest_revision"
                        ]["structured_metadata_hash"],
                    }
                    if canonical_payload["latest_revision"]
                    else None
                ),
            }
            if canonical_payload
            else None
        ),
        "project_knowledge_refs": [
            {
                "id": item["id"],
                "knowledge_node_id": item["knowledge_node_id"],
                "relation_type": item["relation_type"],
                "priority": item["priority"],
                "updated_at": item["updated_at"],
                "node_updated_at": item["node_updated_at"],
                "node_title_hash": _stable_sha256(item["node_title"]),
            }
            for item in references
        ],
        "active_context_memories": active_memories,
    }
    return _stable_sha256(digest_material), metadata


def _metadata_projection(metadata: dict[str, Any]) -> dict[str, Any]:
    """Render bounded metadata into deterministic pack fields."""

    canonical = metadata.get("canonical_node") or {}
    revision = canonical.get("latest_revision") or {}
    refs = metadata.get("project_knowledge_refs") or []
    memories = metadata.get("active_context_memories") or []
    canonical_title = str(canonical.get("title") or "").strip()
    canonical_node_id = canonical.get("node_id")
    summary = (
        f"Canonical Docs: {canonical_title} ({canonical_node_id})"
        if canonical_title and canonical_node_id
        else "Canonical Docs: not configured"
    )
    return {
        "summary_md": summary,
        "goals": [],
        "constraints": [],
        "current_status": {
            "status": "metadata_only",
            "project_id": metadata.get("project_id"),
            "canonical_node_id": canonical_node_id,
            "canonical_title": canonical_title,
            "latest_revision_id": revision.get("id"),
            "latest_revision_title": revision.get("title") or "",
            "project_knowledge_ref_count": len(refs),
            "active_context_memory_count": len(memories),
        },
        "active_task_snapshot": [
            {
                "knowledge_node_id": item.get("knowledge_node_id"),
                "node_title": item.get("node_title") or "",
                "relation_type": item.get("relation_type"),
                "priority": item.get("priority"),
                "node_updated_at": item.get("node_updated_at"),
            }
            for item in refs
        ],
        "decisions": [],
        "open_questions": [],
    }


async def build_project_context_pack_digest(
    session: Any, project_id: str | uuid.UUID
) -> str:
    """Return a stable SHA-256 digest of metadata-only project context inputs."""

    digest, _metadata = await _build_project_context_pack_metadata(session, project_id)
    return digest


async def rebuild_project_context_pack(
    session: Any, project_id: str | uuid.UUID
) -> Dict[str, Any]:
    """Rebuild a bounded metadata projection in the caller's transaction.

    The caller owns the commit.  A savepoint keeps an input/projection failure
    from replacing the last-known pack fields; the pack is marked ``failed``
    so a durable worker can retry it safely.
    """

    project_uuid = _coerce_uuid(project_id)
    result = await session.execute(
        select(ProjectContextPack)
        .where(ProjectContextPack.project_id == project_uuid)
        .with_for_update()
    )
    pack = result.scalar_one_or_none()
    existing_pack = pack is not None
    if pack is None:
        pack = ProjectContextPack(id=uuid.uuid4(), project_id=project_uuid)
        session.add(pack)
        await session.flush()

    old_payload = pack_snapshot(pack)
    pack.status = "building"
    pack.updated_at = datetime.utcnow()
    await session.flush()

    try:
        async with session.begin_nested():
            digest, metadata = await _build_project_context_pack_metadata(
                session, project_uuid
            )
            projection = _metadata_projection(metadata)
            validate_generated_pack_replacement(
                old_payload,
                {
                    **projection,
                    "manual_notes": pack.manual_notes or "",
                },
                fields=tuple(
                    field for field in _PACK_CONTENT_FIELDS if field != "manual_notes"
                ),
            )
            if existing_pack:
                await create_pack_revision(
                    session,
                    pack,
                    change_reason=PROJECT_CONTEXT_PACK_GENERATION_SOURCE,
                )
            for field in _PACK_CONTENT_FIELDS:
                if field == "manual_notes":
                    continue
                setattr(pack, field, deepcopy(projection[field]))

            generated_from = (
                deepcopy(pack.generated_from)
                if isinstance(pack.generated_from, dict)
                else {}
            )
            now = datetime.utcnow()
            generated_from.update(
                {
                    "source": PROJECT_CONTEXT_PACK_GENERATION_SOURCE,
                    "mode": PROJECT_CONTEXT_PACK_GENERATION_MODE,
                    "updated_at": now.isoformat(),
                }
            )
            pack.generated_from = generated_from
            pack.source_digest = digest
            pack.generated_at = now
            pack.generation_version = int(pack.generation_version or 1) + 1
            pack.status = "fresh"
            pack.updated_at = now
            await session.flush()
    except Exception:
        # The savepoint restores every generated field while retaining the
        # outer transaction and its newly-created pack row.
        pack.status = "failed"
        pack.updated_at = datetime.utcnow()
        await session.flush()

    return pack.to_dict()


def pack_snapshot(pack: ProjectContextPack) -> Dict[str, Any]:
    return {field: getattr(pack, field) for field in _PACK_CONTENT_FIELDS} | {
        "generated_from": pack.generated_from or {},
        "captured_updated_at": (
            pack.updated_at.isoformat() if pack.updated_at else None
        ),
    }


def _pack_payload_size(payload: Dict[str, Any]) -> int:
    def content_size(value: Any) -> int:
        if value in (None, "", [], {}):
            return 0
        if isinstance(value, dict):
            return sum(content_size(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return sum(content_size(item) for item in value)
        return len(str(value).strip())

    return sum(content_size(payload.get(field)) for field in _PACK_CONTENT_FIELDS)


def _pack_payload_text(payload: Dict[str, Any], fields: tuple[str, ...]) -> str:
    values: list[str] = []

    def collect(value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
        else:
            values.append(str(value).strip())

    for field in fields:
        collect(payload.get(field))
    return "\n".join(values)


def validate_generated_pack_replacement(
    old_payload: Dict[str, Any],
    new_payload: Dict[str, Any],
    *,
    fields: tuple[str, ...] = _PACK_CONTENT_FIELDS,
) -> None:
    old_size = _pack_payload_size(
        {field: old_payload.get(field) for field in fields}
    )
    new_size = _pack_payload_size(
        {field: new_payload.get(field) for field in fields}
    )
    if new_size <= 24:
        raise ValueError("Generated project context pack is empty or too short.")
    if old_size >= 160 and new_size < int(old_size * 0.45):
        raise ValueError(
            "Generated project context pack discarded most of the previous content."
        )
    if old_size >= 160:
        old_text = _pack_payload_text(old_payload, fields)
        new_text = _pack_payload_text(new_payload, fields)
        old_compact = "".join(old_text.casefold().split())
        new_compact = "".join(new_text.casefold().split())
        old_ngrams = {
            old_compact[index : index + 3]
            for index in range(max(0, len(old_compact) - 2))
        }
        new_ngrams = {
            new_compact[index : index + 3]
            for index in range(max(0, len(new_compact) - 2))
        }
        retention = (
            len(old_ngrams & new_ngrams) / len(old_ngrams)
            if old_ngrams
            else 1.0
        )
        if retention < 0.25:
            raise ValueError(
                "Generated project context pack did not preserve prior content."
            )


async def create_pack_revision(
    session: Any,
    pack: ProjectContextPack,
    *,
    change_reason: str,
    snapshot: Optional[Dict[str, Any]] = None,
) -> ProjectContextPackRevision:
    result = await session.execute(
        select(func.max(ProjectContextPackRevision.revision_number)).where(
            ProjectContextPackRevision.context_pack_id == pack.id
        )
    )
    revision_number = int(result.scalar_one_or_none() or 0) + 1
    revision = ProjectContextPackRevision(
        id=uuid.uuid4(),
        project_id=pack.project_id,
        context_pack_id=pack.id,
        revision_number=revision_number,
        snapshot=snapshot or pack_snapshot(pack),
        change_reason=change_reason[:255],
    )
    session.add(revision)
    return revision


def _question_hash(text: str) -> str:
    return _normalized_question_hash(text)


def _active_open_questions(
    value: Any,
    *,
    terminal_question_hashes: Optional[set[str]] = None,
) -> list[Any]:
    terminal_question_hashes = terminal_question_hashes or set()
    active: list[Any] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            text = str(item or "").strip()
            if text and _question_hash(text) not in terminal_question_hashes:
                active.append(text)
            continue
        status = str(item.get("status") or "open").strip().casefold()
        if status in _TERMINAL_QUESTION_STATES:
            continue
        text = item.get("text") or item.get("question")
        if text and _question_hash(str(text)) not in terminal_question_hashes:
            active.append({**item, "text": str(text)})
    return active


def merge_open_question_states(
    existing: Any,
    discovered: Any,
    *,
    confirmed_at: str,
) -> list[Dict[str, Any]]:
    """Merge organizer discoveries without reopening terminal questions."""

    def normalized_id(text: str) -> str:
        normalized = " ".join(text.strip().casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    merged: dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for item in _as_list(existing):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("question") or "").strip()
            if not text:
                continue
            question_id = str(item.get("id") or normalized_id(text))
            normalized = {
                **item,
                "id": question_id,
                "text": text,
                "status": str(item.get("status") or "open"),
            }
        else:
            text = str(item or "").strip()
            if not text:
                continue
            question_id = normalized_id(text)
            normalized = {
                "id": question_id,
                "text": text,
                "status": "open",
            }
        merged[question_id] = normalized
        order.append(question_id)

    for item in _as_list(discovered):
        text = str(
            (item.get("text") or item.get("question"))
            if isinstance(item, dict)
            else item
        ).strip()
        if not text:
            continue
        question_id = (
            str(item.get("id") or normalized_id(text))
            if isinstance(item, dict)
            else normalized_id(text)
        )
        current = merged.get(question_id)
        if current and str(current.get("status") or "").casefold() in (
            _TERMINAL_QUESTION_STATES
        ):
            continue
        merged[question_id] = {
            **(current or {}),
            "id": question_id,
            "text": text,
            "status": str((current or {}).get("status") or "open"),
            "last_confirmed_at": confirmed_at,
        }
        if question_id not in order:
            order.append(question_id)
    return [merged[question_id] for question_id in order]


class ProjectContextPackService:
    """CRUD helpers for a project's short canonical context."""

    _updatable_fields = {
        "summary_md",
        "goals",
        "constraints",
        "current_status",
        "active_task_snapshot",
        "decisions",
        "open_questions",
        "manual_notes",
        "generated_from",
    }

    async def build_project_context_pack_digest(
        self, session: Any, project_id: str | uuid.UUID
    ) -> str:
        return await build_project_context_pack_digest(session, project_id)

    async def rebuild_project_context_pack(
        self, session: Any, project_id: str | uuid.UUID
    ) -> Dict[str, Any]:
        return await rebuild_project_context_pack(session, project_id)

    async def get_project_context_pack(
        self, project_id: str
    ) -> Optional[Dict[str, Any]]:
        async with await get_db_session() as session:
            result = await session.execute(
                select(ProjectContextPack).where(
                    ProjectContextPack.project_id == _coerce_uuid(project_id)
                )
            )
            pack = result.scalar_one_or_none()
            return pack.to_dict() if pack else None

    async def build_project_context_pack_view(
        self,
        session: Any,
        project_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
    ) -> Optional[Dict[str, Any]]:
        """Build an actor-scoped view of the stored context pack.

        The persisted projection and its digest are deliberately shared across
        actors.  Only the ref-derived ``active_task_snapshot`` is filtered at
        read time, using the Docs ACL point-read authority.  No node body or
        field values are loaded by this view.
        """

        project_uuid = _coerce_uuid(project_id)
        try:
            actor_uuid = _coerce_uuid(actor_user_id)
        except (TypeError, ValueError):
            actor_uuid = None
        result = await session.execute(
            select(ProjectContextPack).where(
                ProjectContextPack.project_id == project_uuid
            )
        )
        pack = result.scalar_one_or_none()
        if pack is None:
            return None

        view = deepcopy(pack.to_dict())
        refs_result = await session.execute(
            select(ProjectKnowledgeRef, KnowledgeNode)
            .join(
                KnowledgeNode,
                KnowledgeNode.id == ProjectKnowledgeRef.knowledge_node_id,
            )
            .where(ProjectKnowledgeRef.project_id == project_uuid)
            .order_by(
                ProjectKnowledgeRef.priority,
                ProjectKnowledgeRef.knowledge_node_id,
                ProjectKnowledgeRef.id,
            )
        )

        visible_refs: dict[uuid.UUID, tuple[ProjectKnowledgeRef, KnowledgeNode]] = {}
        for ref, node in refs_result.all():
            if actor_uuid is None:
                continue
            try:
                readable = await can_read_node(session, node, actor_uuid)
            except Exception:
                # ACL failures must fail closed for this additive projection;
                # the canonical project context remains independently usable.
                readable = False
            if readable:
                visible_refs[node.id] = (ref, node)

        # ``active_task_snapshot`` is generated from ProjectKnowledgeRef rows.
        # Drop entries without a valid node identity as well as every
        # stale/unauthorized node identity; otherwise a stale/manual title
        # could bypass the ACL projection.
        filtered_snapshot: list[Any] = []
        node_id_keys = ("knowledge_node_id", "node_id")
        for item in _as_list(view.get("active_task_snapshot")):
            if not isinstance(item, dict):
                continue

            node_key = next((key for key in node_id_keys if key in item), None)
            if node_key is None:
                continue
            node_uuid = None
            try:
                node_uuid = _coerce_uuid(item.get(node_key))
            except (TypeError, ValueError):
                node_uuid = None
            authorized = visible_refs.get(node_uuid) if node_uuid else None
            if authorized is None:
                # Do not retain the stored title/ID for a hidden or deleted
                # reference, even when the projection is stale.
                continue

            ref, node = authorized
            # Keep only bounded identity metadata.  In particular, never copy
            # arbitrary body/content keys from a stale/manual snapshot.
            filtered_item = {
                key: deepcopy(item[key])
                for key in (
                    "relation_type",
                    "priority",
                    "node_updated_at",
                )
                if key in item
            }
            filtered_item.update(
                {
                    "knowledge_node_id": str(node.id),
                    "node_title": node.title or "",
                    "relation_type": ref.relation_type or "related",
                    "priority": int(ref.priority or 0),
                    "node_updated_at": _iso(node.updated_at),
                }
            )
            filtered_snapshot.append(filtered_item)

        view["active_task_snapshot"] = filtered_snapshot
        current_status = _as_dict(view.get("current_status"))
        current_status["project_knowledge_ref_count"] = len(visible_refs)
        view["current_status"] = current_status
        return view

    async def invalidate_project_context_pack(
        self,
        *,
        session: Any,
        project_id: str,
        reason: str = "source_changed",
    ) -> None:
        """Mark a project's last-known pack stale in the caller's transaction.

        No commit is performed here.  Callers that are already mutating a
        canonical source must keep the invalidation in the same transaction so
        a rollback cannot leave a stale marker behind.
        """

        project_uuid = _coerce_uuid(project_id)
        # ``reason`` is intentionally not persisted yet: the projection schema
        # records freshness, while source-specific audit trails retain the
        # detailed mutation reason.  Keep the parameter for a stable API and
        # future metadata expansion.
        _ = reason
        # Focused compatibility callers may intentionally omit the optional
        # projection table.  Check its existence before issuing the UPDATE so
        # the caller's outer transaction (and rollback semantics) remain
        # untouched.  Lightweight fake sessions do not expose ``run_sync``;
        # they use the normal update path and can provide their own stub row.
        bind = None
        try:
            bind = session.get_bind()
        except Exception:
            bind = None
        dialect_name = str(getattr(getattr(bind, "dialect", None), "name", ""))
        table_exists: bool | None = None
        if dialect_name == "sqlite":
            table_exists = bool(
                await session.scalar(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name=:table_name LIMIT 1"
                    ),
                    {"table_name": ProjectContextPack.__tablename__},
                )
            )
        elif dialect_name == "postgresql":
            table_exists = bool(
                await session.scalar(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": ProjectContextPack.__tablename__},
                )
            )
        if table_exists is False:
            return
        await session.execute(
            update(ProjectContextPack)
            .where(ProjectContextPack.project_id == project_uuid)
            .values(status="stale", updated_at=datetime.utcnow())
        )

    async def upsert_project_context_pack(
        self, project_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        project_uuid = _coerce_uuid(project_id)
        async with await get_db_session() as session:
            result = await session.execute(
                select(ProjectContextPack).where(
                    ProjectContextPack.project_id == project_uuid
                ).with_for_update()
            )
            pack = result.scalar_one_or_none()
            existing_pack = pack is not None
            if pack is None:
                pack = ProjectContextPack(id=uuid.uuid4(), project_id=project_uuid)
                session.add(pack)

            old_payload = pack_snapshot(pack)
            content_updated = False
            for field in self._updatable_fields:
                if field not in data:
                    continue
                content_updated = content_updated or field in _PACK_CONTENT_FIELDS
                value = data[field]
                if field in {
                    "goals",
                    "constraints",
                    "active_task_snapshot",
                    "decisions",
                    "open_questions",
                }:
                    value = _as_list(value)
                elif field in {"current_status", "generated_from"}:
                    value = _as_dict(value)
                elif value is None:
                    value = ""
                setattr(pack, field, value)
            generated_from = pack.generated_from or {}
            incoming_generated_from = data.get("generated_from")
            is_generated = bool(
                isinstance(incoming_generated_from, dict)
                and (
                    incoming_generated_from.get("generated_by")
                    or incoming_generated_from.get("source")
                in {"project_information_organizer", "llm", "model"}
                )
            )
            if is_generated:
                generated_fields = tuple(
                    field for field in _PACK_CONTENT_FIELDS if field in data
                )
                if not generated_fields:
                    raise ValueError(
                        "Generated project context pack has no content fields."
                    )
                validate_generated_pack_replacement(
                    old_payload,
                    pack_snapshot(pack),
                    fields=generated_fields,
                )
            if existing_pack:
                await create_pack_revision(
                    session,
                    pack,
                    change_reason=str(
                        generated_from.get("source") or "context_pack_update"
                    ),
                    snapshot=old_payload,
                )
            if content_updated:
                # A successful pack write replaces the last-known projection,
                # so it is fresh even when the previous source mutation had
                # marked the old row stale.
                pack.status = "fresh"
                pack.generated_at = datetime.utcnow()
            pack.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(pack)
            return pack.to_dict()

    async def render_project_context_pack_for_actor(
        self,
        project_id: str,
        actor_user_id: str | uuid.UUID,
    ) -> tuple[str, str | None]:
        """Render the stored pack after applying the actor's Docs ACL view."""

        async with await get_db_session() as session:
            pack = await self.build_project_context_pack_view(
                session,
                project_id,
                actor_user_id,
            )
            status = _validate_pack_status(pack.get("status")) if pack else None
            if status == "failed":
                return "", status

            canonical_hashes: set[str] = set()
            result = await session.execute(
                select(ProjectQaEntry).where(
                    ProjectQaEntry.project_id == _coerce_uuid(project_id),
                )
            )
            for entry in result.scalars().all():
                value = str(entry.normalized_question_hash or "").strip()
                if not value and entry.question:
                    value = _question_hash(str(entry.question))
                if value:
                    canonical_hashes.add(value)
            return (
                self.render_pack_dict(
                    pack,
                    terminal_question_hashes=canonical_hashes,
                ),
                status,
            )

    async def render_project_context_pack_for_prompt_with_status(
        self, project_id: str
    ) -> tuple[str, str | None]:
        """Render the last-known pack and expose its projection status."""

        pack = await self.get_project_context_pack(project_id)
        status = _validate_pack_status(pack.get("status")) if pack else None
        if status == "failed":
            return "", status
        canonical_hashes: set[str] = set()
        async with await get_db_session() as session:
            result = await session.execute(
                select(ProjectQaEntry).where(
                    ProjectQaEntry.project_id == _coerce_uuid(project_id),
                )
            )
            for entry in result.scalars().all():
                value = str(entry.normalized_question_hash or "").strip()
                if not value and entry.question:
                    value = _question_hash(str(entry.question))
                if value:
                    canonical_hashes.add(value)
        return self.render_pack_dict(
            pack,
            terminal_question_hashes=canonical_hashes,
        ), status

    async def render_project_context_pack_for_prompt(self, project_id: str) -> str:
        rendered, _status = await self.render_project_context_pack_for_prompt_with_status(
            project_id
        )
        return rendered

    @staticmethod
    def render_pack_dict(
        pack: Optional[Dict[str, Any]],
        *,
        terminal_question_hashes: Optional[set[str]] = None,
    ) -> str:
        if not pack:
            return ""

        lines = ["## Project Context Pack"]
        summary = (pack.get("summary_md") or "").strip()
        if summary:
            lines.append(f"- Summary: {summary}")

        sections = [
            ("Goals", pack.get("goals")),
            ("Constraints", pack.get("constraints")),
            ("Current Status", pack.get("current_status")),
            ("Active Tasks", pack.get("active_task_snapshot")),
            ("Decisions", pack.get("decisions")),
            (
                "Open Questions",
                _active_open_questions(
                    pack.get("open_questions"),
                    terminal_question_hashes=terminal_question_hashes,
                ),
            ),
        ]
        for title, value in sections:
            if value in (None, "", [], {}):
                continue
            lines.append(f"- {title}:")
            _append_json_or_text(lines, value)

        manual_notes = (pack.get("manual_notes") or "").strip()
        if manual_notes:
            lines.append("- Manual Notes:")
            lines.append(f"  {manual_notes}")

        return "\n".join(lines) if len(lines) > 1 else ""


_service = ProjectContextPackService()


async def get_project_context_pack(project_id: str) -> Optional[Dict[str, Any]]:
    return await _service.get_project_context_pack(project_id)


async def build_project_context_pack_view(
    session: Any,
    project_id: str | uuid.UUID,
    actor_user_id: str | uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """Transaction-aware actor-scoped view wrapper."""

    return await _service.build_project_context_pack_view(
        session,
        project_id,
        actor_user_id,
    )


async def upsert_project_context_pack(project_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return await _service.upsert_project_context_pack(project_id, data)


async def rebuild_project_context_pack_for_project(
    *, session: Any, project_id: str | uuid.UUID
) -> Dict[str, Any]:
    """Module-level transaction-aware rebuild wrapper."""

    return await rebuild_project_context_pack(session, project_id)


async def invalidate_project_context_pack(
    *, session: Any, project_id: str, reason: str = "source_changed"
) -> None:
    """Transaction-aware module-level invalidation helper."""

    await _service.invalidate_project_context_pack(
        session=session,
        project_id=project_id,
        reason=reason,
    )


async def invalidate_project_context_pack_for_project(
    *, project_id: str, reason: str = "source_changed"
) -> None:
    """Standalone invalidation wrapper for callers without a live session."""

    async with await get_db_session() as session:
        await invalidate_project_context_pack(
            session=session,
            project_id=project_id,
            reason=reason,
        )
        await session.commit()


async def render_project_context_pack_for_prompt(project_id: str) -> str:
    return await _service.render_project_context_pack_for_prompt(project_id)


async def render_project_context_pack_for_actor(
    project_id: str,
    actor_user_id: str | uuid.UUID,
) -> tuple[str, str | None]:
    return await _service.render_project_context_pack_for_actor(
        project_id,
        actor_user_id,
    )
