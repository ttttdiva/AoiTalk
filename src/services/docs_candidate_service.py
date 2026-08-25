"""Reviewable Dreaming suggestions for the canonical Project Information Docs.

This boundary deliberately separates safe, bounded candidate data from
``ContextMemory``.  A candidate is visible only inside its Project ACL and is
materialized into canonical Docs after an explicit manage-settings approval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_db_session
from ..memory.models import DocsCandidate, KnowledgeNode, Project
from ..memory.project_repository import ProjectRepository
from .docs_acl import can_read_node, can_write_node
from .project_information_docs import update_project_information_doc

DOCS_CANDIDATE_STATUSES = frozenset({"proposed", "approved", "rejected", "superseded"})
DOCS_CANDIDATE_SENSITIVITIES = frozenset({"normal", "private", "secret"})
MAX_SOURCE_TYPE_LENGTH = 64
MAX_EVIDENCE_SPAN_LENGTH = 500
MAX_CONTENT_LENGTH = 4000
MAX_TITLE_LENGTH = 200
MAX_SECTION_HINT_LENGTH = 120
MAX_SOURCE_METADATA_ITEMS = 12
MAX_SOURCE_METADATA_VALUE_LENGTH = 240
ACTIVE_DEDUPE_STATUSES = ("proposed", "approved")
logger = logging.getLogger(__name__)

_DANGEROUS_KEY = re.compile(
    r"(?:transcript|assistant|raw|prompt|conversation|message|user[_-]?input|response|secret|token|password|credential)",
    re.IGNORECASE,
)


class DocsCandidateError(RuntimeError):
    """Base class for Docs candidate operations."""


class DocsCandidateNotFound(DocsCandidateError):
    """The candidate, Project, or target is not available to the actor."""


class DocsCandidateConflict(DocsCandidateError):
    """The candidate cannot transition because its version/status changed."""


class DocsCandidateValidation(ValueError, DocsCandidateError):
    """The candidate payload is outside the bounded safe schema."""


def _uuid(value: UUID | str | None, *, field: str, required: bool = True) -> UUID | None:
    if value in (None, ""):
        if required:
            raise DocsCandidateValidation(f"{field} is required")
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DocsCandidateValidation(f"{field} must be a valid UUID") from exc


def _text(value: Any, *, limit: int, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise DocsCandidateValidation(f"{field} is required")
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").replace("\r\n", "\n").strip()
    if not value:
        if required:
            raise DocsCandidateValidation(f"{field} is required")
        return None
    return value[:limit]


def _safe_metadata(value: Any) -> dict[str, Any]:
    """Keep only small scalar source metadata; never persist raw turn fields."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:MAX_SOURCE_METADATA_ITEMS]:
        key = _text(raw_key, limit=64, field="source_metadata key")
        if not key or _DANGEROUS_KEY.search(key):
            continue
        if isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = raw_value.replace("\x00", "").strip()[:MAX_SOURCE_METADATA_VALUE_LENGTH]
    return result


def sanitize_candidate_content(value: Any) -> dict[str, Any]:
    """Normalize a suggestion to the closed, bounded candidate payload.

    Unknown keys (including transcript-like fields) are dropped rather than
    copied.  ``content`` is the only text intended for a later canonical Docs
    append; it is capped so accidental provider payloads cannot become a raw
    transcript store.
    """

    if not isinstance(value, Mapping):
        raise DocsCandidateValidation("content_json must be an object")
    if value.get("content") not in (None, "") and not isinstance(
        value.get("content"), str
    ):
        raise DocsCandidateValidation("candidate content must be a string")
    title = _text(value.get("title"), limit=MAX_TITLE_LENGTH, field="title")
    content = _text(value.get("content"), limit=MAX_CONTENT_LENGTH, field="content")
    section_hint = _text(
        value.get("section_hint"),
        limit=MAX_SECTION_HINT_LENGTH,
        field="section_hint",
    )
    source_metadata = _safe_metadata(value.get("source_metadata"))
    if content is None and title is None:
        raise DocsCandidateValidation("candidate content or title is required")
    result: dict[str, Any] = {}
    if title is not None:
        result["title"] = title
    if content is not None:
        result["content"] = content
    if section_hint is not None:
        result["section_hint"] = section_hint
    if source_metadata:
        result["source_metadata"] = source_metadata
    return result


def _candidate_dict(candidate: DocsCandidate) -> dict[str, Any]:
    return candidate.to_dict()


def _dedupe_key(
    *,
    project_id: UUID,
    source_job_id: UUID | None,
    evidence_hash: str | None,
    content: Mapping[str, Any],
) -> str | None:
    """Build a stable retry key for one durable extraction item."""

    if source_job_id is None:
        return None
    material_hash = evidence_hash
    if not material_hash:
        material_hash = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return hashlib.sha256(
        f"{project_id}:{source_job_id}:{material_hash}".encode("utf-8")
    ).hexdigest()


async def _project_for_actor(
    session: Any,
    project_id: UUID,
    actor_user_id: UUID,
    *,
    permission: str,
) -> Project:
    project = await session.get(Project, project_id)
    if project is None or getattr(project, "deleted_at", None) is not None:
        raise DocsCandidateNotFound("project not found")
    allowed = await ProjectRepository.has_permission(
        session,
        project_id=project_id,
        user_id=actor_user_id,
        permission=permission,
    )
    if not allowed:
        raise PermissionError("Docs candidate permission denied")
    return project


async def _rollback(session: Any) -> None:
    rollback = getattr(session, "rollback", None)
    if callable(rollback):
        await rollback()


def _expected_version(
    expected_version: int | None,
    version: int | None,
    current: int,
) -> int:
    value = expected_version if expected_version is not None else version
    if value is None:
        return int(current)
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DocsCandidateValidation("expected_version must be an integer") from exc
    if normalized < 1:
        raise DocsCandidateValidation("expected_version must be positive")
    return normalized


