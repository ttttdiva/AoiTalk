"""MediaOps WS3 persistence: research evidence ledger and editorial trace."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
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

from .base import Base


class ResearchFindingKind(str, Enum):
    FACT = "fact"
    SIGNAL = "signal"
    HYPOTHESIS = "hypothesis"


RESEARCH_FINDING_KIND_VALUES = tuple(
    value.value for value in ResearchFindingKind
)


_PLATFORM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("x", "platform_x"),
    ("pixiv", "platform_pixiv"),
    ("dlsite", "platform_dlsite"),
    ("patreon", "platform_patreon"),
    ("youtube", "platform_youtube"),
    ("instagram", "platform_instagram"),
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ResearchRoutine(Base):
    """Stable identity for a repeatable research definition."""

    __tablename__ = "media_research_routines"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # A routine is a durable operational definition.  ``state``/``enabled``
    # are mutable lifecycle metadata; the definition itself remains in the
    # append-only revision table below.
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"), index=True)
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_due_at = Column(DateTime, nullable=True, index=True)
    next_due_at = Column(DateTime, nullable=True, index=True)
    create_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_research_routines_create_hash",
        ),
        CheckConstraint(
            "state IN ('draft', 'active', 'paused', 'archived')",
            name="ck_media_research_routines_state",
        ),
        Index(
            "ix_media_research_routines_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_research_routines_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_research_routines_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_id": _uuid(self.persona_id),
            "state": self.state,
            "enabled": bool(self.enabled),
            "platform_account_id": _uuid(self.platform_account_id),
            "last_due_at": _dt(self.last_due_at),
            "next_due_at": _dt(self.next_due_at),
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ResearchRoutineRevision(Base):
    """Immutable version of a ResearchRoutine definition."""

    __tablename__ = "media_research_routine_revisions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    research_routine_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_routines.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    version = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    objective = Column(Text, nullable=False)
    questions_json = Column(
        "questions",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )

    cadence = Column(String(32), nullable=False, default="manual", server_default="manual")
    timezone = Column(String(64), nullable=False, default="UTC", server_default="UTC")
    schedule_json = Column("schedule", JSON, nullable=False, default=dict, server_default="{}")
    source_types_json = Column("source_types", JSON, nullable=False, default=list, server_default="[]")
    search_queries_json = Column("search_queries", JSON, nullable=False, default=list, server_default="[]")
    domains_json = Column("domains", JSON, nullable=False, default=list, server_default="[]")
    follow_accounts_json = Column("follow_accounts", JSON, nullable=False, default=list, server_default="[]")
    follow_tags_json = Column("follow_tags", JSON, nullable=False, default=list, server_default="[]")
    exclusions_json = Column("exclusions", JSON, nullable=False, default=list, server_default="[]")
    freshness_hours = Column(Integer, nullable=False, default=168, server_default="168")
    max_candidates = Column(Integer, nullable=False, default=20, server_default="20")
    review_policy = Column(String(64), nullable=False, default="human_review", server_default="human_review")

    platform_x = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    platform_pixiv = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    platform_dlsite = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    platform_patreon = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    platform_youtube = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    platform_instagram = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    content_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=True,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "version > 0",
            name="ck_media_research_routine_revisions_version",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_research_routine_revisions_hash",
        ),
        CheckConstraint(
            "freshness_hours >= 1 AND freshness_hours <= 8760",
            name="ck_media_research_routine_revisions_freshness",
        ),
        CheckConstraint(
            "max_candidates >= 1 AND max_candidates <= 500",
            name="ck_media_research_routine_revisions_max_candidates",
        ),
        UniqueConstraint(
            "research_routine_id",
            "version",
            name="uq_media_research_routine_revisions_version",
        ),
        UniqueConstraint(
            "research_routine_id",
            "idempotency_key",
            name="uq_media_research_routine_revisions_idempotency",
        ),
        Index(
            "ix_media_research_routine_revisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        platforms = [
            platform
            for platform, attribute in _PLATFORM_COLUMNS
            if bool(getattr(self, attribute, False))
        ]
        questions = (
            self.questions_json
            if isinstance(self.questions_json, list)
            else []
        )

        return {
            "id": _uuid(self.id),
            "research_routine_id": _uuid(self.research_routine_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version),
            "name": self.name,
            "objective": self.objective,
            "questions": [
                str(value)
                for value in questions
                if isinstance(value, str)
            ],
            "target_platforms": platforms,
            "cadence": self.cadence,
            "timezone": self.timezone,
            "schedule": self.schedule_json or {},
            "source_types": list(self.source_types_json or []),
            "search_queries": list(self.search_queries_json or []),
            "domains": list(self.domains_json or []),
            "follow_accounts": list(self.follow_accounts_json or []),
            "follow_tags": list(self.follow_tags_json or []),
            "exclusions": list(self.exclusions_json or []),
            "freshness_hours": int(self.freshness_hours or 168),
            "max_candidates": int(self.max_candidates or 20),
            "review_policy": self.review_policy,
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ResearchRun(Base):
    """Immutable snapshot of one manual research occurrence."""

    __tablename__ = "media_research_runs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    research_routine_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_routines.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    research_routine_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_routine_revisions.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    routine_content_hash = Column(
        String(64),
        nullable=False,
    )
    focus_note = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="recorded", server_default="recorded", index=True)
    started_at = Column(DateTime, nullable=True, index=True)
    finished_at = Column(DateTime, nullable=True, index=True)
    source_refs_json = Column("source_refs", JSON, nullable=False, default=list, server_default="[]")
    omissions_json = Column("omissions", JSON, nullable=False, default=list, server_default="[]")
    run_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(routine_content_hash) = 64",
            name="ck_media_research_runs_routine_hash",
        ),
        CheckConstraint(
            "length(run_hash) = 64",
            name="ck_media_research_runs_hash",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'recorded', 'partial', 'succeeded', 'failed')",
            name="ck_media_research_runs_status",
        ),
        UniqueConstraint(
            "research_routine_id",
            "idempotency_key",
            name="uq_media_research_runs_idempotency",
        ),
        Index(
            "ix_media_research_runs_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "research_routine_id": _uuid(self.research_routine_id),
            "research_routine_revision_id": _uuid(
                self.research_routine_revision_id
            ),
            "routine_content_hash": self.routine_content_hash,
            "focus_note": self.focus_note,
            "status": self.status,
            "started_at": _dt(self.started_at),
            "finished_at": _dt(self.finished_at),
            "source_refs": list(self.source_refs_json or []),
            "omissions": list(self.omissions_json or []),
            "run_hash": self.run_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ResearchFinding(Base):
    """Immutable research statement with mandatory typed evidence."""

    __tablename__ = "media_research_findings"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    research_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_runs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    kind = Column(
        String(16),
        nullable=False,
        index=True,
    )
    statement = Column(Text, nullable=False)
    finding_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'signal', 'hypothesis')",
            name="ck_media_research_findings_kind",
        ),
        CheckConstraint(
            "length(finding_hash) = 64",
            name="ck_media_research_findings_hash",
        ),
        UniqueConstraint(
            "research_run_id",
            "idempotency_key",
            name="uq_media_research_findings_idempotency",
        ),
        UniqueConstraint(
            "research_run_id",
            "finding_hash",
            name="uq_media_research_findings_hash",
        ),
        Index(
            "ix_media_research_findings_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "research_run_id": _uuid(self.research_run_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "kind": self.kind,
            "statement": self.statement,
            "finding_hash": self.finding_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ResearchFindingEvidence(Base):
    """Immutable typed evidence attached atomically to a Finding."""

    __tablename__ = "media_research_finding_evidence"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    finding_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_findings.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    ordinal = Column(Integer, nullable=False)
    evidence_type = Column(
        String(16),
        nullable=False,
    )
    label = Column(String(255), nullable=True)
    source_url = Column(Text, nullable=True)
    artifact_sha256 = Column(
        String(64),
        nullable=True,
    )
    artifact_mime_type = Column(
        String(255),
        nullable=True,
    )
    note = Column(Text, nullable=True)
    evidence_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1 AND ordinal <= 20",
            name="ck_media_research_finding_evidence_ordinal",
        ),
        CheckConstraint(
            "evidence_type IN ('url', 'artifact')",
            name="ck_media_research_finding_evidence_type",
        ),
        CheckConstraint(
            "("
            "evidence_type = 'url' "
            "AND source_url IS NOT NULL "
            "AND artifact_sha256 IS NULL "
            "AND artifact_mime_type IS NULL"
            ") OR ("
            "evidence_type = 'artifact' "
            "AND source_url IS NULL "
            "AND artifact_sha256 IS NOT NULL "
            "AND artifact_mime_type IS NOT NULL"
            ")",
            name="ck_media_research_finding_evidence_shape",
        ),
        CheckConstraint(
            "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
            name="ck_media_research_finding_evidence_artifact_hash",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_media_research_finding_evidence_hash",
        ),
        UniqueConstraint(
            "finding_id",
            "ordinal",
            name="uq_media_research_finding_evidence_ordinal",
        ),
        UniqueConstraint(
            "finding_id",
            "evidence_hash",
            name="uq_media_research_finding_evidence_hash",
        ),
        Index(
            "ix_media_research_finding_evidence_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        if self.evidence_type == "url":
            provenance: dict[str, Any] = {
                "type": "url",
                "url": self.source_url,
            }
        else:
            provenance = {
                "type": "artifact",
                "sha256": self.artifact_sha256,
                "mime_type": self.artifact_mime_type,
            }

        return {
            "id": _uuid(self.id),
            "finding_id": _uuid(self.finding_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "ordinal": int(self.ordinal),
            "label": self.label,
            "note": self.note,
            "provenance": provenance,
            "evidence_hash": self.evidence_hash,
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class EditorialProgram(Base):
    """Stable editorial identity tied to one stable Persona identity."""

    __tablename__ = "media_editorial_programs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"), index=True)
    last_due_at = Column(DateTime, nullable=True, index=True)
    next_due_at = Column(DateTime, nullable=True, index=True)
    create_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_editorial_programs_create_hash",
        ),
        CheckConstraint(
            "state IN ('draft', 'active', 'paused', 'archived')",
            name="ck_media_editorial_programs_state",
        ),
        Index(
            "ix_media_editorial_programs_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_editorial_programs_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_editorial_programs_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_id": _uuid(self.persona_id),
            "state": self.state,
            "enabled": bool(self.enabled),
            "last_due_at": _dt(self.last_due_at),
            "next_due_at": _dt(self.next_due_at),
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class EditorialProgramRevision(Base):
    """Immutable EditorialProgram definition revision."""

    __tablename__ = "media_editorial_program_revisions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    editorial_program_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_editorial_programs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    version = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    objective = Column(Text, nullable=False)
    content_type = Column(String(64), nullable=False, default="article", server_default="article")
    cadence = Column(String(32), nullable=False, default="manual", server_default="manual")
    target_platforms_json = Column("target_platforms", JSON, nullable=False, default=list, server_default="[]")
    target_account_refs_json = Column("target_account_refs", JSON, nullable=False, default=list, server_default="[]")
    content_pillar = Column(String(200), nullable=True)
    required_resources_json = Column("required_resources", JSON, nullable=False, default=list, server_default="[]")
    default_creative_recipe_ref = Column(String(164), nullable=True)
    default_qa_policy_ref = Column(String(164), nullable=True)
    experiment_ref = Column(String(164), nullable=True)
    draft_generation_policy = Column(String(64), nullable=False, default="human_review", server_default="human_review")
    content_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=True,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "version > 0",
            name="ck_media_editorial_program_revisions_version",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_editorial_program_revisions_hash",
        ),
        UniqueConstraint(
            "editorial_program_id",
            "version",
            name="uq_media_editorial_program_revisions_version",
        ),
        UniqueConstraint(
            "editorial_program_id",
            "idempotency_key",
            name="uq_media_editorial_program_revisions_idempotency",
        ),
        Index(
            "ix_media_editorial_program_revisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "editorial_program_id": _uuid(self.editorial_program_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version),
            "name": self.name,
            "objective": self.objective,
            "content_type": self.content_type,
            "cadence": self.cadence,
            "target_platforms": list(self.target_platforms_json or []),
            "target_account_refs": list(self.target_account_refs_json or []),
            "content_pillar": self.content_pillar,
            "required_resources": list(self.required_resources_json or []),
            "default_creative_recipe_ref": self.default_creative_recipe_ref,
            "default_qa_policy_ref": self.default_qa_policy_ref,
            "experiment_ref": self.experiment_ref,
            "draft_generation_policy": self.draft_generation_policy,
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ContentItem(Base):
    """Immutable editorial planning item; publication is outside WS3."""

    __tablename__ = "media_content_items"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    editorial_program_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_editorial_programs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = Column(String(500), nullable=False)
    brief = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    objective = Column(Text, nullable=True)
    content_type = Column(String(64), nullable=False, default="article", server_default="article")
    content_pillar = Column(String(200), nullable=True)
    intended_audience = Column(Text, nullable=True)
    source_refs_json = Column("source_refs", JSON, nullable=False, default=list, server_default="[]")
    candidate_refs_json = Column("candidate_refs", JSON, nullable=False, default=list, server_default="[]")
    desired_assets_json = Column("desired_assets", JSON, nullable=False, default=list, server_default="[]")
    monetization_ref = Column(String(164), nullable=True)
    experiment_ref = Column(String(164), nullable=True)
    scheduled_at = Column(DateTime, nullable=True, index=True)
    content_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_content_items_hash",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_media_content_items_version",
        ),
        CheckConstraint(
            "status IN ('draft', 'ready', 'in_progress', 'published', 'archived')",
            name="ck_media_content_items_status",
        ),
        UniqueConstraint(
            "editorial_program_id",
            "idempotency_key",
            name="uq_media_content_items_idempotency",
        ),
        Index(
            "ix_media_content_items_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "editorial_program_id": _uuid(self.editorial_program_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "title": self.title,
            "brief": self.brief,
            "version": int(self.version or 1),
            "status": self.status,
            "persona_revision_id": _uuid(self.persona_revision_id),
            "objective": self.objective,
            "content_type": self.content_type,
            "content_pillar": self.content_pillar,
            "intended_audience": self.intended_audience,
            "source_refs": list(self.source_refs_json or []),
            "candidate_refs": list(self.candidate_refs_json or []),
            "desired_assets": list(self.desired_assets_json or []),
            "monetization_ref": self.monetization_ref,
            "experiment_ref": self.experiment_ref,
            "scheduled_at": _dt(self.scheduled_at),
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ResearchCandidate(Base):
    """Bounded idea candidate discovered by a research run.

    Candidates are not publication instructions.  They progress through a
    human-review lifecycle and carry only source/evidence references; the
    optional ContentItem link is written only by the promotion path.
    """

    __tablename__ = "media_research_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    research_routine_id = Column(UUID(as_uuid=True), ForeignKey("media_research_routines.id", ondelete="CASCADE"), nullable=False, index=True)
    research_run_id = Column(UUID(as_uuid=True), ForeignKey("media_research_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    routine_revision_id = Column(UUID(as_uuid=True), ForeignKey("media_research_routine_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_key = Column(String(64), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=False)
    source_url = Column(Text, nullable=True)
    source_published_at = Column(DateTime, nullable=True)
    discovered_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    relevance_score = Column(Float, nullable=True)
    freshness_score = Column(Float, nullable=True)
    evidence_json = Column("evidence", JSON, nullable=False, default=list, server_default="[]")
    reason = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="discovered", server_default="discovered", index=True)
    # Monotonic append-only decision projection version.  Legacy rows start at
    # zero and require a fresh, explicit human decision before promotion.
    decision_version = Column(Integer, nullable=False, default=0, server_default="0", index=True)
    content_item_id = Column(UUID(as_uuid=True), ForeignKey("media_content_items.id", ondelete="SET NULL"), nullable=True, index=True)
    candidate_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')",
            name="ck_media_research_candidates_status",
        ),
        CheckConstraint("candidate_key <> ''", name="ck_media_research_candidates_key"),
        CheckConstraint("length(candidate_hash) = 64", name="ck_media_research_candidates_hash"),
        CheckConstraint("relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 1)", name="ck_media_research_candidates_relevance"),
        CheckConstraint("freshness_score IS NULL OR (freshness_score >= 0 AND freshness_score <= 1)", name="ck_media_research_candidates_freshness"),
        CheckConstraint("decision_version >= 0", name="ck_media_research_candidates_decision_version"),
        UniqueConstraint("research_routine_id", "candidate_key", name="uq_media_research_candidates_identity"),
        UniqueConstraint("research_run_id", "idempotency_key", name="uq_media_research_candidates_idempotency"),
        Index("ix_media_research_candidates_owner_project", "owner_user_id", "project_id"),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "research_routine_id": _uuid(self.research_routine_id),
            "research_run_id": _uuid(self.research_run_id),
            "routine_revision_id": _uuid(self.routine_revision_id),
            "candidate_key": self.candidate_key,
            "title": self.title,
            "summary": self.summary,
            "source_url": self.source_url,
            "source_published_at": _dt(self.source_published_at),
            "discovered_at": _dt(self.discovered_at),
            "expires_at": _dt(self.expires_at),
            "relevance_score": self.relevance_score,
            "freshness_score": self.freshness_score,
            "evidence": list(self.evidence_json or []),
            "reason": self.reason,
            "status": self.status,
            "decision_version": int(self.decision_version or 0),
            "review_state": (
                "pending"
                if self.status in {"discovered", "triaged"}
                else "accepted"
                if self.status in {"accepted", "promoted"}
                else "rejected"
                if self.status == "rejected"
                else "expired"
            ),
            "content_item_id": _uuid(self.content_item_id),
            "candidate_hash": self.candidate_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


RESEARCH_CANDIDATE_STATUS_VALUES = (
    "discovered",
    "triaged",
    "accepted",
    "rejected",
    "expired",
    "promoted",
)

RESEARCH_CANDIDATE_DECISION_EVENT_VALUES = (
    "triage",
    "accept",
    "reject",
    "expire",
    "promote",
)

RESEARCH_CANDIDATE_DECISION_ACTOR_TYPES = (
    "human",
    "agent",
    "system",
    "admin",
    "unknown",
)


class ResearchCandidateDecision(Base):
    """Immutable, append-only status transition for a research candidate.

    ``ResearchCandidate`` remains the current-state projection.  This ledger
    stores the evidence required to reconstruct every review/promotion
    decision without trusting mutable candidate fields: the candidate hash and
    bounded safe snapshot are captured at append time, while request/event
    hashes and sequence links provide idempotent and tamper-evident history.
    The snapshot is intentionally not exposed by :meth:`to_safe_dict`.
    """

    __tablename__ = "media_research_candidate_decisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_research_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    sequence = Column(Integer, nullable=False, default=1, server_default="1")
    event_type = Column(String(16), nullable=False)
    from_status = Column(String(16), nullable=True)
    to_status = Column(String(16), nullable=False)
    reason = Column(Text, nullable=True)
    # ``candidate_snapshot`` is a bounded, already-sanitized JSON envelope;
    # never put provider payloads, credentials, or raw source bodies here.
    candidate_snapshot_json = Column(
        "candidate_snapshot",
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    candidate_hash = Column(String(64), nullable=False, index=True)
    actor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_type = Column(
        String(16),
        nullable=False,
        default="system",
        server_default="system",
    )
    idempotency_key = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False)
    decision_hash = Column(String(64), nullable=False, index=True)
    prev_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False, index=True)
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('triage', 'accept', 'reject', 'expire', 'promote')",
            name="ck_media_research_candidate_decisions_event_type",
        ),
        CheckConstraint(
            "from_status IS NULL OR from_status IN ('discovered', 'triaged', 'accepted')",
            name="ck_media_research_candidate_decisions_from_status",
        ),
        CheckConstraint(
            "to_status IN ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')",
            name="ck_media_research_candidate_decisions_to_status",
        ),
        CheckConstraint(
            "actor_type IN ('human', 'agent', 'system', 'admin', 'unknown')",
            name="ck_media_research_candidate_decisions_actor_type",
        ),
        CheckConstraint(
            "event_type NOT IN ('accept', 'reject') OR (length(trim(coalesce(reason, ''))) > 0 AND actor_type IN ('human', 'admin'))",
            name="ck_media_research_candidate_decisions_reason",
        ),
        CheckConstraint(
            "event_type <> 'promote' OR content_item_id IS NOT NULL",
            name="ck_media_research_candidate_decisions_promotion_content",
        ),
        CheckConstraint(
            "(event_type = 'triage' AND to_status = 'triaged') OR "
            "(event_type = 'accept' AND to_status = 'accepted') OR "
            "(event_type = 'reject' AND to_status = 'rejected') OR "
            "(event_type = 'expire' AND to_status = 'expired') OR "
            "(event_type = 'promote' AND to_status = 'promoted')",
            name="ck_media_research_candidate_decisions_event_target",
        ),
        CheckConstraint(
            "sequence > 0",
            name="ck_media_research_candidate_decisions_sequence",
        ),
        CheckConstraint(
            "length(candidate_hash) = 64",
            name="ck_media_research_candidate_decisions_candidate_hash",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_media_research_candidate_decisions_request_hash",
        ),
        CheckConstraint(
            "length(decision_hash) = 64",
            name="ck_media_research_candidate_decisions_decision_hash",
        ),
        CheckConstraint(
            "prev_event_hash IS NULL OR length(prev_event_hash) = 64",
            name="ck_media_research_candidate_decisions_prev_event_hash",
        ),
        CheckConstraint(
            "length(event_hash) = 64",
            name="ck_media_research_candidate_decisions_event_hash",
        ),
        UniqueConstraint(
            "candidate_id",
            "sequence",
            name="uq_media_research_candidate_decisions_sequence",
        ),
        UniqueConstraint(
            "candidate_id",
            "idempotency_key",
            name="uq_media_research_candidate_decisions_idempotency",
        ),
        Index(
            "ix_media_research_candidate_decisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "ix_media_research_candidate_decisions_candidate_created",
            "candidate_id",
            "created_at",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        """Return a bounded hash-only projection; omit the candidate snapshot."""

        return {
            "id": _uuid(self.id),
            "candidate_id": _uuid(self.candidate_id),
            "sequence": int(self.sequence or 0),
            "event_type": self.event_type,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "candidate_hash": self.candidate_hash,
            "candidate_snapshot_hash": self.candidate_hash,
            "request_hash": self.request_hash,
            "actor_id": _uuid(self.actor_id),
            "actor_type": self.actor_type,
            "content_item_id": _uuid(self.content_item_id),
            "decision_hash": self.decision_hash,
            "prev_decision_hash": self.prev_event_hash,
            "prev_event_hash": self.prev_event_hash,
            "event_hash": self.event_hash,
            "decided_at": _dt(self.created_at),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ContentItemFinding(Base):
    """Trace edge from ContentItem back to immutable Findings."""

    __tablename__ = "media_content_item_findings"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_content_items.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    finding_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_research_findings.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    ordinal = Column(Integer, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1 AND ordinal <= 20",
            name="ck_media_content_item_findings_ordinal",
        ),
        UniqueConstraint(
            "content_item_id",
            "finding_id",
            name="uq_media_content_item_findings_finding",
        ),
        UniqueConstraint(
            "content_item_id",
            "ordinal",
            name="uq_media_content_item_findings_ordinal",
        ),
        Index(
            "ix_media_content_item_findings_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "content_item_id": _uuid(self.content_item_id),
            "finding_id": _uuid(self.finding_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "ordinal": int(self.ordinal),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict
