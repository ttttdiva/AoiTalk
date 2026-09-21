"""Durable MediaOps automation orchestration state.

Automation owns the "what/why/when" workflow in AoiTalk while Generation Studio
remains the execution system of record for presets, workflows and outputs.
Research evidence is linked to the existing MediaOps Research domain instead of
being duplicated here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


AUTOMATION_EXECUTION_MODE_VALUES = (
    "research_only",
    "draft",
    "review_before_generate",
    "auto_generate",
)
AUTOMATION_RUN_STATE_VALUES = (
    "scheduled",
    "theme_discovery",
    "research",
    "brief",
    "concept_planning",
    "prompt_planning",
    "waiting_review",
    "generation_submitting",
    "generation_running",
    "complete",
    "failed",
    "uncertain",
)
AUTOMATION_NOVELTY_POLICY_VALUES = ("conservative", "balanced", "exploratory")


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class AutomationProgram(Base):
    """Stable automation identity; all editable policy lives in revisions."""

    __tablename__ = "media_automation_programs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    create_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("length(create_hash) = 64", name="ck_media_automation_programs_create_hash"),
        Index("uq_media_automation_programs_global_idem", "owner_user_id", "idempotency_key", unique=True, postgresql_where=text("project_id IS NULL"), sqlite_where=text("project_id IS NULL")),
        Index("uq_media_automation_programs_project_idem", "project_id", "idempotency_key", unique=True, postgresql_where=text("project_id IS NOT NULL"), sqlite_where=text("project_id IS NOT NULL")),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id), "name": self.name,
            "enabled": bool(self.enabled), "create_hash": self.create_hash,
            "idempotency_key": self.idempotency_key, "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at), "updated_at": _dt(self.updated_at),
        }


class AutomationProgramRevision(Base):
    """Immutable automation recipe revision."""

    __tablename__ = "media_automation_program_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id = Column(UUID(as_uuid=True), ForeignKey("media_automation_programs.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    version = Column(Integer, nullable=False)
    execution_mode = Column(String(32), nullable=False)
    trigger_json = Column(JSON, nullable=False, default=dict)
    discovery_json = Column(JSON, nullable=False, default=dict)
    research_binding_json = Column(JSON, nullable=False, default=dict)
    planning_policy_json = Column(JSON, nullable=False, default=dict)
    generation_action_json = Column(JSON, nullable=False, default=dict)
    fallback_json = Column(JSON, nullable=False, default=dict)
    content_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("program_id", "version", name="uq_media_automation_program_revisions_version"),
        UniqueConstraint("program_id", "idempotency_key", name="uq_media_automation_program_revisions_idem"),
        CheckConstraint("version >= 1", name="ck_media_automation_program_revisions_version"),
        CheckConstraint("execution_mode IN ('research_only','draft','review_before_generate','auto_generate')", name="ck_media_automation_program_revisions_mode"),
        CheckConstraint("length(content_hash) = 64", name="ck_media_automation_program_revisions_hash"),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "program_id": _uuid(self.program_id),
            "owner_user_id": _uuid(self.owner_user_id), "project_id": _uuid(self.project_id),
            "version": int(self.version), "execution_mode": self.execution_mode,
            "trigger": self.trigger_json or {}, "discovery": self.discovery_json or {},
            "research_binding": self.research_binding_json or {},
            "planning_policy": self.planning_policy_json or {},
            "generation_action": self.generation_action_json or {},
            "fallback": self.fallback_json or {}, "content_hash": self.content_hash,
            "idempotency_key": self.idempotency_key, "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }


class AutomationRun(Base):
    """Durable state-machine owner for one automation execution."""

    __tablename__ = "media_automation_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id = Column(UUID(as_uuid=True), ForeignKey("media_automation_programs.id", ondelete="CASCADE"), nullable=False, index=True)
    program_revision_id = Column(UUID(as_uuid=True), ForeignKey("media_automation_program_revisions.id", ondelete="RESTRICT"), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    execution_key = Column(String(64), nullable=False, unique=True, index=True)
    trigger_kind = Column(String(32), nullable=False, default="manual", server_default="manual")
    state = Column(String(32), nullable=False, default="scheduled", server_default="scheduled", index=True)
    observation_json = Column(JSON, nullable=False, default=dict)
    research_run_id = Column(UUID(as_uuid=True), ForeignKey("media_research_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    research_brief_json = Column(JSON, nullable=False, default=dict)
    candidates_json = Column(JSON, nullable=False, default=list)
    selected_candidate_id = Column(String(176), nullable=True)
    novelty_snapshot_json = Column(JSON, nullable=False, default=dict)
    generation_request_json = Column(JSON, nullable=True)
    generation_request_hash = Column(String(64), nullable=True, index=True)
    external_idempotency_key = Column(String(256), nullable=True, unique=True, index=True)
    external_run_id = Column(String(176), nullable=True, index=True)
    preset_id = Column(String(176), nullable=True)
    preset_revision_id = Column(String(176), nullable=True)
    preset_revision_number = Column(Integer, nullable=True)
    preset_checksum = Column(String(64), nullable=True)
    result_deep_link = Column(String(2000), nullable=True)
    generation_result_json = Column(JSON, nullable=False, default=dict)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    correlation_id = Column(String(176), nullable=True, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("length(execution_key) = 64", name="ck_media_automation_runs_execution_key"),
        CheckConstraint("state IN ('scheduled','theme_discovery','research','brief','concept_planning','prompt_planning','waiting_review','generation_submitting','generation_running','complete','failed','uncertain')", name="ck_media_automation_runs_state"),
        CheckConstraint("generation_request_hash IS NULL OR length(generation_request_hash) = 64", name="ck_media_automation_runs_generation_hash"),
        CheckConstraint("preset_checksum IS NULL OR length(preset_checksum) = 64", name="ck_media_automation_runs_preset_checksum"),
        Index("ix_media_automation_runs_program_created", "program_id", "created_at"),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "program_id": _uuid(self.program_id),
            "program_revision_id": _uuid(self.program_revision_id),
            "owner_user_id": _uuid(self.owner_user_id), "project_id": _uuid(self.project_id),
            "execution_key": self.execution_key, "trigger_kind": self.trigger_kind,
            "state": self.state, "observation": self.observation_json or {},
            "research_run_id": _uuid(self.research_run_id), "research_brief": self.research_brief_json or {},
            "candidates": self.candidates_json or [], "selected_candidate_id": self.selected_candidate_id,
            "novelty_snapshot": self.novelty_snapshot_json or {},
            "generation_request": self.generation_request_json,
            "generation_request_hash": self.generation_request_hash,
            "external_idempotency_key": self.external_idempotency_key,
            "external_run_id": self.external_run_id, "preset_id": self.preset_id,
            "preset_revision_id": self.preset_revision_id,
            "preset_revision_number": self.preset_revision_number,
            "preset_checksum": self.preset_checksum, "result_deep_link": self.result_deep_link,
            "generation_result": self.generation_result_json or {},
            "error_code": self.error_code, "error_message": self.error_message,
            "correlation_id": self.correlation_id, "started_at": _dt(self.started_at),
            "completed_at": _dt(self.completed_at), "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at), "updated_at": _dt(self.updated_at),
        }
