"""Durable generic Agent identity and assignment models.

The tables in this module are deliberately additive.  A ``User`` remains an
authenticated human account, while ``Agent`` is a durable worker identity
that can be attached to a presentation ``Character`` and explicitly granted
access to existing Spaces, Projects, Tasks, and Media Personas.

No prompt, model, tool list, lease, scheduler, or provider credential is kept
on the stable Agent row.  Mutable worker definition lives in immutable
``AgentRevision`` rows and authorization is resolved by the service layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
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
from sqlalchemy.orm import relationship

from .base import Base


AGENT_STATES = ("draft", "active", "paused", "retired")
AGENT_ASSIGNMENT_STATES = ("active", "revoked", "expired")
AGENT_SPACE_ASSIGNMENT_KINDS = ("primary", "secondary", "supporting")
AGENT_PROJECT_ROLES = ("owner", "admin", "member", "viewer")
AGENT_TASK_ASSIGNMENT_ROLES = ("owner", "executor", "reviewer", "observer")
PERSONA_OPERATOR_ROLES = (
    "operator",
    "strategist",
    "researcher",
    "creator",
    "analyst",
    "publisher",
)
AUTONOMY_LEVELS = ("disabled", "supervised", "bounded", "autonomous")
EMPLOYMENT_STATES = (
    "active",
    "on_leave",
    "suspended",
    "terminated",
    "contractor",
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json(value: Any, default: Any) -> Any:
    return value if value is not None else default


_SECRET_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "cookie",
    "authorization",
    "provider",
    "model",
    "filesystem",
    "path",
)


def _safe_json(value: Any, default: Any) -> Any:
    """Bounded projection for JSON policy fields used in safe DTOs."""

    if value is None:
        return default
    if isinstance(value, dict):
        if len(value) > 64:
            return {}
        return {
            str(key): _safe_json(item, None)
            for key, item in value.items()
            if not any(marker in str(key).casefold() for marker in _SECRET_MARKERS)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, None) for item in list(value)[:128]]
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            lowered = value.casefold()
            if any(
                marker in lowered
                for marker in (
                    "bearer ",
                    "api_key",
                    "apikey",
                    "password=",
                    "token=",
                    "secret=",
                    "credential=",
                    "authorization:",
                    "cookie=",
                )
            ):
                return "[REDACTED]"
            if ("/" in value or "\\" in value) and len(value) > 160:
                return "[REDACTED]"
        return value
    return None


def _safe_instruction(value: Any, *, limit: int = 1200) -> str:
    """Bound revision prose before exposing it through operator APIs.

    Agent instructions are executable context, not an authority grant.  Keep
    a short diagnostic preview for administrators while removing obvious
    credential/path-shaped fragments and never returning an unbounded prompt.
    """

    text = str(value or "")
    if not text:
        return ""
    lowered = text.casefold()
    if any(marker in lowered for marker in ("api_key", "apikey", "password", "secret", "token", "credential", "authorization:")):
        return "[REDACTED]"
    if "/" in text or "\\" in text:
        # Paths and command snippets are not needed in a safe identity DTO.
        return "[REDACTED]"
    return text[:limit]


class Organization(Base):
    """Deployment-wide singleton organization/settings row.

    AoiTalk intentionally has one organization per installation.  The fixed
    ``singleton_key`` is protected both by a unique index and a database
    check, so a future caller cannot accidentally turn this table into a
    tenant selector.
    """

    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    singleton_key = Column(
        String(32), nullable=False, default="installation", server_default="installation"
    )
    display_name = Column(String(200), nullable=False, default="AoiTalk", server_default="AoiTalk")
    legal_name = Column(String(200), nullable=True)
    locale = Column(String(32), nullable=False, default="ja-JP", server_default="ja-JP")
    timezone = Column(String(64), nullable=False, default="Asia/Tokyo", server_default="Asia/Tokyo")
    policy_version = Column(Integer, nullable=False, default=1, server_default="1")
    autonomy_level = Column(
        String(16), nullable=False, default="disabled", server_default="disabled", index=True
    )
    # These are bounded, versioned policy projections.  They are never
    # consumed as authority without service-layer schema validation.
    policy_json = Column("policy", JSON, nullable=False, default=dict, server_default="{}")
    budget_policy_json = Column(
        "budget_policy", JSON, nullable=False, default=dict, server_default="{}"
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("singleton_key", name="uq_organizations_singleton_key"),
        CheckConstraint(
            "singleton_key = 'installation'", name="ck_organizations_singleton_key"
        ),
        CheckConstraint("policy_version > 0", name="ck_organizations_policy_version_positive"),
        CheckConstraint(
            "autonomy_level IN ('disabled','supervised','bounded','autonomous')",
            name="ck_organizations_autonomy_level",
        ),
        CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="ck_organizations_display_name"),
    )

    @property
    def policy(self) -> Any:
        return self.policy_json

    @policy.setter
    def policy(self, value: Any) -> None:
        self.policy_json = value

    @property
    def budget_policy(self) -> Any:
        return self.budget_policy_json

    @budget_policy.setter
    def budget_policy(self, value: Any) -> None:
        self.budget_policy_json = value

    @property
    def settings(self) -> Any:
        """Compatibility alias for bounded organization policy settings."""

        return self.policy_json

    @settings.setter
    def settings(self, value: Any) -> None:
        self.policy_json = value

    @property
    def company_name(self) -> str | None:
        return self.legal_name or self.display_name

    @company_name.setter
    def company_name(self, value: str | None) -> None:
        self.legal_name = value

    @property
    def name(self) -> str:
        return self.display_name

    @name.setter
    def name(self, value: str) -> None:
        self.display_name = value

    @property
    def settings_json(self) -> Any:
        return self.policy_json

    @settings_json.setter
    def settings_json(self, value: Any) -> None:
        self.policy_json = value

    @property
    def organization_policy(self) -> Any:
        return self.policy_json

    @organization_policy.setter
    def organization_policy(self, value: Any) -> None:
        self.policy_json = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "singleton_key": self.singleton_key,
            "display_name": self.display_name,
            "legal_name": self.legal_name,
            "locale": self.locale,
            "timezone": self.timezone,
            "policy_version": int(self.policy_version or 1),
            "autonomy_level": self.autonomy_level,
            "policy": _safe_json(self.policy_json, {}),
            "budget_policy": _safe_json(self.budget_policy_json, {}),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class Agent(Base):
    """Stable generic autonomous-worker identity."""

    __tablename__ = "agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ``display_name`` is a discovery label only; executable instructions and
    # capabilities are pinned by AgentRevision.
    display_name = Column(String(160), nullable=False)
    slug = Column(String(100), nullable=True, unique=True, index=True)
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    character_id = Column(
        UUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    create_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    revisions = relationship(
        "AgentRevision", back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    organization_profile = relationship(
        "AgentOrganizationProfile", back_populates="agent", uselist=False,
        cascade="all, delete-orphan", passive_deletes=True,
        foreign_keys="AgentOrganizationProfile.agent_id",
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','active','paused','retired')", name="ck_agents_state"
        ),
        CheckConstraint("length(create_hash) = 64", name="ck_agents_create_hash_length"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 160", name="ck_agents_display_name"),
    )

    @property
    def created_by_user_id(self):
        return self.created_by

    @created_by_user_id.setter
    def created_by_user_id(self, value):
        self.created_by = value

    @property
    def name(self) -> str:
        return self.display_name

    @name.setter
    def name(self, value: str) -> None:
        self.display_name = value

    @property
    def lifecycle_state(self) -> str:
        return self.state

    @lifecycle_state.setter
    def lifecycle_state(self, value: str) -> None:
        self.state = value

    @property
    def create_content_hash(self) -> str:
        return self.create_hash

    @create_content_hash.setter
    def create_content_hash(self, value: str) -> None:
        self.create_hash = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "display_name": self.display_name,
            "slug": self.slug,
            "state": self.state,
            "character_id": _uuid(self.character_id),
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_by_user_id": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentRevision(Base):
    """Immutable worker definition pinned by future AgentRuns."""

    __tablename__ = "agent_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    display_name = Column(String(160), nullable=False)
    mission = Column(Text, nullable=False, default="", server_default="")
    responsibility_summary = Column(Text, nullable=False, default="", server_default="")
    operational_instructions = Column(Text, nullable=False, default="", server_default="")
    agent_team_id = Column(String(100), nullable=False)
    execution_profile_id = Column(String(100), nullable=False)
    allowed_subagent_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    capability_ceiling_json = Column("capability_ceiling", JSON, nullable=False, default=list, server_default="[]")
    wake_policy_json = Column("wake_policy", JSON, nullable=False, default=dict, server_default="{}")
    budget_policy_json = Column("budget_policy", JSON, nullable=False, default=dict, server_default="{}")
    concurrency_policy_json = Column("concurrency_policy", JSON, nullable=False, default=dict, server_default="{}")
    content_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True)

    agent = relationship("Agent", back_populates="revisions")

    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_revisions_agent_version"),
        UniqueConstraint("agent_id", "idempotency_key", name="uq_agent_revisions_agent_idempotency"),
        CheckConstraint("version > 0", name="ck_agent_revisions_version_positive"),
        CheckConstraint("length(content_hash) = 64", name="ck_agent_revisions_content_hash_length"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 160", name="ck_agent_revisions_display_name"),
    )

    @property
    def team_id(self) -> str:
        return self.agent_team_id

    @team_id.setter
    def team_id(self, value: str) -> None:
        self.agent_team_id = value

    @property
    def execution_profile(self) -> str:
        return self.execution_profile_id

    @execution_profile.setter
    def execution_profile(self, value: str) -> None:
        self.execution_profile_id = value

    @property
    def capability_ceiling(self) -> Any:
        return self.capability_ceiling_json

    @capability_ceiling.setter
    def capability_ceiling(self, value: Any) -> None:
        self.capability_ceiling_json = value

    @property
    def instructions(self) -> str:
        return self.operational_instructions

    @instructions.setter
    def instructions(self, value: str) -> None:
        self.operational_instructions = value

    @property
    def allowed_subagents(self) -> Any:
        return self.allowed_subagent_ids

    @allowed_subagents.setter
    def allowed_subagents(self, value: Any) -> None:
        self.allowed_subagent_ids = value

    @property
    def revision_idempotency_key(self) -> str:
        return self.idempotency_key

    @revision_idempotency_key.setter
    def revision_idempotency_key(self, value: str) -> None:
        self.idempotency_key = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "agent_id": _uuid(self.agent_id),
            "version": int(self.version or 1),
            "display_name": self.display_name,
            "mission": _safe_instruction(self.mission),
            "responsibility_summary": _safe_instruction(self.responsibility_summary),
            "operational_instructions": _safe_instruction(self.operational_instructions),
            "agent_team_id": self.agent_team_id,
            "team_id": self.agent_team_id,
            "execution_profile_id": self.execution_profile_id,
            "allowed_subagent_ids": list(_json(self.allowed_subagent_ids, [])),
            "capability_ceiling": list(_safe_json(self.capability_ceiling_json, []) or []),
            "wake_policy": _safe_json(self.wake_policy_json, {}),
            "budget_policy": _safe_json(self.budget_policy_json, {}),
            "concurrency_policy": _safe_json(self.concurrency_policy_json, {}),
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_by_user_id": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentOrganizationProfile(Base):
    """Optional employment/company extension for a generic Agent."""

    __tablename__ = "agent_organization_profiles"

    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    job_title = Column(String(160), nullable=False, default="")
    responsibility_summary = Column(Text, nullable=False, default="")
    primary_space_id = Column(UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True, index=True)
    manager_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    manager_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    autonomy_level = Column(String(16), nullable=False, default="supervised", server_default="supervised")
    company_permission_ceiling_json = Column("company_permission_ceiling", JSON, nullable=False, default=dict, server_default="{}")
    employment_state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    agent = relationship("Agent", back_populates="organization_profile", foreign_keys=[agent_id])

    __table_args__ = (
        CheckConstraint(
            "NOT (manager_user_id IS NOT NULL AND manager_agent_id IS NOT NULL)",
            name="ck_agent_org_profiles_manager_xor",
        ),
        CheckConstraint(
            "autonomy_level IN ('disabled','supervised','bounded','autonomous')",
            name="ck_agent_org_profiles_autonomy_level",
        ),
        CheckConstraint(
            "employment_state IN ('active','on_leave','suspended','terminated','contractor')",
            name="ck_agent_org_profiles_employment_state",
        ),
    )

    @property
    def company_permission_ceiling(self) -> Any:
        return self.company_permission_ceiling_json

    @company_permission_ceiling.setter
    def company_permission_ceiling(self, value: Any) -> None:
        self.company_permission_ceiling_json = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": _uuid(self.agent_id),
            "job_title": self.job_title,
            "responsibility_summary": self.responsibility_summary,
            "primary_space_id": _uuid(self.primary_space_id),
            "manager_user_id": _uuid(self.manager_user_id),
            "manager_agent_id": _uuid(self.manager_agent_id),
            "autonomy_level": self.autonomy_level,
            "company_permission_ceiling": _safe_json(self.company_permission_ceiling_json, {}),
            "employment_state": self.employment_state,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentSpaceAssignment(Base):
    """Explicit Agent membership in an existing Space."""

    __tablename__ = "agent_space_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    space_id = Column(UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True)
    assignment_kind = Column(String(16), nullable=False, default="supporting", server_default="supporting")
    role_label = Column(String(120), nullable=True)
    policy_ceiling_json = Column("policy_ceiling", JSON, nullable=False, default=dict, server_default="{}")
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        CheckConstraint(
            "assignment_kind IN ('primary','secondary','supporting')",
            name="ck_agent_space_assignments_kind",
        ),
        CheckConstraint(
            "state IN ('active','revoked','expired')", name="ck_agent_space_assignments_state"
        ),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_space_assignments_dates",
        ),
        Index(
            "uq_agent_space_assignments_active_equivalent",
            "agent_id", "space_id", "assignment_kind", unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_agent_space_assignments_space_state", "space_id", "state"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "agent_id": _uuid(self.agent_id), "space_id": _uuid(self.space_id),
            "assignment_kind": self.assignment_kind, "role_label": self.role_label,
            "policy_ceiling": _safe_json(self.policy_ceiling_json, {}), "state": self.state,
            "active_from": _dt(self.active_from), "active_until": _dt(self.active_until),
            "assigned_by": _uuid(self.assigned_by), "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict

    @property
    def assigned_by_user_id(self):
        return self.assigned_by

    @assigned_by_user_id.setter
    def assigned_by_user_id(self, value):
        self.assigned_by = value

    @property
    def assignment_type(self) -> str:
        return self.assignment_kind

    @assignment_type.setter
    def assignment_type(self, value: str) -> None:
        self.assignment_kind = value


class AgentProjectGrant(Base):
    """Project ACL for an Agent, separate from human ProjectMember rows."""

    __tablename__ = "agent_project_grants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False, default="viewer", server_default="viewer")
    permissions = Column(JSON, nullable=False, default=dict, server_default="{}")
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    granted_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','member','viewer')", name="ck_agent_project_grants_role"),
        CheckConstraint("state IN ('active','revoked','expired')", name="ck_agent_project_grants_state"),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_project_grants_dates",
        ),
        Index(
            "uq_agent_project_grants_active_project",
            "agent_id", "project_id", unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_agent_project_grants_project_state", "project_id", "state"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "agent_id": _uuid(self.agent_id), "project_id": _uuid(self.project_id),
            "role": self.role, "permissions": _safe_json(self.permissions, {}), "state": self.state,
            "active_from": _dt(self.active_from), "active_until": _dt(self.active_until),
            "granted_by": _uuid(self.granted_by), "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict

    @property
    def granted_by_user_id(self):
        return self.granted_by

    @granted_by_user_id.setter
    def granted_by_user_id(self, value):
        self.granted_by = value

    @property
    def permission_ceiling(self) -> Any:
        return self.permissions

    @permission_ceiling.setter
    def permission_ceiling(self, value: Any) -> None:
        self.permissions = value


class AgentTaskAssignment(Base):
    """Task assignment for an Agent, separate from TaskAssignee.user_id."""

    __tablename__ = "agent_task_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    assignment_role = Column(String(16), nullable=False, default="executor", server_default="executor")
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        CheckConstraint(
            "assignment_role IN ('owner','executor','reviewer','observer')",
            name="ck_agent_task_assignments_role",
        ),
        CheckConstraint("state IN ('active','revoked','expired')", name="ck_agent_task_assignments_state"),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_task_assignments_dates",
        ),
        Index(
            "uq_agent_task_assignments_active_equivalent",
            "agent_id", "task_id", "assignment_role", unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_agent_task_assignments_task_state", "task_id", "state"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "agent_id": _uuid(self.agent_id), "task_id": _uuid(self.task_id),
            "assignment_role": self.assignment_role, "role": self.assignment_role, "state": self.state,
            "active_from": _dt(self.active_from), "active_until": _dt(self.active_until),
            "assigned_by": _uuid(self.assigned_by), "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict

    @property
    def assigned_by_user_id(self):
        return self.assigned_by

    @assigned_by_user_id.setter
    def assigned_by_user_id(self, value):
        self.assigned_by = value

class PersonaOperatorAssignment(Base):
    """Explicit bridge from a generic Agent to a Media Persona."""

    __tablename__ = "persona_operator_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    persona_id = Column(UUID(as_uuid=True), ForeignKey("media_personas.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False, default="operator", server_default="operator")
    is_primary = Column(Boolean, nullable=False, default=False, server_default="false")
    capability_ceiling_json = Column("capability_ceiling", JSON, nullable=False, default=list, server_default="[]")
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        CheckConstraint(
            "role IN ('operator','strategist','researcher','creator','analyst','publisher')",
            name="ck_persona_operator_assignments_role",
        ),
        CheckConstraint("state IN ('active','revoked','expired')", name="ck_persona_operator_assignments_state"),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_persona_operator_assignments_dates",
        ),
        Index(
            "uq_persona_operator_assignments_active_role",
            "agent_id", "persona_id", "role", unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_persona_operator_assignments_persona_state", "persona_id", "state"),
    )

    @property
    def capability_ceiling(self) -> Any:
        return self.capability_ceiling_json

    @capability_ceiling.setter
    def capability_ceiling(self, value: Any) -> None:
        self.capability_ceiling_json = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id), "agent_id": _uuid(self.agent_id), "persona_id": _uuid(self.persona_id),
            "role": self.role, "is_primary": bool(self.is_primary),
            "capability_ceiling": list(_json(self.capability_ceiling_json, [])), "state": self.state,
            "active_from": _dt(self.active_from), "active_until": _dt(self.active_until),
            "assigned_by": _uuid(self.assigned_by), "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict

    @property
    def assigned_by_user_id(self):
        return self.assigned_by

    @assigned_by_user_id.setter
    def assigned_by_user_id(self, value):
        self.assigned_by = value

    @property
    def operator_role(self) -> str:
        return self.role

    @operator_role.setter
    def operator_role(self, value: str) -> None:
        self.role = value


__all__ = [
    "AGENT_STATES",
    "AGENT_ASSIGNMENT_STATES",
    "AGENT_SPACE_ASSIGNMENT_KINDS",
    "AGENT_PROJECT_ROLES",
    "AGENT_TASK_ASSIGNMENT_ROLES",
    "PERSONA_OPERATOR_ROLES",
    "AUTONOMY_LEVELS",
    "EMPLOYMENT_STATES",
    "Organization",
    "Agent",
    "AgentRevision",
    "AgentOrganizationProfile",
    "AgentSpaceAssignment",
    "AgentProjectGrant",
    "AgentTaskAssignment",
    "PersonaOperatorAssignment",
    "OrganizationSettings",
]

# Naming alias retained for callers that call the deployment singleton a
# settings row; it is the same table/identity, not a second model.
OrganizationSettings = Organization
