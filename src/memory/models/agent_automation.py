"""Persistence models for AI employee automation and action policy state.

``Agent`` remains the canonical employee identity.  These tables only add
automation/policy bindings and immutable evidence around that identity; they
do not introduce a second Employee table or an execution queue.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, synonym

from .base import Base


AGENT_AUTOMATION_RULE_STATES = ("draft", "active", "paused", "retired")
AGENT_AUTOMATION_CONDITION_MODES = ("always", "keywords", "semantic")
AGENT_AUTOMATION_EVENT_ACTOR_KINDS = ("human", "agent", "service", "system")
AGENT_ACTION_POLICY_STATES = ("draft", "active", "paused", "retired")
AGENT_ACTION_POLICY_AUTHORIZATION_MODES = ("human_approval", "bounded_auto")
AGENT_ACTION_POLICY_FALLBACK_BEHAVIORS = ("require_approval", "block")

# Short aliases make the constants convenient for service code without
# changing the canonical names above.
AUTOMATION_RULE_STATES = AGENT_AUTOMATION_RULE_STATES
AUTOMATION_CONDITION_MODES = AGENT_AUTOMATION_CONDITION_MODES
AUTOMATION_EVENT_ACTOR_KINDS = AGENT_AUTOMATION_EVENT_ACTOR_KINDS
ACTION_POLICY_STATES = AGENT_ACTION_POLICY_STATES
ACTION_POLICY_AUTHORIZATION_MODES = AGENT_ACTION_POLICY_AUTHORIZATION_MODES
ACTION_POLICY_FALLBACK_BEHAVIORS = AGENT_ACTION_POLICY_FALLBACK_BEHAVIORS


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "apikey",
    "cookie",
    "authorization",
    "bearer",
    "private_key",
    "webhook",
)
_SECRET_VALUE_MARKERS = (
    "bearer ",
    "api_key=",
    "apikey=",
    "password=",
    "secret=",
    "token=",
    "authorization:",
)


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Bound JSON projections without exposing credential-shaped leaves."""

    if depth > 5:
        return None
    if isinstance(value, dict):
        if len(value) > 64:
            return {}
        result: dict[str, Any] = {}
        for key, item in value.items():
            rendered = str(key)
            folded = rendered.casefold()
            if any(marker in folded for marker in _SECRET_KEY_MARKERS):
                continue
            result[rendered] = _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:128]]
    if isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in _SECRET_VALUE_MARKERS):
            return "[REDACTED]"
        return value[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_text(value: Any, *, limit: int = 1200) -> str | None:
    rendered = str(value or "")
    if not rendered:
        return ""
    lowered = rendered.casefold()
    if any(marker in lowered for marker in _SECRET_KEY_MARKERS + _SECRET_VALUE_MARKERS):
        return "[REDACTED]"
    return rendered[:limit]


class AgentAutomationRule(Base):
    """Stable lifecycle row for one Agent-owned automation rule."""

    __tablename__ = "agent_automation_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name = Column(String(160), nullable=False)
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    idempotency_key = Column(String(255), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __mapper_args__ = {"version_id_col": version}

    agent = relationship("Agent")
    revisions = relationship(
        "AgentAutomationRuleRevision",
        back_populates="rule",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentAutomationRuleRevision.version",
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "idempotency_key", name="uq_agent_automation_rules_idempotency"),
        CheckConstraint("version > 0", name="ck_agent_automation_rules_version"),
        CheckConstraint(
            "state IN ('draft','active','paused','retired')",
            name="ck_agent_automation_rules_state",
        ),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 160",
            name="ck_agent_automation_rules_display_name",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "agent_id": _uuid(self.agent_id),
            "display_name": self.display_name,
            "state": self.state,
            "version": int(self.version or 1),
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentAutomationRuleRevision(Base):
    """Immutable, versioned trigger definition for an automation rule."""

    __tablename__ = "agent_automation_rule_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_automation_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    agent_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(80), nullable=False, index=True)
    trigger_config_json = Column(
        "trigger_config", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    condition_mode = Column(String(16), nullable=False, default="always", server_default="always")
    condition_config_json = Column(
        "condition_config", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    priority = Column(Integer, nullable=False, default=0, server_default="0", index=True)
    concurrency_key = Column(String(255), nullable=True, index=True)
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    idempotency_key = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    # Historical human identity: SET NULL would mutate immutable evidence.
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    rule = relationship("AgentAutomationRule", back_populates="revisions")
    agent_revision = relationship("AgentRevision")
    actions = relationship(
        "AgentAutomationRuleAction",
        back_populates="rule_revision",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentAutomationRuleAction.position",
    )

    # JSON aliases follow the naming used by the existing models.
    trigger_config = synonym("trigger_config_json")
    condition_config = synonym("condition_config_json")

    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_agent_automation_rule_revisions_version"),
        UniqueConstraint(
            "rule_id", "idempotency_key", name="uq_agent_automation_rule_revisions_idempotency"
        ),
        CheckConstraint("version > 0", name="ck_agent_automation_rule_revisions_version_positive"),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 80",
            name="ck_agent_automation_rule_revisions_event_type",
        ),
        CheckConstraint(
            "condition_mode IN ('always','keywords','semantic')",
            name="ck_agent_automation_rule_revisions_condition_mode",
        ),
        CheckConstraint(
            "priority BETWEEN -1000000 AND 1000000",
            name="ck_agent_automation_rule_revisions_priority",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 100",
            name="ck_agent_automation_rule_revisions_max_attempts",
        ),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_automation_rule_revisions_dates",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_agent_automation_rule_revisions_content_hash",
        ),
        Index(
            "ix_agent_automation_rule_revisions_rule_created",
            "rule_id",
            "created_at",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "rule_id": _uuid(self.rule_id),
            "version": int(self.version or 1),
            "agent_revision_id": _uuid(self.agent_revision_id),
            "event_type": self.event_type,
            "trigger_config": _safe_json(self.trigger_config_json),
            "condition_mode": self.condition_mode,
            "condition_config": _safe_json(self.condition_config_json),
            "priority": int(self.priority or 0),
            "concurrency_key": self.concurrency_key,
            "max_attempts": int(self.max_attempts or 1),
            "active_from": _dt(self.active_from),
            "active_until": _dt(self.active_until),
            "idempotency_key": self.idempotency_key,
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentActionPolicy(Base):
    """Stable lifecycle row for one Agent authorization policy."""

    __tablename__ = "agent_action_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name = Column(String(160), nullable=False)
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    idempotency_key = Column(String(255), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __mapper_args__ = {"version_id_col": version}

    agent = relationship("Agent")
    revisions = relationship(
        "AgentActionPolicyRevision",
        back_populates="policy",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentActionPolicyRevision.version",
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "idempotency_key", name="uq_agent_action_policies_idempotency"),
        CheckConstraint("version > 0", name="ck_agent_action_policies_version"),
        CheckConstraint(
            "state IN ('draft','active','paused','retired')",
            name="ck_agent_action_policies_state",
        ),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 160",
            name="ck_agent_action_policies_display_name",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "agent_id": _uuid(self.agent_id),
            "display_name": self.display_name,
            "state": self.state,
            "version": int(self.version or 1),
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentActionPolicyRevision(Base):
    """Immutable authorization contract for one registered action key."""

    __tablename__ = "agent_action_policy_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_action_policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    action_type = Column(String(80), nullable=False, index=True)
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("external_connections.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    authorization_mode = Column(
        String(24), nullable=False, default="human_approval", server_default="human_approval"
    )
    constraints_json = Column(
        "constraints", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    rate_limit_json = Column(
        "rate_limit", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    dedupe_window_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    fallback_behavior = Column(
        String(24), nullable=False, default="require_approval", server_default="require_approval"
    )
    active_from = Column(DateTime, nullable=True)
    active_until = Column(DateTime, nullable=True)
    registry_schema_version = Column(String(32), nullable=False, default="1", server_default="1")
    registry_adapter_key = Column(String(128), nullable=True)
    registry_adapter_version = Column(String(32), nullable=True)
    content_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    policy = relationship("AgentActionPolicy", back_populates="revisions")
    connection = relationship("ExternalConnection")

    constraints = synonym("constraints_json")
    rate_limit = synonym("rate_limit_json")

    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_agent_action_policy_revisions_version"),
        UniqueConstraint(
            "policy_id", "idempotency_key", name="uq_agent_action_policy_revisions_idempotency"
        ),
        CheckConstraint("version > 0", name="ck_agent_action_policy_revisions_version_positive"),
        CheckConstraint(
            "length(trim(action_type)) BETWEEN 1 AND 80 AND action_type = lower(action_type) AND action_type = trim(action_type) AND action_type NOT LIKE '% %'",
            name="ck_agent_action_policy_revisions_action_type",
        ),
        CheckConstraint(
            "authorization_mode IN ('human_approval','bounded_auto')",
            name="ck_agent_action_policy_revisions_authorization_mode",
        ),
        CheckConstraint(
            "dedupe_window_seconds >= 0",
            name="ck_agent_action_policy_revisions_dedupe_window",
        ),
        CheckConstraint(
            "fallback_behavior IN ('require_approval','block')",
            name="ck_agent_action_policy_revisions_fallback_behavior",
        ),
        CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_action_policy_revisions_dates",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_agent_action_policy_revisions_content_hash",
        ),
        Index(
            "ix_agent_action_policy_revisions_policy_created",
            "policy_id",
            "created_at",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "policy_id": _uuid(self.policy_id),
            "version": int(self.version or 1),
            "action_type": self.action_type,
            "connection_id": _uuid(self.connection_id),
            "authorization_mode": self.authorization_mode,
            "constraints": _safe_json(self.constraints_json),
            "rate_limit": _safe_json(self.rate_limit_json),
            "dedupe_window_seconds": int(self.dedupe_window_seconds or 0),
            "fallback_behavior": self.fallback_behavior,
            "active_from": _dt(self.active_from),
            "active_until": _dt(self.active_until),
            "registry_schema_version": self.registry_schema_version,
            "registry_adapter_key": self.registry_adapter_key,
            "registry_adapter_version": self.registry_adapter_version,
            "content_hash": self.content_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentAutomationRuleAction(Base):
    """Ordered binding from a rule revision to a policy revision."""

    __tablename__ = "agent_automation_rule_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_automation_rule_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = Column(Integer, nullable=False, default=0)
    action_policy_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_action_policy_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    input_mapping_json = Column(
        "input_mapping", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    on_noop = Column(String(16), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    rule_revision = relationship("AgentAutomationRuleRevision", back_populates="actions")
    action_policy_revision = relationship("AgentActionPolicyRevision")

    input_mapping = synonym("input_mapping_json")

    __table_args__ = (
        UniqueConstraint(
            "rule_revision_id", "position", name="uq_agent_automation_rule_actions_position"
        ),
        CheckConstraint("position >= 0", name="ck_agent_automation_rule_actions_position"),
        CheckConstraint(
            "on_noop IS NULL OR on_noop IN ('continue','stop')",
            name="ck_agent_automation_rule_actions_on_noop",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "rule_revision_id": _uuid(self.rule_revision_id),
            "position": int(self.position or 0),
            "action_policy_revision_id": _uuid(self.action_policy_revision_id),
            "input_mapping": _safe_json(self.input_mapping_json),
            "on_noop": self.on_noop,
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentAutomationEvent(Base):
    """Immutable trigger fact; it is deliberately not an execution queue."""

    __tablename__ = "agent_automation_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(String(80), nullable=False, index=True)
    source_type = Column(String(80), nullable=False, index=True)
    source_id = Column(String(255), nullable=False, index=True)
    source_revision = Column(String(160), nullable=False, index=True)
    # Source identities are historical evidence, not ownership links. Keeping
    # them without FKs permits source deletion without rewriting this fact.
    project_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    space_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    conversation_session_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    actor_kind = Column(String(16), nullable=False, default="system", server_default="system")
    actor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    service_actor_key = Column(String(120), nullable=True)
    safe_metadata_json = Column(
        "safe_metadata", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    event_hash = Column(String(64), nullable=False, index=True)
    occurred_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    safe_metadata = synonym("safe_metadata_json")

    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "source_type",
            "source_id",
            "source_revision",
            name="uq_agent_automation_events_source_identity",
        ),
        CheckConstraint("length(event_type) BETWEEN 1 AND 80", name="ck_agent_automation_events_event_type"),
        CheckConstraint("length(source_type) BETWEEN 1 AND 80", name="ck_agent_automation_events_source_type"),
        CheckConstraint("length(source_id) BETWEEN 1 AND 255", name="ck_agent_automation_events_source_id"),
        CheckConstraint(
            "length(source_revision) BETWEEN 1 AND 160",
            name="ck_agent_automation_events_source_revision",
        ),
        CheckConstraint(
            "actor_kind IN ('human','agent','service','system')",
            name="ck_agent_automation_events_actor_kind",
        ),
        CheckConstraint("length(event_hash) = 64", name="ck_agent_automation_events_event_hash"),
        CheckConstraint(
            "NOT (actor_id IS NOT NULL AND service_actor_key IS NOT NULL)",
            name="ck_agent_automation_events_actor_fields_xor",
        ),
        Index("ix_agent_automation_events_type_occurred", "event_type", "occurred_at"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "event_type": self.event_type,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "project_id": _uuid(self.project_id),
            "space_id": _uuid(self.space_id),
            "conversation_session_id": _uuid(self.conversation_session_id),
            "actor_kind": self.actor_kind,
            "actor_id": _uuid(self.actor_id),
            "service_actor_key": self.service_actor_key,
            "safe_metadata": _safe_json(self.safe_metadata_json),
            "event_hash": self.event_hash,
            "occurred_at": _dt(self.occurred_at),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentAutomationDiscoveryCursor(Base):
    """Discovery pagination only; never execution ownership or a queue."""

    __tablename__ = "agent_automation_discovery_cursors"
    key = Column(String(80), primary_key=True, default="rules")
    after_rule_id = Column(UUID(as_uuid=True), nullable=True)
    through_rule_id = Column(UUID(as_uuid=True), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))


class AgentAutomationRuleDiscoveryCursor(Base):
    """Per-revision cyclic event scan position, advanced with work materialization."""

    __tablename__ = "agent_automation_rule_discovery_cursors"
    rule_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_automation_rule_revisions.id", ondelete="CASCADE"), primary_key=True)
    after_occurred_at = Column(DateTime, nullable=True)
    after_event_id = Column(UUID(as_uuid=True), nullable=True)
    sweep_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        CheckConstraint("(after_occurred_at IS NULL AND after_event_id IS NULL) OR (after_occurred_at IS NOT NULL AND after_event_id IS NOT NULL)", name="ck_automation_rule_discovery_cursor_pair"),
    )


__all__ = [
    "AgentAutomationDiscoveryCursor",
    "AgentAutomationRuleDiscoveryCursor",
    "AGENT_AUTOMATION_RULE_STATES",
    "AGENT_AUTOMATION_CONDITION_MODES",
    "AGENT_AUTOMATION_EVENT_ACTOR_KINDS",
    "AGENT_ACTION_POLICY_STATES",
    "AGENT_ACTION_POLICY_AUTHORIZATION_MODES",
    "AGENT_ACTION_POLICY_FALLBACK_BEHAVIORS",
    "AUTOMATION_RULE_STATES",
    "AUTOMATION_CONDITION_MODES",
    "AUTOMATION_EVENT_ACTOR_KINDS",
    "ACTION_POLICY_STATES",
    "ACTION_POLICY_AUTHORIZATION_MODES",
    "ACTION_POLICY_FALLBACK_BEHAVIORS",
    "AgentAutomationRule",
    "AgentAutomationRuleRevision",
    "AgentAutomationRuleAction",
    "AgentAutomationEvent",
    "AgentActionPolicy",
    "AgentActionPolicyRevision",
]
