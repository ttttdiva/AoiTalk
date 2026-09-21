"""Authenticated HTTP boundary for Resolution Knowledge Capture.

The candidate/research/publisher services own state transitions, evidence
validation, and Docs invariants.  This module only authenticates the actor,
checks the Project boundary for project-scoped calls, validates bounded JSON
payloads, and projects safe DTOs.  The review/challenge endpoint may invoke
the isolated read-only curator, but it never performs candidate/question/Docs writes.
"""

from __future__ import annotations

import inspect
import logging
import sys
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..services.knowledge_capture_evidence import (
    resolve_knowledge_capture_review_chat_sessions,
    resolve_knowledge_capture_review_publication_target,
)
from ..services.knowledge_capture_publication_guard import (
    validate_publication_subtree_guard,
)

logger = logging.getLogger(__name__)

KnowledgeCaptureMode = Literal["off", "suggest", "auto"]
KnowledgeCaptureStatus = Literal[
    "queued",
    "researching",
    "needs_user",
    "draft_ready",
    "publishing",
    "published",
    "discarded",
    "dismissed",
    "stale",
    "failed",
    "retry_wait",
]
_KNOWLEDGE_CAPTURE_STATUSES = frozenset(
    {
        "queued",
        "researching",
        "needs_user",
        "draft_ready",
        "publishing",
        "published",
        "discarded",
        "dismissed",
        "stale",
        "failed",
        "retry_wait",
    }
)


class KnowledgeCaptureSettingsDTO(BaseModel):
    project_id: UUID
    mode: KnowledgeCaptureMode = "suggest"
    updated_by: UUID | None = None
    updated_at: str | None = None


class KnowledgeCaptureSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: KnowledgeCaptureMode


class KnowledgeCaptureSourceSummaryDTO(BaseModel):
    id: str
    kind: str
    relation: str | None = None
    title: str | None = None
    summary: str | None = None
    strength: str | None = None


class KnowledgeCaptureQuestionDTO(BaseModel):
    id: UUID
    candidate_id: UUID | None = None
    ordinal: int = 0
    status: str
    title: str
    message: str
    options: list[dict[str, str]] = Field(default_factory=list)
    candidate_version: int


class KnowledgeCaptureReviewChatSessionDTO(BaseModel):
    id: UUID
    title: str | None = Field(default=None, max_length=240)
    relation: Literal["direct_reference", "supporting_evidence"]
    primary: bool = False


class KnowledgeCapturePublicationTargetDTO(BaseModel):
    action: Literal["create", "update", "no_change"]
    node_id: UUID | None = None
    title: str | None = Field(default=None, max_length=240)


class KnowledgeCaptureReviewContextDTO(BaseModel):
    review_reason: str | None = Field(default=None, max_length=1000)
    source_task: dict[str, Any] | None = None
    chat_sessions: list[KnowledgeCaptureReviewChatSessionDTO] = Field(
        default_factory=list, max_length=8
    )
    publication_target: KnowledgeCapturePublicationTargetDTO | None = None


class KnowledgeCaptureCandidateDTO(BaseModel):
    id: UUID
    project_id: UUID
    seed_task: dict[str, Any] | None = None
    status: KnowledgeCaptureStatus
    version: int
    mode: KnowledgeCaptureMode | None = None
    reuse_score: int | None = None
    confidence: float | None = None
    knowledge_kind: str | None = None
    knowledge_semantic_key: str | None = None
    questions: list[KnowledgeCaptureQuestionDTO] = Field(default_factory=list)
    draft: dict[str, Any] | None = None
    sources: list[KnowledgeCaptureSourceSummaryDTO] = Field(default_factory=list)
    published: dict[str, Any] | None = None
    publication_guard_adoption_required: bool = False
    review_context: KnowledgeCaptureReviewContextDTO | None = None
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeCaptureCandidateListResponse(BaseModel):
    items: list[KnowledgeCaptureCandidateDTO] = Field(default_factory=list)
    total: int


class KnowledgeCaptureAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_version: int = Field(..., gt=0)
    option_id: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_answer_value(self) -> "KnowledgeCaptureAnswerRequest":
        if not (str(self.option_id or "").strip() or str(self.text or "").strip()):
            raise ValueError("option_id or text is required")
        return self


class KnowledgeCaptureReviewExchangeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(..., min_length=1, max_length=2000)


class KnowledgeCaptureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_version: int = Field(..., gt=0)
    text: str = Field(..., min_length=1, max_length=2000)
    thread: list[KnowledgeCaptureReviewExchangeDTO] = Field(
        default_factory=list, max_length=8
    )


class KnowledgeCaptureReviewResponse(BaseModel):
    candidate_version: int
    action: Literal["keep_question", "rephrase_question", "discard_candidate"]
    reply: str = Field(..., min_length=1, max_length=2000)
    rephrased_question: str | None = Field(default=None, max_length=2000)


_DRAFT_FIELDS = (
    "title",
    "problem",
    "preconditions",
    "symptoms",
    "root_cause",
    "resolution",
    "procedure",
    "verification",
    "pitfalls",
    "environment_constraints",
    "known_uncertainty",
)


class KnowledgeCaptureDraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_version: int = Field(..., gt=0)
    title: str | None = Field(default=None, max_length=240)
    problem: str | None = Field(default=None, max_length=4000)
    preconditions: list[str] | None = Field(default=None, max_length=20)
    symptoms: list[str] | None = Field(default=None, max_length=20)
    root_cause: str | None = Field(default=None, max_length=4000)
    resolution: str | None = Field(default=None, max_length=4000)
    procedure: list[dict[str, Any]] | None = Field(default=None, max_length=40)
    verification: list[dict[str, Any]] | None = Field(default=None, max_length=40)
    pitfalls: list[dict[str, Any]] | None = Field(default=None, max_length=40)
    environment_constraints: list[str] | None = Field(default=None, max_length=20)
    known_uncertainty: list[str] | None = Field(default=None, max_length=20)
    # Keep compatibility with clients that wrap the editable fields in a
    # ``draft`` object, while still filtering its keys through _DRAFT_FIELDS.
    draft: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_editable_field(self) -> "KnowledgeCaptureDraftPatch":
        explicit = self.model_dump(exclude={"expected_candidate_version", "draft"})
        if not any(value is not None for value in explicit.values()) and not self.draft:
            raise ValueError("at least one editable draft field is required")
        if self.draft is not None and any(key not in _DRAFT_FIELDS for key in self.draft):
            raise ValueError("draft contains unsupported fields")
        return self


class KnowledgeCaptureVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The public contract is expected_candidate_version.  Accept the design
    # packet's short spelling as an input alias for rollout compatibility, but
    # always pass the canonical name to domain services.
    expected_candidate_version: int | None = Field(default=None, gt=0)
    expected_version: int | None = Field(default=None, gt=0)
    legacy_version: int | None = Field(default=None, alias="version", gt=0)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_one_version(self) -> "KnowledgeCaptureVersionRequest":
        supplied = [
            value
            for value in (
                self.expected_candidate_version,
                self.expected_version,
                self.legacy_version,
            )
            if value is not None
        ]
        if not supplied:
            raise ValueError("expected_candidate_version is required")
        if len(set(supplied)) != 1:
            raise ValueError("candidate version values do not match")
        return self

    @property
    def version(self) -> int:
        return int(
            self.expected_candidate_version
            if self.expected_candidate_version is not None
            else self.expected_version
            if self.expected_version is not None
            else self.legacy_version
        )


class KnowledgeCaptureUnavailable(RuntimeError):
    """Raised when an optional rollout dependency is not installed."""


# Optional at composition time: the legacy server must remain bootable while
# a rolling deployment has not yet installed the candidate/research modules.
candidate_service: Any | None = None
publisher_service: Any | None = None
research_service: Any | None = None


def _load_candidate_service() -> Any | None:
    try:
        from ..services import knowledge_capture_candidate_service

        return knowledge_capture_candidate_service
    except (ImportError, ModuleNotFoundError):
        return None


def _candidate_module() -> Any | None:
    global candidate_service
    if candidate_service is None:
        candidate_service = _load_candidate_service()
    return candidate_service


def _load_publisher_service() -> Any | None:
    try:
        from ..services import knowledge_capture_publisher

        return knowledge_capture_publisher
    except (ImportError, ModuleNotFoundError):
        return None


def _publisher_module() -> Any | None:
    global publisher_service
    if publisher_service is None:
        publisher_service = _load_publisher_service()
    return publisher_service


def _load_research_service() -> Any | None:
    try:
        from ..services import knowledge_capture_research_service

        return knowledge_capture_research_service
    except (ImportError, ModuleNotFoundError):
        return None


def _research_module() -> Any | None:
    global research_service
    if research_service is None:
        research_service = _load_research_service()
    return research_service


