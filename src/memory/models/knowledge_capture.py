"""Durable state for Resolution Knowledge Capture.

The capture rows are workflow state, not a second transcript or research
store.  Potentially sensitive candidate/question bodies use the same
application-layer encryption helpers as the rest of the memory models.  The
safe DTOs intentionally expose identifiers, lifecycle metadata, and bounded
references while omitting research/draft bodies unless a caller explicitly
opts in.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, synonym

from .base import Base, _encrypted_json_property, _encrypted_text_property


KNOWLEDGE_CAPTURE_MODES = frozenset({"off", "suggest", "auto"})
DEFAULT_KNOWLEDGE_CAPTURE_MODE = "suggest"

# ``pending`` is the durable enqueue state.  ``researching`` is also the
# claim/lease state; a separate transient ``claimed`` state is unnecessary
# and would make recovery harder to reason about.
KNOWLEDGE_CAPTURE_CANDIDATE_STATES = frozenset(
    {
        "queued",
        # ``pending`` was used by the first bounded implementation.  Keep it
        # legal for rolling deployments, but serialize/use ``queued`` below.
        "pending",
        "researching",
        "needs_user",
        "draft_ready",
        "approved",
        "published",
        "dismissed",
        "discarded",
        "superseded",
        "retry_wait",
        "failed",
    }
)
KNOWLEDGE_CAPTURE_CANDIDATE_TERMINAL_STATES = frozenset(
    {"published", "dismissed", "discarded", "superseded", "failed"}
)
KNOWLEDGE_CAPTURE_QUESTION_STATES = frozenset(
    {"pending", "answered", "dismissed"}
)
KNOWLEDGE_CAPTURE_QUESTION_TERMINAL_STATES = frozenset(
    {"answered", "dismissed"}
)
MAX_KNOWLEDGE_CAPTURE_QUESTION_ROUNDS = 2
KNOWLEDGE_CAPTURE_QUEUED_STATE = "queued"


_SECRET_KEY_MARKERS = frozenset(
    {
        "secret",
        "token",
        "password",
        "credential",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "private_key",
        "access_key",
        "refresh_key",
        "client_secret",
        "transcript",
        "raw_response",
        "provider_response",
    }
)
_SENSITIVE_URL_RE = re.compile(
    r"(?:https?|ftp)://[^\s]+(?:[?&](?:token|secret|password|key|sig|signature|credential)="
    r"[^\s&]+|@[^\s/]+)",
    re.IGNORECASE,
)
_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:users?|home|tmp|var|etc|appdata)(?:[\\/]|$)",
    re.IGNORECASE,
)


def normalize_knowledge_capture_mode(value: Any) -> str:
    """Return one legal setting mode, defaulting absent values to ``suggest``."""

    normalized = str(value or DEFAULT_KNOWLEDGE_CAPTURE_MODE).strip().lower()
    if normalized not in KNOWLEDGE_CAPTURE_MODES:
        raise ValueError(f"invalid knowledge capture mode: {value}")
    return normalized


def validate_candidate_transition(current: str, target: str) -> None:
    """Validate a candidate lifecycle edge.

    The worker may retry a queued/researching item, a user answer resumes it
    through ``pending``, and terminal rows are never silently reopened.
    """

    current = normalize_candidate_state(current)
    target = normalize_candidate_state(target)
    if current not in KNOWLEDGE_CAPTURE_CANDIDATE_STATES:
        raise ValueError(f"invalid knowledge capture candidate state: {current}")
    if target not in KNOWLEDGE_CAPTURE_CANDIDATE_STATES:
        raise ValueError(f"invalid knowledge capture candidate state: {target}")
    legal = {
        "queued": {
            "queued",
            "researching",
            "needs_user",
            "dismissed",
            "discarded",
            "superseded",
        },
        "researching": {
            "researching",
            "queued",
            "needs_user",
            "draft_ready",
            "retry_wait",
            "failed",
            "dismissed",
            "discarded",
            "superseded",
        },
        "needs_user": {"needs_user", "queued", "draft_ready", "dismissed", "discarded", "superseded"},
        "draft_ready": {
            "draft_ready",
            "needs_user",
            "approved",
            "queued",
            "dismissed",
            "discarded",
            "superseded",
        },
        "approved": {"approved", "published", "dismissed", "discarded", "superseded"},
        "published": {"published", "superseded"},
        "dismissed": {"dismissed"},
        "superseded": {"superseded"},
        "retry_wait": {"retry_wait", "queued", "researching", "failed", "dismissed", "discarded"},
        "failed": {"failed", "retry_wait", "dismissed", "discarded"},
    }
    if target not in legal[current]:
        raise ValueError(f"illegal knowledge capture candidate transition: {current} -> {target}")


def normalize_candidate_state(value: Any) -> str:
    """Map the rolling-deployment ``pending`` alias to canonical ``queued``."""

    normalized = str(value or KNOWLEDGE_CAPTURE_QUEUED_STATE).strip().lower()
    if normalized == "pending":
        return KNOWLEDGE_CAPTURE_QUEUED_STATE
    return normalized


def validate_question_transition(current: str, target: str) -> None:
    """Validate a question lifecycle edge."""

    current = str(current or "pending").strip().lower()
    target = str(target or "").strip().lower()
    if current not in KNOWLEDGE_CAPTURE_QUESTION_STATES:
        raise ValueError(f"invalid knowledge capture question state: {current}")
    if target not in KNOWLEDGE_CAPTURE_QUESTION_STATES:
        raise ValueError(f"invalid knowledge capture question state: {target}")
    legal = {
        "pending": {"pending", "answered", "dismissed"},
        "answered": {"answered"},
        "dismissed": {"dismissed"},
    }
    if target not in legal[current]:
        raise ValueError(f"illegal knowledge capture question transition: {current} -> {target}")


def _safe_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text_value = str(value).replace("\x00", "").strip()
    if not text_value:
        return None
    lowered = text_value.casefold()
    if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
        return None
    if _SENSITIVE_URL_RE.search(text_value) or _PATH_RE.search(text_value):
        return None
    return text_value[:limit]


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Keep a small metadata-only projection safe for operator/UI DTOs."""

    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_text(value, limit=512)
    if isinstance(value, (list, tuple, set, frozenset)):
        projected: list[Any] = []
        for item in list(value)[:64]:
            item_value = _safe_json(item, depth=depth + 1)
            if item_value is not None:
                projected.append(item_value)
        return projected
    if isinstance(value, dict):
        projected_dict: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:64]:
            key = str(raw_key)
            if any(marker in key.casefold() for marker in _SECRET_KEY_MARKERS):
                continue
            item_value = _safe_json(item, depth=depth + 1)
            if item_value is not None:
                projected_dict[key[:96]] = item_value
        return projected_dict
    return None


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ProjectKnowledgeCaptureSetting(Base):
    """Project-local capture policy.  An absent row means ``suggest``."""

    __tablename__ = "project_knowledge_capture_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    mode = Column(
        String(16),
        nullable=False,
        default=DEFAULT_KNOWLEDGE_CAPTURE_MODE,
        server_default=DEFAULT_KNOWLEDGE_CAPTURE_MODE,
        index=True,
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    project = relationship("Project", foreign_keys=[project_id])
    updated_by_user = relationship("User", foreign_keys=[updated_by])

    __table_args__ = (
        CheckConstraint(
            "mode IN ('off', 'suggest', 'auto')",
            name="ck_project_knowledge_capture_settings_mode",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_project_knowledge_capture_settings_version",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "project_id": _id(self.project_id),
            "mode": normalize_knowledge_capture_mode(self.mode),
            "version": int(self.version or 1),
            "updated_by": _id(self.updated_by),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    to_dict = to_safe_dict


class KnowledgeCaptureCandidate(Base):
    """One deduplicated completed-Task capture workflow."""

    __tablename__ = "knowledge_capture_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seed_task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Compatibility spelling for callers that treat the seed as the task.
    task_id = synonym("seed_task_id")
    task_activity_id = Column(
        UUID(as_uuid=True),
        ForeignKey("task_activities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trigger_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    completion_fingerprint = Column(String(64), nullable=False, unique=True, index=True)
    knowledge_semantic_key = Column(String(255), nullable=True, index=True)
    terminal_status = Column(String(32), nullable=False, default="closed", server_default="closed")
    status = Column(
        String(32),
        nullable=False,
        default=KNOWLEDGE_CAPTURE_QUEUED_STATE,
        server_default=KNOWLEDGE_CAPTURE_QUEUED_STATE,
        index=True,
    )
    mode_snapshot = Column(
        String(16),
        nullable=False,
        default=DEFAULT_KNOWLEDGE_CAPTURE_MODE,
        server_default=DEFAULT_KNOWLEDGE_CAPTURE_MODE,
        index=True,
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    evidence_digest = Column(String(64), nullable=True, index=True)
    evidence_refs = Column(JSON, nullable=False, default=list, server_default="[]")
    reuse_score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    question_rounds = Column(Integer, nullable=False, default=0, server_default="0")
    user_edited = Column(Boolean, nullable=False, default=False, server_default="false")
    _research_json = Column("research_json", JSON, nullable=True)
    research_json = _encrypted_json_property(
        "_research_json", "knowledge_capture_candidates.research_json"
    )
    _draft_json = Column("draft_json", JSON, nullable=True)
    draft_json = _encrypted_json_property(
        "_draft_json", "knowledge_capture_candidates.draft_json"
    )
    _answers_json = Column("answers_json", JSON, nullable=False, default=list, server_default="[]")
    answers_json = _encrypted_json_property(
        "_answers_json", "knowledge_capture_candidates.answers_json"
    )
    published_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    published_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=5, server_default="5")
    lease_owner = Column(String(128), nullable=True, index=True)
    lease_token = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    heartbeat_at = Column(DateTime, nullable=True)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    last_error_code = Column(String(96), nullable=True)
    last_error_message = Column(String(512), nullable=True)
    completed_at = Column(DateTime, nullable=True, index=True)
    dismissed_at = Column(DateTime, nullable=True)
    dismissed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        index=True,
    )

    project = relationship("Project", foreign_keys=[project_id])
    seed_task = relationship("Task", foreign_keys=[seed_task_id])
    task_activity = relationship("TaskActivity", foreign_keys=[task_activity_id])
    trigger_user = relationship("User", foreign_keys=[trigger_user_id])
    dismissed_by_user = relationship("User", foreign_keys=[dismissed_by])
    questions = relationship(
        "KnowledgeCaptureQuestion",
        back_populates="candidate",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="KnowledgeCaptureQuestion.round_number",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'pending', 'researching', 'needs_user', 'draft_ready', 'approved', 'published', 'dismissed', 'discarded', 'superseded', 'retry_wait', 'failed')",
            name="ck_knowledge_capture_candidates_status",
        ),
        CheckConstraint(
            "terminal_status IN ('closed', 'cancelled')",
            name="ck_knowledge_capture_candidates_terminal_status",
        ),
        CheckConstraint(
            "version >= 1 AND attempt_count >= 0 AND max_attempts BETWEEN 1 AND 20 AND question_rounds BETWEEN 0 AND 2",
            name="ck_knowledge_capture_candidates_counters",
        ),
        CheckConstraint(
            "mode_snapshot IN ('off', 'suggest', 'auto')",
            name="ck_knowledge_capture_candidates_mode_snapshot",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_knowledge_capture_candidates_lease_triplet",
        ),
        Index(
            "ix_knowledge_capture_candidates_project_status_updated",
            "project_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_knowledge_capture_candidates_claimable",
            "status",
            "next_retry_at",
            "lease_expires_at",
        ),
    )

    def to_safe_dict(self, *, include_body: bool = False) -> Dict[str, Any]:
        now = datetime.utcnow()
        payload: Dict[str, Any] = {
            "id": _id(self.id),
            "project_id": _id(self.project_id),
            "seed_task_id": _id(self.seed_task_id),
            "task_id": _id(self.seed_task_id),
            "task_activity_id": _id(self.task_activity_id),
            "trigger_user_id": _id(self.trigger_user_id),
            "completion_fingerprint": self.completion_fingerprint,
            "terminal_status": self.terminal_status,
            "status": normalize_candidate_state(self.status),
            "version": int(self.version or 1),
            "knowledge_semantic_key": _safe_text(self.knowledge_semantic_key, limit=255),
            "mode_snapshot": normalize_knowledge_capture_mode(self.mode_snapshot),
            "evidence_digest": self.evidence_digest,
            "evidence_refs": _safe_json(self.evidence_refs or []) or [],
            "reuse_score": self.reuse_score,
            "confidence": self.confidence,
            "question_rounds": int(self.question_rounds or 0),
            "user_edited": bool(self.user_edited),
            "published_node_id": _id(self.published_node_id),
            "target_node_id": _id(self.target_node_id),
            "target_revision_id": _id(self.target_revision_id),
            "published_revision_id": _id(self.published_revision_id),
            "attempt_count": int(self.attempt_count or 0),
            "max_attempts": int(self.max_attempts or 5),
            "lease_owner": _safe_text(self.lease_owner, limit=128),
            "lease_active": bool(
                self.lease_owner
                and self.lease_expires_at is not None
                and self.lease_expires_at > now
            ),
            "lease_expires_at": _iso(self.lease_expires_at),
            "heartbeat_at": _iso(self.heartbeat_at),
            "next_retry_at": _iso(self.next_retry_at),
            "last_error_code": _safe_text(self.last_error_code, limit=96),
            "last_error_message": _safe_text(self.last_error_message, limit=512),
            "completed_at": _iso(self.completed_at),
            "dismissed_at": _iso(self.dismissed_at),
            "dismissed_by": _id(self.dismissed_by),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        # Research, draft, and answer material is intentionally absent unless
        # an explicitly trusted caller asks for a bounded body projection.
        if include_body:
            payload["research"] = _safe_json(self.research_json) or {}
            payload["draft"] = _safe_json(self.draft_json) or {}
            payload["answers"] = _safe_json(self.answers_json or []) or []
        return payload

    def to_dict(self, *, include_body: bool = False) -> Dict[str, Any]:
        return self.to_safe_dict(include_body=include_body)

    def transition_to(self, target_status: str) -> None:
        """Apply only a legal in-memory lifecycle transition."""

        target = normalize_candidate_state(target_status)
        validate_candidate_transition(self.status, target)
        self.status = target


class KnowledgeCaptureQuestion(Base):
    """At most one pending, bounded user question per candidate."""

    __tablename__ = "knowledge_capture_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_capture_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    round_number = Column(Integer, nullable=False, default=1, server_default="1")
    question_round = synonym("round_number")
    status = Column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    _question = Column("question", Text, nullable=False)
    question = _encrypted_text_property(
        "_question", "knowledge_capture_questions.question"
    )
    _title = Column("title", Text, nullable=True)
    title = _encrypted_text_property(
        "_title", "knowledge_capture_questions.title"
    )
    _message = Column("message", Text, nullable=True)
    message = _encrypted_text_property(
        "_message", "knowledge_capture_questions.message"
    )
    _options_json = Column(
        "options_json", JSON, nullable=True, default=list, server_default="[]"
    )
    options_json = _encrypted_json_property(
        "_options_json", "knowledge_capture_questions.options_json"
    )
    _answer = Column("answer", Text, nullable=True)
    answer = _encrypted_text_property(
        "_answer", "knowledge_capture_questions.answer"
    )
    _answer_source_refs = Column(
        "answer_source_refs", JSON, nullable=False, default=list, server_default="[]"
    )
    answer_source_refs = _encrypted_json_property(
        "_answer_source_refs", "knowledge_capture_questions.answer_source_refs"
    )
    evidence_digest = Column(String(64), nullable=True, index=True)
    candidate_version = Column(Integer, nullable=True, index=True)
    asked_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    answered_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    asked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    answered_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    candidate = relationship("KnowledgeCaptureCandidate", back_populates="questions")
    project = relationship("Project", foreign_keys=[project_id])
    asked_by = relationship("User", foreign_keys=[asked_by_user_id])
    answered_by = relationship("User", foreign_keys=[answered_by_user_id])

    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "round_number",
            name="uq_knowledge_capture_questions_candidate_round",
        ),
        CheckConstraint(
            "round_number BETWEEN 1 AND 2",
            name="ck_knowledge_capture_questions_round",
        ),
        CheckConstraint(
            "status IN ('pending', 'answered', 'dismissed')",
            name="ck_knowledge_capture_questions_status",
        ),
        CheckConstraint(
            "version >= 1 AND (candidate_version IS NULL OR candidate_version >= 1)",
            name="ck_knowledge_capture_questions_version",
        ),
        Index(
            "ix_knowledge_capture_questions_project_status",
            "project_id",
            "status",
        ),
        Index(
            "uq_knowledge_capture_questions_one_pending",
            "candidate_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    def to_safe_dict(self, *, include_answer: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": _id(self.id),
            "candidate_id": _id(self.candidate_id),
            "project_id": _id(self.project_id),
            "round_number": int(self.round_number or 1),
            "status": self.status,
            "question": _safe_text(self.question, limit=2000),
            "title": _safe_text(self.title, limit=240),
            "message": _safe_text(self.message, limit=2000),
            "options": _safe_json(self.options_json or []) or [],
            "evidence_digest": self.evidence_digest,
            "candidate_version": (
                int(self.candidate_version)
                if self.candidate_version is not None
                else None
            ),
            "version": int(self.version or 1),
            "asked_by_user_id": _id(self.asked_by_user_id),
            "answered_by_user_id": _id(self.answered_by_user_id),
            "asked_at": _iso(self.asked_at),
            "answered_at": _iso(self.answered_at),
            "dismissed_at": _iso(self.dismissed_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_answer:
            payload["answer"] = _safe_text(self.answer, limit=4000)
            payload["answer_source_refs"] = _safe_json(self.answer_source_refs or []) or []
        return payload

    def to_dict(self, *, include_answer: bool = True) -> Dict[str, Any]:
        return self.to_safe_dict(include_answer=include_answer)

    def transition_to(self, target_status: str) -> None:
        validate_question_transition(self.status, target_status)
        self.status = target_status


__all__ = [
    "DEFAULT_KNOWLEDGE_CAPTURE_MODE",
    "KNOWLEDGE_CAPTURE_CANDIDATE_STATES",
    "KNOWLEDGE_CAPTURE_CANDIDATE_TERMINAL_STATES",
    "KNOWLEDGE_CAPTURE_MODES",
    "KNOWLEDGE_CAPTURE_QUEUED_STATE",
    "KNOWLEDGE_CAPTURE_QUESTION_STATES",
    "KNOWLEDGE_CAPTURE_QUESTION_TERMINAL_STATES",
    "MAX_KNOWLEDGE_CAPTURE_QUESTION_ROUNDS",
    "KnowledgeCaptureCandidate",
    "KnowledgeCaptureQuestion",
    "ProjectKnowledgeCaptureSetting",
    "normalize_knowledge_capture_mode",
    "normalize_candidate_state",
    "validate_candidate_transition",
    "validate_question_transition",
]