async def create_candidate(
    *,
    project_id: UUID | str,
    created_by: UUID | str | None = None,
    actor_user_id: UUID | str | None = None,
    source_type: str = "dreaming_auto",
    content_json: Mapping[str, Any] | None = None,
    content: Mapping[str, Any] | None = None,
    confidence: float = 0.0,
    importance: int = 1,
    sensitivity: str = "normal",
    evidence_hash: str | None = None,
    evidence_span: str | None = None,
    source_job_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Create one proposed candidate in a Project-scoped review queue."""

    project_uuid = _uuid(project_id, field="project_id")
    creator_uuid = _uuid(created_by or actor_user_id, field="created_by")
    payload = sanitize_candidate_content(content_json if content_json is not None else content)
    source = _text(source_type, limit=MAX_SOURCE_TYPE_LENGTH, field="source_type", required=True)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise DocsCandidateValidation("confidence must be a number") from exc
    if not 0.0 <= confidence_value <= 1.0:
        raise DocsCandidateValidation("confidence must be between 0 and 1")
    try:
        importance_value = int(importance)
    except (TypeError, ValueError) as exc:
        raise DocsCandidateValidation("importance must be an integer") from exc
    if not 1 <= importance_value <= 10:
        raise DocsCandidateValidation("importance must be between 1 and 10")
    sensitivity_value = str(sensitivity or "normal").strip().lower()
    if sensitivity_value not in DOCS_CANDIDATE_SENSITIVITIES:
        raise DocsCandidateValidation("unsupported sensitivity")
    evidence_span_value = _text(
        evidence_span,
        limit=MAX_EVIDENCE_SPAN_LENGTH,
        field="evidence_span",
    )
    evidence_hash_value = _text(evidence_hash, limit=64, field="evidence_hash")
    if evidence_hash_value is None and evidence_span_value:
        evidence_hash_value = hashlib.sha256(evidence_span_value.encode("utf-8")).hexdigest()
    source_job_uuid = _uuid(source_job_id, field="source_job_id", required=False)
    dedupe_key = _dedupe_key(
        project_id=project_uuid,
        source_job_id=source_job_uuid,
        evidence_hash=evidence_hash_value,
        content=payload,
    )

    async with await get_db_session() as session:
        # A candidate writer must at least be able to read the Project.  This
        # keeps background routing from writing across an unrelated Project.
        await _project_for_actor(
            session, project_uuid, creator_uuid, permission="read"
        )
        scalar = getattr(session, "scalar", None)
        if dedupe_key and callable(scalar):
            existing = await scalar(
                select(DocsCandidate)
                .where(
                    DocsCandidate.dedupe_key == dedupe_key,
                    DocsCandidate.status.in_(ACTIVE_DEDUPE_STATUSES),
                )
                .limit(1)
            )
            if existing is not None:
                return _candidate_dict(existing)
        now = datetime.utcnow()
        candidate = DocsCandidate(
            id=uuid4(),
            project_id=project_uuid,
            source_type=source,
            content_json=payload,
            confidence=confidence_value,
            importance=importance_value,
            sensitivity=sensitivity_value,
            evidence_hash=evidence_hash_value,
            evidence_span=evidence_span_value,
            source_job_id=source_job_uuid,
            dedupe_key=dedupe_key,
            status="proposed",
            version=1,
            created_by=creator_uuid,
            created_at=now,
            updated_at=now,
        )
        session.add(candidate)
        try:
            await session.commit()
        except IntegrityError:
            await _rollback(session)
            if dedupe_key and callable(scalar):
                existing = await scalar(
                    select(DocsCandidate)
                    .where(
                        DocsCandidate.dedupe_key == dedupe_key,
                        DocsCandidate.status.in_(ACTIVE_DEDUPE_STATUSES),
                    )
                    .limit(1)
                )
                if existing is not None:
                    return _candidate_dict(existing)
            raise
        except Exception:
            await _rollback(session)
            raise
        return _candidate_dict(candidate)


async def list_candidates(
    *,
    project_id: UUID | str,
    actor_user_id: UUID | str,
    statuses: list[str] | tuple[str, ...] | set[str] | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List bounded candidate metadata visible to a Project reader."""

    project_uuid = _uuid(project_id, field="project_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    if status is not None:
        if statuses is not None:
            raise DocsCandidateValidation("use status or statuses, not both")
        statuses = [status]
    selected_statuses = None
    if statuses is not None:
        selected_statuses = {str(item).strip().lower() for item in statuses}
        if not selected_statuses.issubset(DOCS_CANDIDATE_STATUSES):
            raise DocsCandidateValidation("unsupported candidate status")
    try:
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
    except (TypeError, ValueError) as exc:
        raise DocsCandidateValidation("limit/offset must be integers") from exc

    async with await get_db_session() as session:
        await _project_for_actor(session, project_uuid, actor_uuid, permission="read")
        statement = (
            select(DocsCandidate)
            .where(DocsCandidate.project_id == project_uuid)
            .order_by(DocsCandidate.created_at.desc(), DocsCandidate.id)
            .offset(bounded_offset)
            .limit(bounded_limit)
        )
        if selected_statuses:
            statement = statement.where(DocsCandidate.status.in_(selected_statuses))
        result = await session.execute(statement)
        scalars = getattr(result, "scalars", None)
        rows = list(scalars().all()) if callable(scalars) else list(result.all())
        return [_candidate_dict(row) for row in rows]


async def _load_candidate_for_write(
    session: Any,
    candidate_id: UUID,
    actor_user_id: UUID,
    *,
    expected_project_id: UUID | None = None,
) -> tuple[DocsCandidate, Project]:
    try:
        candidate = await session.get(
            DocsCandidate, candidate_id, with_for_update=True
        )
    except TypeError:
        # Lightweight test/session doubles may not expose SQLAlchemy's
        # ``with_for_update`` keyword; production sessions take the row lock.
        candidate = await session.get(DocsCandidate, candidate_id)
    if candidate is None:
        raise DocsCandidateNotFound("candidate not found")
    candidate_project_id = _uuid(
        candidate.project_id,
        field="candidate.project_id",
    )
    # The HTTP path is part of the authorization boundary.  Do this check in
    # the same locked session before resolving the candidate's actual Project,
    # so an id from another Project cannot be approved/rejected through a
    # caller-controlled path parameter.
    if expected_project_id is not None and candidate_project_id != expected_project_id:
        raise DocsCandidateNotFound("candidate not found")
    project = await _project_for_actor(
        session,
        candidate_project_id,
        actor_user_id,
        permission="manage_settings",
    )
    return candidate, project


async def approve_candidate(
    candidate_id: UUID | str,
    *,
    actor_user_id: UUID | str,
    expected_version: int | None = None,
    version: int | None = None,
    target_node_id: UUID | str | None = None,
    expected_project_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Materialize a proposed candidate into canonical Project Information Docs."""

    candidate_uuid = _uuid(candidate_id, field="candidate_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    requested_target = _uuid(target_node_id, field="target_node_id", required=False)
    expected_project_uuid = _uuid(
        expected_project_id,
        field="expected_project_id",
        required=False,
    )
    async with await get_db_session() as session:
        candidate, project = await _load_candidate_for_write(
            session,
            candidate_uuid,
            actor_uuid,
            expected_project_id=expected_project_uuid,
        )
        expected = _expected_version(expected_version, version, int(candidate.version or 1))
        if candidate.status != "proposed":
            raise DocsCandidateConflict("candidate is not proposed")
        if int(candidate.version or 1) != expected:
            raise DocsCandidateConflict("candidate version conflict")

        if requested_target is not None:
            target = await session.get(KnowledgeNode, requested_target)
            if target is None or getattr(target, "archived_at", None) is not None:
                raise DocsCandidateNotFound("target Docs node not found")
            canonical_pointer = getattr(project, "knowledge_node_id", None)
            if canonical_pointer is not None and str(requested_target) != str(canonical_pointer):
                raise DocsCandidateNotFound(
                    "target Docs node is not the Project canonical node"
                )
            target_project_id = getattr(target, "project_id", None)
            if (
                target_project_id is not None
                and str(target_project_id) != str(project.id)
                and str(requested_target) != str(canonical_pointer or "")
            ):
                raise DocsCandidateNotFound("target Docs node belongs to another project")
            if not await can_write_node(session, target, actor_uuid):
                raise DocsCandidateNotFound("target Docs node is not writable")

        payload = candidate.content_json if isinstance(candidate.content_json, Mapping) else {}
        body = _text(payload.get("content"), limit=MAX_CONTENT_LENGTH, field="content")
        title = _text(payload.get("title"), limit=MAX_TITLE_LENGTH, field="title")
        section_hint = _text(
            payload.get("section_hint"), limit=MAX_SECTION_HINT_LENGTH, field="section_hint"
        )
        append_text = body or title
        if not append_text:
            raise DocsCandidateValidation("candidate has no canonical Docs content")
        source_refs = [
            {
                "type": "docs_candidate",
                "candidate_id": str(candidate.id),
                "evidence_hash": candidate.evidence_hash,
                "source_job_id": str(candidate.source_job_id)
                if candidate.source_job_id
                else None,
            }
        ]
        try:
            node = await update_project_information_doc(
                session,
                project=project,
                user_id=actor_uuid,
                append_text=append_text,
                section_heading=section_hint,
                operation="append",
                change_summary="Docs候補を案件情報Docsへ承認適用",
                source_refs=source_refs,
            )
            if requested_target is not None and getattr(node, "id", None) != requested_target:
                raise DocsCandidateConflict("target Docs node is not the canonical Project node")
            candidate.target_node_id = node.id
            candidate.status = "approved"
            candidate.version = int(candidate.version or 1) + 1
            candidate.updated_at = datetime.utcnow()
            await session.commit()
        except Exception:
            await _rollback(session)
            raise
        result = _candidate_dict(candidate)
    # The canonical Docs transaction committed above.  Scheduling outside it
    # prevents a failed approval rollback from creating a rebuild job, while a
    # transient queue failure does not hide the successful approval response.
    from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

    try:
        await enqueue_project_context_pack_rebuild(
            project.id,
            actor_uuid,
            "docs_candidate_approved",
        )
    except Exception:
        logger.exception(
            "Failed to enqueue ProjectContextPack rebuild after Docs candidate approval"
        )
    return result


async def reject_candidate(
    candidate_id: UUID | str,
    *,
    actor_user_id: UUID | str,
    expected_version: int | None = None,
    version: int | None = None,
    reason: str | None = None,
    expected_project_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Reject a proposed candidate with an optimistic version transition."""

    del reason  # The lifecycle row is the audit record; no raw rationale is stored.
    candidate_uuid = _uuid(candidate_id, field="candidate_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    expected_project_uuid = _uuid(
        expected_project_id,
        field="expected_project_id",
        required=False,
    )
    async with await get_db_session() as session:
        candidate, _project = await _load_candidate_for_write(
            session,
            candidate_uuid,
            actor_uuid,
            expected_project_id=expected_project_uuid,
        )
        expected = _expected_version(expected_version, version, int(candidate.version or 1))
        if candidate.status != "proposed":
            raise DocsCandidateConflict("candidate is not proposed")
        if int(candidate.version or 1) != expected:
            raise DocsCandidateConflict("candidate version conflict")
        candidate.status = "rejected"
        candidate.version = int(candidate.version or 1) + 1
        candidate.updated_at = datetime.utcnow()
        try:
            await session.commit()
        except Exception:
            await _rollback(session)
            raise
        return _candidate_dict(candidate)


class DocsCandidateService:
    """Small class facade for workers and integrations that prefer services."""

    @staticmethod
    async def create_candidate(**kwargs: Any) -> dict[str, Any]:
        return await create_candidate(**kwargs)

    @staticmethod
    async def list_candidates(**kwargs: Any) -> list[dict[str, Any]]:
        return await list_candidates(**kwargs)

    @staticmethod
    async def approve_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await approve_candidate(*args, **kwargs)

    @staticmethod
    async def reject_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await reject_candidate(*args, **kwargs)


__all__ = [
    "DOCS_CANDIDATE_SENSITIVITIES",
    "DOCS_CANDIDATE_STATUSES",
    "DocsCandidateConflict",
    "DocsCandidateError",
    "DocsCandidateNotFound",
    "DocsCandidateService",
    "DocsCandidateValidation",
    "approve_candidate",
    "create_candidate",
    "list_candidates",
    "reject_candidate",
    "sanitize_candidate_content",
]