def _row_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("to_safe_dict", "to_public_dict", "to_dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            result = method(include_sensitive=False)
        except TypeError:
            try:
                result = method()
            except Exception:
                continue
        if isinstance(result, Mapping):
            return dict(result)
    values = getattr(value, "__dict__", None)
    return dict(values) if isinstance(values, dict) else {}


def _value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return default


def _uuid(value: Any, field_name: str, *, required: bool = True) -> UUID | None:
    if value is None and not required:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        if required:
            raise ValueError(f"invalid {field_name}") from exc
        return None


def _bounded_text(value: Any, limit: int = 4000) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text[:limit] if text else None


def _compact_options(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(list(values)[:8], start=1):
        if isinstance(item, str):
            label = _bounded_text(item, 500)
            if not label:
                continue
            option_id = f"option_{index}"
        else:
            mapping = _row_mapping(item)
            if not mapping:
                continue
            option_id_value = next(
                (mapping.get(key) for key in ("id", "option_id", "value") if mapping.get(key)),
                None,
            )
            label_value = next(
                (
                    mapping.get(key)
                    for key in ("label", "text", "value", "title")
                    if mapping.get(key)
                ),
                None,
            )
            option_id = _bounded_text(option_id_value, 200)
            label = _bounded_text(label_value, 500)
            if not label and option_id:
                label = option_id
            if not label:
                continue
            if not option_id:
                option_id = f"option_{index}"

        normalized_id = option_id.casefold()
        if normalized_id in seen_ids:
            continue
        seen_ids.add(normalized_id)
        options.append({"id": option_id, "label": label})
    return options


def _compact_question(value: Any, candidate_id: UUID | None = None) -> dict[str, Any]:
    row = _row_mapping(value)
    question_id = _uuid(_value(row, "id", "question_id"), "question_id")
    options = _value(row, "options", "options_json")
    if (
        not isinstance(value, Mapping)
        and "options" not in row
        and "options_json" not in row
    ):
        # Some durable ORM projections intentionally omit encrypted fields
        # from their safe mapping.  Once the caller has enforced the
        # candidate/project ACL, options_json is the only encrypted question
        # field this DTO is allowed to read directly.
        options = getattr(value, "options_json", None)
    return {
        "id": question_id,
        "candidate_id": _uuid(
            _value(row, "candidate_id"), "candidate_id", required=False
        )
        or candidate_id,
        "ordinal": int(_value(row, "ordinal", default=0) or 0),
        "status": _bounded_text(_value(row, "status"), 32) or "pending",
        "title": _bounded_text(_value(row, "title"), 240) or "確認が必要です",
        "message": _bounded_text(
            _value(row, "message", "question"), 1000
        ) or "",
        "options": _compact_options(options),
        "candidate_version": int(
            _value(row, "candidate_version", "version", default=1) or 1
        ),
    }


def _compact_source(value: Any) -> dict[str, Any] | None:
    row = _row_mapping(value)
    source_id = _bounded_text(
        _value(row, "id", "evidence_id", "source_id", "source_key"), 240
    )
    kind = _bounded_text(_value(row, "kind", "source_type", "type"), 64)
    if not source_id or not kind:
        return None
    return {
        "id": source_id,
        "kind": kind,
        "relation": _bounded_text(_value(row, "relation"), 80),
        "title": _bounded_text(_value(row, "title", "name"), 240),
        "summary": _bounded_text(_value(row, "summary", "source_summary"), 600),
        "strength": _bounded_text(_value(row, "strength"), 40),
    }


def _compact_draft(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    # Only the resolution-knowledge-v1 public draft fields are projected.
    allowed = {
        "schema_version",
        "title",
        "semantic_key",
        "knowledge_kind",
        "problem",
        "preconditions",
        "symptoms",
        "root_cause",
        "resolution",
        "procedure",
        "verification",
        "pitfalls",
        "environment_constraints",
        "known_uncertainty",
        "source_evidence_ids",
        "publication",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            result[key] = _bounded_text(item, 4000)
        elif isinstance(item, list):
            result[key] = list(item[:40])
        elif key == "publication" and isinstance(item, Mapping):
            publication: dict[str, Any] = {}
            for field in (
                "action",
                "target_node_id",
                "target_revision_id",
                "adopted_by",
                "adopted_at",
            ):
                if item.get(field) is None:
                    continue
                limit = 64 if field in {"adopted_by", "adopted_at"} else 240
                publication[field] = _bounded_text(item.get(field), limit)
            result[key] = publication
        elif isinstance(item, (int, float, bool)) or item is None:
            result[key] = item
    return result or None


def _compact_published(value: Any) -> dict[str, Any] | None:
    row = _row_mapping(value)
    if not row:
        return None
    allowed = {
        "node_id",
        "published_node_id",
        "revision_id",
        "published_revision_id",
        "title",
        "action",
        "created",
        "updated",
        "no_change",
    }
    result = {key: row.get(key) for key in allowed if key in row}
    return result or None


def _publication_guard_adoption_required(row: Mapping[str, Any], status: str) -> bool:
    """Return the safe legacy-publication adoption marker without exposing a guard."""

    if status != "published":
        return False
    published_node_id = _value(row, "published_node_id")
    if published_node_id in (None, ""):
        published = _value(row, "published", "published_node")
        if isinstance(published, Mapping):
            published_node_id = _value(
                published,
                "published_node_id",
                "node_id",
                "target_node_id",
            )
    if published_node_id in (None, ""):
        return False
    draft = _value(row, "draft", "draft_json")
    if isinstance(draft, Mapping) and "publication" in draft:
        publication = draft.get("publication")
    else:
        publication = _value(row, "publication")
    if not isinstance(publication, Mapping):
        return True
    guard = publication.get("subtree_guard")
    # Validate the complete hidden guard against the published root.  A
    # non-empty mapping alone is not sufficient: malformed legacy values must
    # remain explicitly recoverable and are never projected.
    return not validate_publication_subtree_guard(guard, published_node_id)


def _compact_candidate(value: Any) -> dict[str, Any]:
    row = _row_mapping(value)
    candidate_id = _uuid(_value(row, "id", "candidate_id"), "candidate_id")
    project_id = _uuid(_value(row, "project_id"), "project_id")
    status = _bounded_text(_value(row, "status"), 32) or "queued"
    if status == "pending":
        # ``pending`` is the rolling-deployment alias for the public queued
        # state; retry_wait remains visible so operators can distinguish a
        # bounded backoff from a terminal failure.
        status = "queued"
    version = int(_value(row, "version", "candidate_version", default=1) or 1)
    if status not in _KNOWLEDGE_CAPTURE_STATUSES:
        # Unknown internal states are not terminal failures.  Keep the safe
        # response typed without falsely claiming that the candidate failed.
        status = "queued"
    questions_raw = _value(row, "questions", "question", default=[])
    if isinstance(questions_raw, Mapping):
        questions_raw = [questions_raw]
    sources_raw = _value(row, "sources", "source_summaries", "evidence_refs", default=[])
    if isinstance(sources_raw, Mapping):
        sources_raw = [sources_raw]
    sources = [
        source
        for source in (_compact_source(item) for item in (sources_raw or [])[:80])
        if source is not None
    ]
    seed_task = _row_mapping(_value(row, "seed_task", "seed_task_summary"))
    if seed_task:
        seed_task = {
            key: _bounded_text(seed_task.get(key), 600)
            for key in ("id", "title", "status", "completed_at")
            if seed_task.get(key) is not None
        }
    return {
        "id": candidate_id,
        "project_id": project_id,
        "seed_task": seed_task or None,
        "status": status,
        "version": max(1, version),
        "mode": _value(row, "mode", "mode_snapshot"),
        "reuse_score": _value(row, "reuse_score"),
        "confidence": _value(row, "confidence"),
        "knowledge_kind": _bounded_text(_value(row, "knowledge_kind"), 64),
        "knowledge_semantic_key": _bounded_text(
            _value(row, "knowledge_semantic_key", "semantic_key"), 240
        ),
        "questions": [
            _compact_question(item, candidate_id)
            for item in (questions_raw or [])[:4]
        ],
        "draft": _compact_draft(_value(row, "draft", "draft_json")),
        "sources": sources,
        "published": _compact_published(
            _value(row, "published", "publication", "published_node")
        ),
        "publication_guard_adoption_required": _publication_guard_adoption_required(
            row, status
        ),
        "created_at": _value(row, "created_at"),
        "updated_at": _value(row, "updated_at"),
    }


def _candidate_safe_row(candidate: Any) -> dict[str, Any]:
    """Build a body-safe mapping from the durable ORM candidate row."""

    safe_method = getattr(candidate, "to_safe_dict", None)
    if callable(safe_method):
        try:
            row = dict(safe_method(include_body=True))
        except TypeError:
            row = dict(safe_method())
    else:
        row = _row_mapping(candidate)

    seed_task = getattr(candidate, "seed_task", None)
    if seed_task is not None:
        row["seed_task"] = {
            "id": str(getattr(seed_task, "id", "")),
            "title": str(getattr(seed_task, "title", "") or "")[:600],
            "status": str(getattr(seed_task, "status", "") or "")[:64],
            "completed_at": (
                getattr(seed_task, "completed_at", None).isoformat()
                if getattr(seed_task, "completed_at", None) is not None
                else None
            ),
        }
    questions = getattr(candidate, "questions", None)
    if questions is not None:
        row["questions"] = [
            _compact_question(question, _uuid(candidate.id, "candidate_id"))
            for question in list(questions)[:4]
        ]
    row["sources"] = row.get("evidence_refs", [])
    row["draft"] = row.get("draft")
    row["mode"] = row.get("mode_snapshot")
    published = {
        key: row.get(key)
        for key in (
            "published_node_id",
            "published_revision_id",
            "target_node_id",
            "target_revision_id",
        )
        if row.get(key) is not None
    }
    row["published"] = published or None
    return row


async def _build_review_context(
    session: Any,
    *,
    candidate: Any,
    actor: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the bounded, live review-only provenance for one candidate detail."""

    row = _row_mapping(candidate)
    compact = _compact_candidate(row)
    project_id = _uuid(compact.get("project_id"), "project_id")
    source_task = compact.get("seed_task")

    review_reason = None
    for question in compact.get("questions", [])[:4]:
        if str(question.get("status") or "").casefold() != "pending":
            continue
        review_reason = _bounded_text(question.get("message"), 1000)
        break

    chat_sessions: list[dict[str, Any]] = []
    task_id = source_task.get("id") if isinstance(source_task, Mapping) else None
    if task_id:
        evidence_refs = _value(row, "evidence_refs", default=None)
        if not isinstance(evidence_refs, (list, tuple)):
            evidence_refs = _value(row, "sources", default=())
        chat_sessions = await resolve_knowledge_capture_review_chat_sessions(
            session,
            task_id=task_id,
            project_id=project_id,
            actor=actor,
            evidence_refs=tuple(
                item for item in (evidence_refs or ()) if isinstance(item, Mapping)
            ),
        )

    draft = compact.get("draft")
    publication = draft.get("publication") if isinstance(draft, Mapping) else None
    publication_target = await resolve_knowledge_capture_review_publication_target(
        session,
        project_id=project_id,
        actor=actor,
        publication=publication if isinstance(publication, Mapping) else None,
    )

    return {
        "review_reason": review_reason,
        "source_task": source_task,
        "chat_sessions": chat_sessions,
        "publication_target": publication_target,
    }


async def _read_project_candidates(
    session: Any,
    *,
    project_id: UUID,
    status: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Safe read adapter for the candidate domain's current public model."""

    if session is None:
        raise KnowledgeCaptureUnavailable("Database unavailable")
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..memory.models import KnowledgeCaptureCandidate

    statement = (
        select(KnowledgeCaptureCandidate)
        .options(
            selectinload(KnowledgeCaptureCandidate.questions),
            selectinload(KnowledgeCaptureCandidate.seed_task),
        )
        .where(KnowledgeCaptureCandidate.project_id == project_id)
        .order_by(
            KnowledgeCaptureCandidate.updated_at.desc(),
            KnowledgeCaptureCandidate.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    if status:
        normalized = "queued" if status == "pending" else status
        if normalized == "queued":
            statement = statement.where(
                KnowledgeCaptureCandidate.status.in_(("queued", "pending"))
            )
        else:
            statement = statement.where(KnowledgeCaptureCandidate.status == normalized)
    result = await session.execute(statement)
    return [_candidate_safe_row(row) for row in result.scalars().all()]


async def _read_candidate(
    session: Any,
    *,
    candidate_id: UUID,
) -> dict[str, Any]:
    """Safe detail adapter; callers must ACL-check its returned Project."""

    if session is None:
        raise KnowledgeCaptureUnavailable("Database unavailable")
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..memory.models import KnowledgeCaptureCandidate

    result = await session.execute(
        select(KnowledgeCaptureCandidate)
        .options(
            selectinload(KnowledgeCaptureCandidate.questions),
            selectinload(KnowledgeCaptureCandidate.seed_task),
        )
        .where(KnowledgeCaptureCandidate.id == candidate_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge Capture candidate not found")
    return _candidate_safe_row(row)


async def _candidate_project_id(session: Any, candidate_id: UUID) -> UUID:
    detail = await _read_candidate(session, candidate_id=candidate_id)
    return _uuid(detail.get("project_id"), "project_id")


async def _authorize_candidate_mutation(
    session: Any,
    *,
    candidate_id: UUID,
    user_id: UUID,
) -> UUID | None:
    """Preflight candidate ACL before invoking any state-changing service."""

    if session is None:
        return None
    project_id = await _candidate_project_id(session, candidate_id)
    await _require_project_permission(
        session,
        project_id=project_id,
        user_id=user_id,
        permission="write",
    )
    return project_id


def _pending_review_question(
    candidate: Mapping[str, Any],
    *,
    candidate_id: UUID,
    question_id: UUID,
) -> dict[str, Any]:
    questions = candidate.get("questions") or []
    if isinstance(questions, Mapping):
        questions = [questions]
    for item in list(questions)[:4]:
        row = _row_mapping(item)
        resolved = _uuid(row.get("id"), "question_id", required=False)
        if resolved != question_id:
            continue
        bound_candidate = _uuid(
            row.get("candidate_id"), "candidate_id", required=False
        )
        if bound_candidate is not None and bound_candidate != candidate_id:
            raise HTTPException(status_code=404, detail="Knowledge Capture question not found")
        if str(row.get("status") or "").casefold() != "pending":
            raise HTTPException(status_code=409, detail="Knowledge Capture question is no longer pending")
        return row
    raise HTTPException(status_code=404, detail="Knowledge Capture question not found")


async def _commit_session(session: Any) -> None:
    if session is None:
        return
    commit = getattr(session, "commit", None)
    if callable(commit):
        result = commit()
        if inspect.isawaitable(result):
            await result


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:500]


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    status_code = getattr(exc, "status_code", None)
    name = type(exc).__name__.casefold()
    if status_code is None:
        if "conflict" in name or "stale" in name:
            status_code = 409
        elif "notfound" in name or "not_found" in name:
            status_code = 404
        elif "permission" in name or "forbidden" in name or "unauthorized" in name:
            status_code = 403
        elif "validation" in name or "invalid" in name:
            status_code = 400
        elif "unavailable" in name:
            status_code = 503
    if status_code in {400, 401, 403, 404, 409, 422, 503}:
        raise HTTPException(status_code=status_code, detail=_safe_error_message(exc)) from exc
    raise exc


async def _open_session(get_db_manager: Any) -> Any | None:
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    getter = getattr(manager, "get_session", None)
    if not callable(getter):
        return None
    result = getter()
    return await result if inspect.isawaitable(result) else result


async def _close_session(session: Any) -> None:
    if session is None:
        return
    close = getattr(session, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


async def _require_project_permission(
    session: Any,
    *,
    project_id: UUID,
    user_id: UUID,
    permission: str,
) -> None:
    if session is None:
        return
    from ..memory.project_repository import ProjectRepository

    if not await ProjectRepository.has_permission(
        session,
        project_id=project_id,
        user_id=user_id,
        permission=permission,
    ):
        raise HTTPException(status_code=403, detail="Project permission denied")


def _callable_from_module(module: Any, names: tuple[str, ...]) -> Any | None:
    if module is None:
        return None
    for name in names:
        target = getattr(module, name, None)
        if callable(target):
            return target
    return None


def _service_instance(module: Any, *, session_factory: Any, config: Any) -> Any | None:
    if module is None:
        return None
    for class_name in (
        "KnowledgeCaptureCandidateService",
        "KnowledgeCaptureService",
        "KnowledgeCapturePublisher",
    ):
        cls = getattr(module, class_name, None)
        if not callable(cls):
            continue
        for kwargs in (
            {"session_factory": session_factory, "config": config},
            {"get_db_session": session_factory, "config": config},
            {"config": config},
            {},
        ):
            try:
                signature = inspect.signature(cls)
                if not any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                ):
                    kwargs = {
                        key: value
                        for key, value in kwargs.items()
                        if key in signature.parameters
                    }
                return cls(**kwargs)
            except (TypeError, ValueError):
                continue
    return None


async def _invoke(
    module: Any,
    names: tuple[str, ...],
    *,
    session: Any,
    session_factory: Any,
    config: Any,
    kwargs: dict[str, Any],
) -> Any:
    target = _callable_from_module(module, names)
    if target is None:
        target = _callable_from_module(
            _service_instance(module, session_factory=session_factory, config=config),
            names,
        )
    if target is None:
        raise KnowledgeCaptureUnavailable(
            f"Knowledge Capture service operation unavailable: {names[0]}"
        )
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and not any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        kwargs = {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
    result = target(**kwargs)
    return await result if inspect.isawaitable(result) else result


async def _close_after_mutation(
    get_db_manager: Any,
    *,
    candidate_id: UUID,
    project_id: UUID | None = None,
    question_id: UUID | None = None,
) -> None:
    session = await _open_session(get_db_manager)
    if session is None:
        return
    try:
        from ..services.task_management.notifications import (
            close_knowledge_capture_notifications,
        )

        await close_knowledge_capture_notifications(
            session,
            candidate_id=candidate_id,
            project_id=project_id,
            question_id=question_id,
            commit=True,
        )
    except Exception:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            result = rollback()
            if inspect.isawaitable(result):
                await result
        logger.warning(
            "Knowledge Capture notification cancellation failed: exception_type=%s",
            type(sys.exc_info()[1]).__name__ if sys.exc_info()[1] else "unknown",
        )
    finally:
        await _close_session(session)


def create_knowledge_capture_router(
    get_db_manager: Any,
    get_user_from_request: Any,
    require_auth_dependency: Any,
    *,
    config: Any = None,
) -> APIRouter:
    """Create the authenticated Knowledge Capture router."""

    router = APIRouter(tags=["knowledge-capture"])

    async def actor(request: Request) -> tuple[dict[str, Any], UUID]:
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            user_id = _uuid(user_info.get("id"), "user_id")
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Not authenticated") from exc
        return user_info, user_id

    async def session_factory() -> Any:
        session = await _open_session(get_db_manager)
        if session is None:
            raise KnowledgeCaptureUnavailable("Database unavailable")
        return session

    common_kwargs = lambda *, session, user_id, project_id=None, candidate_id=None, question_id=None, expected_version=None, **extra: {
        "session": session,
        "user_id": user_id,
        "actor_user_id": user_id,
        "project_id": project_id,
        "expected_project_id": project_id,
        "candidate_id": candidate_id,
        "question_id": question_id,
        "expected_candidate_version": expected_version,
        "expected_version": expected_version,
        "updated_by_user_id": user_id,
        "answered_by_user_id": user_id,
        "dismissed_by_user_id": user_id,
        **extra,
    }

    @router.get(
        "/api/projects/{project_id}/knowledge-capture/settings",
        response_model=KnowledgeCaptureSettingsDTO,
    )
    async def get_knowledge_capture_settings(
        project_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            await _require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            result = await _invoke(
                _candidate_module(),
                (
                    "get_project_knowledge_capture_mode",
                    "get_project_settings",
                    "get_settings",
                    "get_or_create_setting",
                ),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    project_id=project_id,
                ),
            )
            row = _row_mapping(result)
            mode = result if isinstance(result, str) else _value(row, "mode")
            return {
                "project_id": _uuid(
                    _value(row, "project_id", default=project_id), "project_id"
                ),
                "mode": mode or "suggest",
                "updated_by": _uuid(
                    _value(row, "updated_by"), "updated_by", required=False
                ),
                "updated_at": _value(row, "updated_at"),
            }
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.patch(
        "/api/projects/{project_id}/knowledge-capture/settings",
        response_model=KnowledgeCaptureSettingsDTO,
    )
    async def patch_knowledge_capture_settings(
        project_id: UUID,
        payload: KnowledgeCaptureSettingsPatch,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            await _require_project_permission(
                session,
                project_id=project_id,
                user_id=user_id,
                permission="manage_settings",
            )
            result = await _invoke(
                _candidate_module(),
                (
                    "upsert_project_knowledge_capture_setting",
                    "update_project_settings",
                    "update_settings",
                    "set_mode",
                    "update_setting",
                ),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    project_id=project_id,
                    mode=payload.mode,
                ),
            )
            await _commit_session(session)
            row = _row_mapping(result)
            return {
                "project_id": _uuid(
                    _value(row, "project_id", default=project_id), "project_id"
                ),
                "mode": _value(row, "mode", default=payload.mode),
                "updated_by": _uuid(
                    _value(row, "updated_by", default=user_id), "updated_by", required=False
                ),
                "updated_at": _value(row, "updated_at"),
            }
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.get(
        "/api/projects/{project_id}/knowledge-capture/candidates",
        response_model=KnowledgeCaptureCandidateListResponse,
    )
    async def list_knowledge_capture_candidates(
        project_id: UUID,
        request: Request,
        status: KnowledgeCaptureStatus | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            await _require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            try:
                result = await _invoke(
                    _candidate_module(),
                    ("list_project_candidates", "list_candidates"),
                    session=session,
                    session_factory=session_factory,
                    config=config,
                    kwargs=common_kwargs(
                        session=session,
                        user_id=user_id,
                        project_id=project_id,
                        status=status,
                        limit=limit,
                        offset=offset,
                    ),
                )
            except KnowledgeCaptureUnavailable:
                # The current candidate domain intentionally exposes enqueue
                # and mutation primitives, not a broad list API.  This
                # adapter performs the bounded, ACL-checked safe projection
                # at the HTTP boundary without reading encrypted bodies.
                result = await _read_project_candidates(
                    session,
                    project_id=project_id,
                    status=status,
                    limit=limit,
                    offset=offset,
                )
            if isinstance(result, Mapping):
                rows = result.get("items", result.get("candidates", []))
            else:
                rows = result or []
            items = [_compact_candidate(item) for item in list(rows)[:limit]]
            return {"items": items, "total": len(items)}
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.get(
        "/api/knowledge-capture/candidates/{candidate_id}",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def get_knowledge_capture_candidate(
        candidate_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        user_info, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            # Load the candidate's Project binding and enforce ACL before any
            # optional domain detail adapter runs.  This prevents a guessed
            # candidate UUID from reaching a service that might do extra
            # work before its own authorization check.
            preflight = None
            if session is not None:
                preflight = await _read_candidate(session, candidate_id=candidate_id)
                candidate_project_id = _uuid(
                    _value(preflight, "project_id"), "project_id"
                )
                await _require_project_permission(
                    session,
                    project_id=candidate_project_id,
                    user_id=user_id,
                    permission="read",
                )
            try:
                result = await _invoke(
                    _candidate_module(),
                    ("get_candidate_detail", "get_candidate", "get_candidate_by_id"),
                    session=session,
                    session_factory=session_factory,
                    config=config,
                    kwargs=common_kwargs(
                        session=session,
                        user_id=user_id,
                        candidate_id=candidate_id,
                    ),
                )
            except KnowledgeCaptureUnavailable:
                result = preflight
            if result is None:
                raise KnowledgeCaptureUnavailable("Knowledge Capture candidate unavailable")
            detail = _compact_candidate(result)
            if session is not None:
                detail["review_context"] = await _build_review_context(
                    session,
                    candidate=preflight if preflight is not None else result,
                    actor=user_info,
                )
            return detail
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.post(
        "/api/knowledge-capture/candidates/{candidate_id}/questions/{question_id}/answer",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def answer_knowledge_capture_question(
        candidate_id: UUID,
        question_id: UUID,
        payload: KnowledgeCaptureAnswerRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            project_id = await _authorize_candidate_mutation(
                session,
                candidate_id=candidate_id,
                user_id=user_id,
            )
            # Option IDs identify a durable choice; they are not answer prose.
            answer_value = (payload.text or "").strip() or None
            result = await _invoke(
                _candidate_module(),
                ("answer_question", "answer_candidate_question"),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    question_id=question_id,
                    # The public CAS is the candidate version.  The question
                    # row has its own internal version; omitting it lets the
                    # domain service use the current pending-row version.
                    expected_version=None,
                    expected_candidate_id=candidate_id,
                    expected_candidate_version=payload.expected_candidate_version,
                    answer=answer_value,
                    option_id=payload.option_id,
                    text=payload.text,
                    answer_text=payload.text,
                ),
            )
            await _commit_session(session)
            await _close_after_mutation(
                get_db_manager,
                candidate_id=candidate_id,
                project_id=project_id,
                question_id=question_id,
            )
            if session is not None:
                result = await _read_candidate(session, candidate_id=candidate_id)
            return _compact_candidate(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.post(
        "/api/projects/{project_id}/knowledge-capture/candidates/{candidate_id}/questions/{question_id}/review",
        response_model=KnowledgeCaptureReviewResponse,
    )
    async def review_knowledge_capture_question(
        project_id: UUID,
        candidate_id: UUID,
        question_id: UUID,
        payload: KnowledgeCaptureReviewRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        user_info, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            if session is None:
                raise KnowledgeCaptureUnavailable("Database unavailable")
            await _require_project_permission(
                session,
                project_id=project_id,
                user_id=user_id,
                permission="read",
            )
            candidate = await _read_candidate(session, candidate_id=candidate_id)
            candidate_project_id = _uuid(candidate.get("project_id"), "project_id")
            if candidate_project_id != project_id:
                raise HTTPException(status_code=404, detail="Knowledge Capture candidate not found")
            try:
                live_version = int(candidate.get("version") or 0)
            except (TypeError, ValueError):
                live_version = 0
            if live_version != payload.expected_candidate_version:
                raise HTTPException(status_code=409, detail="Knowledge Capture candidate version conflict")
            if str(candidate.get("status") or "").casefold() != "needs_user":
                raise HTTPException(status_code=409, detail="Knowledge Capture candidate is no longer awaiting review")
            question = _pending_review_question(
                candidate, candidate_id=candidate_id, question_id=question_id
            )
            result = await _invoke(
                _research_module(),
                ("review_candidate_question",),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs={
                    "session_factory": session_factory,
                    "session": session,
                    "candidate": candidate,
                    "question": question,
                    "actor": user_info,
                    "expected_candidate_version": payload.expected_candidate_version,
                    "text": payload.text,
                    "thread": [item.model_dump() for item in payload.thread],
                    "config": config,
                },
            )
            # Stateless boundary: no commit, answer call, question transition,
            # notification mutation, or Docs publication is performed here.
            return _row_mapping(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.patch(
        "/api/knowledge-capture/candidates/{candidate_id}/draft",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def patch_knowledge_capture_draft(
        candidate_id: UUID,
        payload: KnowledgeCaptureDraftPatch,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            await _authorize_candidate_mutation(
                session,
                candidate_id=candidate_id,
                user_id=user_id,
            )
            updates = {
                key: value
                for key, value in payload.model_dump(exclude_none=True).items()
                if key not in {"expected_candidate_version", "draft"}
            }
            if payload.draft:
                updates.update(payload.draft)
            result = await _invoke(
                _candidate_module(),
                (
                    "edit_candidate",
                    "update_draft",
                    "edit_draft",
                    "update_candidate_draft",
                ),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    expected_version=payload.expected_candidate_version,
                    draft_json=updates,
                    draft=updates,
                    updates=updates,
                ),
            )
            await _commit_session(session)
            await _close_after_mutation(get_db_manager, candidate_id=candidate_id)
            if session is not None:
                result = await _read_candidate(session, candidate_id=candidate_id)
            return _compact_candidate(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.post(
        "/api/knowledge-capture/candidates/{candidate_id}/publish",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def publish_knowledge_capture_candidate(
        candidate_id: UUID,
        payload: KnowledgeCaptureVersionRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            project_id = await _authorize_candidate_mutation(
                session,
                candidate_id=candidate_id,
                user_id=user_id,
            )
            module = _publisher_module() or _candidate_module()
            result = await _invoke(
                module,
                ("publish_candidate", "publish", "publish_knowledge_capture"),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    expected_version=payload.version,
                    expected_candidate_version=payload.version,
                ),
            )
            await _commit_session(session)
            row = _row_mapping(result)
            project_id = project_id or _uuid(
                _value(row, "project_id"), "project_id", required=False
            )
            await _close_after_mutation(
                get_db_manager,
                candidate_id=candidate_id,
                project_id=project_id,
            )
            if session is not None:
                result = await _read_candidate(session, candidate_id=candidate_id)
            return _compact_candidate(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.post(
        "/api/knowledge-capture/candidates/{candidate_id}/adopt-publication-guard",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def adopt_knowledge_capture_publication_guard(
        candidate_id: UUID,
        payload: KnowledgeCaptureVersionRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            project_id = None
            if session is not None:
                project_id = await _candidate_project_id(session, candidate_id)
                # Guard adoption is an explicit publication-control operation,
                # so a normal Project writer is not sufficient.
                await _require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="manage_settings",
                )
            module = _publisher_module() or _candidate_module()
            result = await _invoke(
                module,
                ("adopt_publication_subtree_guard",),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    expected_version=payload.version,
                    expected_candidate_version=payload.version,
                ),
            )
            await _commit_session(session)
            if session is not None:
                result = await _read_candidate(session, candidate_id=candidate_id)
            return _compact_candidate(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    @router.post(
        "/api/knowledge-capture/candidates/{candidate_id}/dismiss",
        response_model=KnowledgeCaptureCandidateDTO,
    )
    async def dismiss_knowledge_capture_candidate(
        candidate_id: UUID,
        payload: KnowledgeCaptureVersionRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        _, user_id = await actor(request)
        session = await _open_session(get_db_manager)
        try:
            project_id = await _authorize_candidate_mutation(
                session,
                candidate_id=candidate_id,
                user_id=user_id,
            )
            result = await _invoke(
                _candidate_module(),
                ("dismiss_candidate", "dismiss", "transition_candidate"),
                session=session,
                session_factory=session_factory,
                config=config,
                kwargs=common_kwargs(
                    session=session,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    expected_version=payload.version,
                    reason=payload.reason,
                    target_status="dismissed",
                    dismissed_by_user_id=user_id,
                ),
            )
            await _commit_session(session)
            row = _row_mapping(result)
            project_id = project_id or _uuid(
                _value(row, "project_id"), "project_id", required=False
            )
            await _close_after_mutation(
                get_db_manager,
                candidate_id=candidate_id,
                project_id=project_id,
            )
            if session is not None:
                result = await _read_candidate(session, candidate_id=candidate_id)
            return _compact_candidate(result)
        except Exception as exc:
            _raise_http_error(exc)
        finally:
            await _close_session(session)

    return router


__all__ = [
    "KnowledgeCaptureAnswerRequest",
    "KnowledgeCaptureCandidateDTO",
    "KnowledgeCaptureCandidateListResponse",
    "KnowledgeCapturePublicationTargetDTO",
    "KnowledgeCaptureReviewChatSessionDTO",
    "KnowledgeCaptureReviewContextDTO",
    "KnowledgeCaptureDraftPatch",
    "KnowledgeCaptureReviewExchangeDTO",
    "KnowledgeCaptureReviewRequest",
    "KnowledgeCaptureReviewResponse",
    "KnowledgeCaptureSettingsDTO",
    "KnowledgeCaptureSettingsPatch",
    "KnowledgeCaptureVersionRequest",
    "create_knowledge_capture_router",
]
