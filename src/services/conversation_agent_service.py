"""Canonical Agent identity binding for Chat conversations.

Chat sessions may opt into one durable Operations ``Agent`` while retaining
the existing Character and group-chat modes.  This service owns the boundary
between the human session ACL, Agent/Revision identity, and the immutable
execution snapshot consumed by the run layer.

The service intentionally does not execute a run or resolve tool authority.
Those decisions remain in ``AgentRunService`` and ``AgentAuthorityResolver``.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4
from types import MappingProxyType

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentExecutionBinding,
    AgentPrivateRoot,
    AgentProjectGrant,
    AgentRevision,
    AgentRun,
    AgentWorkItem,
    ConversationSession,
)
from ..memory.project_repository import ProjectRepository
from ..services.agent_model_routing import (
    AgentRoutingPins,
    build_agent_routing_snapshot,
)
from ..services.agent_revision_v2 import (
    revision_delegate_policy,
    revision_model_overrides,
    revision_v2_fields_present,
    revision_tool_ids,
)


_BUSY_RUN_STATES = frozenset({"queued", "running", "awaiting_approval", "uncertain"})
_AGENT_REVISION_V2_SNAPSHOT_KEY = "agent_revision_v2"


class ConversationAgentError(RuntimeError):
    """Base error with a stable API-safe error code."""

    status_code = 400
    code = "conversation_agent_error"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(message or self.code)


class ConversationAgentNotFoundError(ConversationAgentError):
    status_code = 404
    code = "conversation_agent_not_found"


class ConversationAgentAuthorizationError(ConversationAgentError):
    status_code = 403
    code = "conversation_agent_forbidden"


class ConversationAgentConflictError(ConversationAgentError):
    status_code = 409
    code = "conversation_agent_conflict"


class ConversationAgentValidationError(ConversationAgentError):
    status_code = 400
    code = "conversation_agent_invalid"


def _uuid(value: Any, field: str, *, required: bool = True) -> UUID | None:
    if value in (None, "") and not required:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConversationAgentValidationError(f"invalid {field}", code=f"invalid_{field}") from exc


def _text(value: Any, *, limit: int = 4096) -> str:
    text = str(value or "")
    return text[:limit]


def _prose(value: Any) -> str:
    """Preserve canonical revision prose without a lossy API projection."""

    return str(value or "")


def _safe_list(value: Any, *, limit: int = 128) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    for item in list(value)[:limit]:
        token = str(item or "").strip()
        if token and token not in result:
            result.append(token[:160])
    return result


def _safe_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _revision_v2_snapshot_fields(revision: Any) -> dict[str, Any]:
    """Project only non-NULL AgentRevision v2 policy fields for a binding."""

    result: dict[str, Any] = {}
    model_overrides = revision_model_overrides(revision)
    tool_ids = revision_tool_ids(revision)
    delegate_policy = revision_delegate_policy(revision)
    if model_overrides is not None:
        result["model_overrides"] = deepcopy(model_overrides)
    if tool_ids is not None:
        result["tool_ids"] = list(tool_ids)
    if delegate_policy is not None:
        result["delegate_policy"] = deepcopy(delegate_policy)
    return result


def _v2_snapshot_field(value: Any, name: str) -> Any:
    """Read one bounded v2 field from the namespaced route snapshot."""

    if not isinstance(value, Mapping):
        return None
    raw = value.get(name)
    if name == "model_overrides":
        return deepcopy(dict(raw)) if isinstance(raw, Mapping) else None
    if name == "delegate_policy":
        return deepcopy(dict(raw)) if isinstance(raw, Mapping) else None
    if name == "tool_ids":
        return (
            tuple(_safe_list(raw))
            if isinstance(raw, (list, tuple, set, frozenset))
            else None
        )
    return None


def _agent_session_projection(row: ConversationSession) -> dict[str, Any]:
    """Safe Agent activity projection without private conversation state."""

    payload = row.to_dict()
    root_metadata = getattr(row, "_private_agent_root_metadata", None)
    if isinstance(root_metadata, Mapping):
        payload["private_agent_root"] = True
        payload["purpose"] = root_metadata.get("purpose")
        payload["readonly_binding"] = bool(
            root_metadata.get("readonly_binding", True)
        )
        payload["private_agent_root_metadata"] = dict(root_metadata)
    allowed = (
        "id",
        "user_id",
        "character_name",
        "title",
        "session_start",
        "last_activity",
        "message_count",
        "is_active",
        "is_pinned",
        "pinned_at",
        "pin_updated_at",
        "project_id",
        "agent_id",
        "agent_binding_updated_at",
        "character_id",
        "app_id",
        "app_target_id",
        "development_status",
        "last_read_at",
        "parent_session_id",
        "forked_from_message_id",
        "is_group_chat",
        "private_agent_root",
        "purpose",
        "readonly_binding",
        "private_agent_root_metadata",
    )
    return {key: payload.get(key) for key in allowed if key in payload}


@dataclass(frozen=True)
class AgentExecutionBindingSnapshot:
    """Trusted immutable revision/topology data passed to a run."""

    binding_id: str | None
    session_id: str
    agent_id: str
    agent_revision_id: str
    revision_version: int
    display_name: str
    mission: str
    responsibility_summary: str
    operational_instructions: str
    agent_team_id: str
    execution_profile_id: str
    allowed_subagent_ids: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    route_snapshot: Mapping[str, Any]
    revision_content_hash: str
    snapshot_hash: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    # Additive P2 policy fields.  NULL means the immutable binding predates
    # v2; the values are persisted inside route_snapshot only when present.
    model_overrides: Mapping[str, Any] | None = None
    tool_ids: tuple[str, ...] | None = None
    delegate_policy: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        # Freeze the outer routing map as well as the scalar/tuple fields. The
        # persisted row is immutable; callers must not be able to mutate the
        # object handed to run creation between ACL validation and dispatch.
        object.__setattr__(
            self,
            "route_snapshot",
            _freeze_json(deepcopy(dict(self.route_snapshot or {}))),
        )
        v2 = _thaw_json(self.route_snapshot).get(_AGENT_REVISION_V2_SNAPSHOT_KEY)
        if self.model_overrides is None:
            object.__setattr__(
                self,
                "model_overrides",
                _v2_snapshot_field(v2, "model_overrides"),
            )
        if self.tool_ids is None:
            object.__setattr__(
                self,
                "tool_ids",
                _v2_snapshot_field(v2, "tool_ids"),
            )
        if self.delegate_policy is None:
            object.__setattr__(
                self,
                "delegate_policy",
                _v2_snapshot_field(v2, "delegate_policy"),
            )
        for name in ("model_overrides", "delegate_policy"):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, _freeze_json(deepcopy(dict(value))))
        if self.tool_ids is not None:
            object.__setattr__(self, "tool_ids", tuple(_safe_list(self.tool_ids)))

    @classmethod
    def from_row(cls, row: AgentExecutionBinding) -> "AgentExecutionBindingSnapshot":
        raw = row.to_snapshot()
        snapshot = cls(
            binding_id=str(raw.get("id")) if raw.get("id") else None,
            session_id=str(raw.get("session_id")),
            agent_id=str(raw.get("agent_id")),
            agent_revision_id=str(raw.get("agent_revision_id")),
            revision_version=int(raw.get("revision_version") or 0),
            display_name=_text(raw.get("display_name"), limit=160),
            mission=_prose(raw.get("mission")),
            responsibility_summary=_prose(raw.get("responsibility_summary")),
            operational_instructions=_prose(raw.get("operational_instructions")),
            agent_team_id=_text(raw.get("agent_team_id"), limit=100),
            execution_profile_id=_text(raw.get("execution_profile_id"), limit=100),
            allowed_subagent_ids=tuple(_safe_list(raw.get("allowed_subagent_ids"))),
            capability_ceiling=tuple(_safe_list(raw.get("capability_ceiling"))),
            route_snapshot=_safe_mapping(raw.get("route_snapshot")),
            revision_content_hash=_text(raw.get("revision_content_hash"), limit=64),
            snapshot_hash=_text(raw.get("snapshot_hash"), limit=64) or None,
            created_by=str(raw.get("created_by")) if raw.get("created_by") else None,
            created_at=str(raw.get("created_at")) if raw.get("created_at") else None,
        )
        if snapshot.snapshot_hash:
            material = snapshot.to_dict(include_prose=True)
            material.pop("id", None)
            material.pop("created_at", None)
            material.pop("snapshot_hash", None)
            material.pop("created_by", None)
            expected = hashlib.sha256(
                json.dumps(
                    material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if expected != snapshot.snapshot_hash:
                raise ConversationAgentConflictError(
                    "Conversation Agent binding integrity check failed",
                    code="agent_binding_hash_mismatch",
                )
        return snapshot

    @classmethod
    def from_revision(
        cls,
        *,
        session_id: UUID | str,
        agent: Agent,
        revision: AgentRevision,
        config: Any | None,
        created_by: UUID | str | None,
        binding_id: UUID | str | None = None,
        created_at: datetime | None = None,
        main_route_override: Mapping[str, Any] | None = None,
    ) -> "AgentExecutionBindingSnapshot":
        """Snapshot one exact P2 Agent revision for this conversation."""

        pins = AgentRoutingPins.from_revision(revision)
        if not revision_v2_fields_present(pins):
            raise ConversationAgentValidationError(
                "Agent revision has no current model/tool/delegation policy",
                code="agent_revision_legacy_runtime_retired",
            )
        route_snapshot = build_agent_routing_snapshot(
            config,
            pins,
            main_route_override=main_route_override,
        )
        v2_fields = _revision_v2_snapshot_fields(revision)
        if v2_fields:
            route_snapshot = dict(route_snapshot)
            route_snapshot[_AGENT_REVISION_V2_SNAPSHOT_KEY] = v2_fields
        return cls(
            binding_id=str(binding_id) if binding_id else None,
            session_id=str(session_id),
            agent_id=str(agent.id),
            agent_revision_id=str(revision.id),
            revision_version=int(revision.version or 0),
            display_name=_text(revision.display_name or agent.display_name, limit=160),
            mission=_prose(revision.mission),
            responsibility_summary=_prose(revision.responsibility_summary),
            operational_instructions=_prose(revision.operational_instructions),
            # The binding table still has non-null legacy columns for history.
            # New P2 bindings leave them empty and never use them for routing.
            agent_team_id="",
            execution_profile_id="",
            allowed_subagent_ids=(),
            capability_ceiling=tuple(_safe_list(revision.capability_ceiling_json)),
            route_snapshot=route_snapshot,
            revision_content_hash=_text(revision.content_hash, limit=64),
            created_by=str(created_by) if created_by else None,
            created_at=created_at.isoformat() if created_at else None,
            model_overrides=(
                deepcopy(v2_fields["model_overrides"])
                if "model_overrides" in v2_fields
                else None
            ),
            tool_ids=(
                tuple(v2_fields["tool_ids"])
                if "tool_ids" in v2_fields
                else None
            ),
            delegate_policy=(
                deepcopy(v2_fields["delegate_policy"])
                if "delegate_policy" in v2_fields
                else None
            ),
        )

    def to_revision_dict(self) -> dict[str, Any]:
        """Return the server-owned revision projection for run creation."""

        payload = {
            "id": self.agent_revision_id,
            "agent_id": self.agent_id,
            "version": self.revision_version,
            "display_name": self.display_name,
            "mission": self.mission,
            "responsibility_summary": self.responsibility_summary,
            "operational_instructions": self.operational_instructions,
            "capability_ceiling": list(self.capability_ceiling),
            "content_hash": self.revision_content_hash,
        }
        if self.agent_team_id or self.execution_profile_id or self.allowed_subagent_ids:
            payload["legacy"] = {
                "agent_team_id": self.agent_team_id or None,
                "execution_profile_id": self.execution_profile_id or None,
                "allowed_subagent_ids": list(self.allowed_subagent_ids) or None,
            }
        if self.model_overrides is not None:
            payload["model_overrides"] = _thaw_json(self.model_overrides)
        if self.tool_ids is not None:
            payload["tool_ids"] = list(self.tool_ids)
        if self.delegate_policy is not None:
            payload["delegate_policy"] = _thaw_json(self.delegate_policy)
        return payload

    def routing_pins(self) -> AgentRoutingPins:
        return AgentRoutingPins(
            agent_id=self.agent_id,
            agent_revision_id=self.agent_revision_id,
            agent_team_id=self.agent_team_id,
            execution_profile_id=self.execution_profile_id,
            allowed_subagent_ids=self.allowed_subagent_ids,
            # AgentRoutingPins is a transport value consumed by manifest
            # serializers; provide a JSON-native copy while the binding
            # snapshot itself remains immutable.
            route_snapshot=_thaw_json(self.route_snapshot),
            model_overrides=(
                _thaw_json(self.model_overrides)
                if self.model_overrides is not None
                else None
            ),
            tool_ids=tuple(self.tool_ids) if self.tool_ids is not None else None,
            delegate_policy=(
                _thaw_json(self.delegate_policy)
                if self.delegate_policy is not None
                else None
            ),
        )

    def to_dict(self, *, include_prose: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.binding_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "revision_version": self.revision_version,
            "display_name": self.display_name,
            "agent_team_id": self.agent_team_id,
            "execution_profile_id": self.execution_profile_id,
            "allowed_subagent_ids": list(self.allowed_subagent_ids),
            "capability_ceiling": list(self.capability_ceiling),
            "route_snapshot": _thaw_json(self.route_snapshot),
            "revision_content_hash": self.revision_content_hash,
            "snapshot_hash": self.snapshot_hash,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }
        if include_prose:
            payload.update(
                {
                    "mission": self.mission,
                    "responsibility_summary": self.responsibility_summary,
                    "operational_instructions": self.operational_instructions,
                }
            )
        return payload


@dataclass(frozen=True)
class ConversationAgentBindingResult:
    session: ConversationSession
    binding: AgentExecutionBindingSnapshot | None
    control_notices: tuple[dict[str, Any], ...] = ()


def _snapshot_hash(snapshot: AgentExecutionBindingSnapshot) -> str:
    material = snapshot.to_dict(include_prose=True)
    material.pop("id", None)
    material.pop("created_at", None)
    material.pop("snapshot_hash", None)
    material.pop("created_by", None)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ConversationAgentService:
    """Session/Agent binding and exact revision snapshot service."""

    def __init__(self, db_manager: Any | None = None, *, config: Any | None = None) -> None:
        self._db_manager = db_manager
        self.config = config

    async def _session(self):
        manager = self._db_manager or get_database_manager()
        value = manager.get_session()
        if hasattr(value, "__await__"):
            value = await value
        return value

    @staticmethod
    def _actor_uuid(actor_user_id: Any) -> UUID:
        return _uuid(actor_user_id, "actor_user_id")  # type: ignore[return-value]

    @staticmethod
    def _actor_is_admin(actor_role: Any) -> bool:
        return str(actor_role or "").strip().lower() == "admin"

    @staticmethod
    def _session_can_manage(row: ConversationSession, actor_id: UUID, actor_role: Any) -> bool:
        if ConversationAgentService._actor_is_admin(actor_role):
            return True
        if str(row.user_id) == str(actor_id):
            return True
        return any(
            str(item.participant_id) == str(actor_id)
            and item.participant_type == "user"
            and item.status == "joined"
            and item.role in {"owner", "admin"}
            for item in (getattr(row, "participants", None) or [])
        )

    @staticmethod
    def _session_can_read(row: ConversationSession, actor_id: UUID, actor_role: Any) -> bool:
        if ConversationAgentService._actor_is_admin(actor_role):
            return True
        if str(row.user_id) == str(actor_id):
            return True
        return any(
            str(item.participant_id) == str(actor_id)
            and item.participant_type == "user"
            and item.status == "joined"
            and item.role in {"owner", "admin", "member", "viewer"}
            for item in (getattr(row, "participants", None) or [])
        )

    @staticmethod
    async def _human_project_permission(
        session: Any,
        *,
        project_id: UUID | None,
        actor_id: UUID,
        actor_role: Any,
        permission: str,
    ) -> bool:
        if project_id is None:
            return True
        return bool(
            await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=actor_id,
                permission=permission,
            )
        )

    async def _latest_revision(self, session: Any, agent_id: UUID) -> AgentRevision | None:
        return (
            await session.execute(
                select(AgentRevision)
                .where(AgentRevision.agent_id == agent_id)
                .order_by(desc(AgentRevision.version), desc(AgentRevision.id))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _agent_for_selection(
        self,
        session: Any,
        *,
        agent_id: UUID,
        actor_id: UUID,
        actor_role: Any,
        project_id: UUID | None,
        require_project_read: bool = True,
        revision_id: UUID | None = None,
    ) -> tuple[Agent, AgentRevision]:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise ConversationAgentNotFoundError(code="agent_not_found")
        if str(agent.state or "") != "active":
            raise ConversationAgentAuthorizationError("Agent is not active", code="agent_inactive")
        revision = (await session.get(AgentRevision, revision_id) if revision_id
                    else await self._latest_revision(session, agent_id))
        if revision is None or revision.agent_id != agent_id:
            raise ConversationAgentAuthorizationError(
                "Agent has no usable revision", code="agent_revision_missing"
            )
        from .agent_revision_v2 import revision_v2_fields_present

        if (
            int(revision.version or 0) <= 0
            or len(str(revision.content_hash or "")) != 64
            or not revision_v2_fields_present(revision)
        ):
            raise ConversationAgentAuthorizationError(
                "Agent revision is invalid", code="agent_revision_invalid"
            )

        is_admin = self._actor_is_admin(actor_role)
        is_creator = agent.created_by is not None and str(agent.created_by) == str(actor_id)
        if not (is_admin or is_creator):
            if project_id is None:
                raise ConversationAgentAuthorizationError(
                    "Project grant is required for this Agent", code="agent_project_grant_missing"
                )
            human_read = await self._human_project_permission(
                session,
                project_id=project_id,
                actor_id=actor_id,
                actor_role=actor_role,
                permission="read",
            )
            if not human_read:
                raise ConversationAgentAuthorizationError(
                    "Human Project read permission is required", code="human_project_read_denied"
                )
            grant = (
                await session.execute(
                    select(AgentProjectGrant)
                    .where(
                        AgentProjectGrant.agent_id == agent_id,
                        AgentProjectGrant.project_id == project_id,
                        AgentProjectGrant.state == "active",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            now = datetime.utcnow()
            permissions = grant.permissions if grant is not None else None
            if (
                grant is None
                or (grant.active_from is not None and grant.active_from > now)
                or (grant.active_until is not None and grant.active_until < now)
                or not isinstance(permissions, Mapping)
                or permissions.get("read") is not True
            ):
                raise ConversationAgentAuthorizationError(
                    "Active Agent Project read grant is required",
                    code="agent_project_grant_missing",
                )

        if require_project_read and project_id is not None:
            # Creator/admin may choose the Agent, but the session still may
            # not be used to bypass the human Project ACL.
            if not await self._human_project_permission(
                session,
                project_id=project_id,
                actor_id=actor_id,
                actor_role=actor_role,
                permission="read",
            ):
                raise ConversationAgentAuthorizationError(
                    "Project read permission is required", code="project_read_denied"
                )
        return agent, revision

    async def validate_agent_selection(
        self,
        *,
        agent_id: Any,
        actor_user_id: Any,
        actor_role: Any = None,
        project_id: Any = None,
        db_session: Any | None = None,
    ) -> tuple[Agent, AgentRevision]:
        """Validate a requested Agent before a new session is persisted."""

        aid = _uuid(agent_id, "agent_id")
        actor = self._actor_uuid(actor_user_id)
        pid = _uuid(project_id, "project_id", required=False)
        own = db_session is None
        session = db_session or await self._session()
        try:
            return await self._agent_for_selection(
                session,
                agent_id=aid,  # type: ignore[arg-type]
                actor_id=actor,
                actor_role=actor_role,
                project_id=pid,
            )
        finally:
            if own:
                await session.close()

    async def _persist_snapshot(
        self,
        session: Any,
        *,
        row: ConversationSession,
        agent: Agent,
        revision: AgentRevision,
        actor_id: UUID | None,
        config: Any | None = None,
    ) -> AgentExecutionBindingSnapshot:
        # A conversation's explicit Main route (its model picker) is the
        # Main that an Agent profile inheriting Main resolves to.  Read the
        # persisted setting of this very session; never an ambient request
        # override, and never the global Main when the session chose its own.
        from .conversation_session_selection import (
            read_session_llm_settings,
            session_main_route_override_for_binding,
        )

        snapshot = AgentExecutionBindingSnapshot.from_revision(
            session_id=row.id,
            agent=agent,
            revision=revision,
            config=self.config if config is None else config,
            created_by=actor_id,
            main_route_override=session_main_route_override_for_binding(
                read_session_llm_settings(row.context)
            ),
        )
        # ``content_hash`` is a revision identity, while the binding hash is
        # derived from the complete trusted snapshot.  Keep the latter in the
        # row's revision_content_hash only when a legacy revision omitted a
        # valid hash; otherwise preserve the canonical revision hash verbatim.
        if len(snapshot.revision_content_hash) != 64:
            snapshot = AgentExecutionBindingSnapshot(
                **{
                    **snapshot.__dict__,
                    "revision_content_hash": _snapshot_hash(snapshot),
                }
            )
        binding = AgentExecutionBinding(
            session_id=row.id,
            agent_id=agent.id,
            agent_revision_id=revision.id,
            revision_version=snapshot.revision_version,
            display_name=snapshot.display_name,
            mission=snapshot.mission,
            responsibility_summary=snapshot.responsibility_summary,
            operational_instructions=snapshot.operational_instructions,
            agent_team_id=snapshot.agent_team_id,
            execution_profile_id=snapshot.execution_profile_id,
            allowed_subagent_ids=list(snapshot.allowed_subagent_ids),
            capability_ceiling_json=list(snapshot.capability_ceiling),
            route_snapshot=_thaw_json(snapshot.route_snapshot),
            revision_content_hash=snapshot.revision_content_hash,
            snapshot_hash=_snapshot_hash(snapshot),
            created_by=actor_id,
        )
        session.add(binding)
        await session.flush()
        return AgentExecutionBindingSnapshot.from_row(binding)

    async def prepare_work_session_binding(
        self,
        *,
        db_session: Any,
        work_item: Any,
        owner_user_id: Any,
        project_id: Any | None = None,
        agent_id: Any | None = None,
        agent_revision_id: Any | None = None,
    ) -> dict[str, Any]:
        """Prepare one durable owner/session/binding context for a Work row.

        This helper is intentionally a Work-only bridge for the dedicated
        CrowdWorks Agent source.  It locks the canonical Work row, creates or
        reuses its owner ConversationSession, and persists one exact immutable
        AgentRevision snapshot in the caller's transaction.  The caller owns
        the commit so the Work/session/binding updates become visible together.
        """

        owner = _uuid(owner_user_id, "owner_user_id")
        requested_project = _uuid(project_id, "project_id", required=False)
        requested_agent = _uuid(agent_id, "agent_id", required=False)
        requested_revision = _uuid(
            agent_revision_id, "agent_revision_id", required=False
        )
        if owner is None:
            raise ConversationAgentValidationError(
                "Work owner is invalid", code="work_owner_invalid"
            )

        work_id = getattr(work_item, "work_item_id", None) or getattr(
            work_item, "id", None
        )
        if work_id is None:
            work_id = work_item
        work_uuid = _uuid(work_id, "work_item_id")
        row = work_item if isinstance(work_item, AgentWorkItem) else None
        if row is None:
            row = await db_session.get(AgentWorkItem, work_uuid, with_for_update=True)
        else:
            # The refresh path may pass an already-loaded row.  Re-read it
            # under the same transaction so an immutable Work claim cannot be
            # replaced by a stale object before the binding is prepared.
            locked = await db_session.get(
                AgentWorkItem, work_uuid, with_for_update=True
            )
            if locked is not None:
                row = locked
        if row is None:
            raise ConversationAgentNotFoundError(
                "Work item was not found", code="work_item_not_found"
            )
        if (
            getattr(row, "source_type", None)
            != "marketplace_crowdworks_agent_scan"
            or getattr(row, "intent_key", None)
            != "marketplace.crowdworks.agent_scan"
            or getattr(row, "execution_adapter", None)
            != "crowdworks_agent_scan"
        ):
            raise ConversationAgentValidationError(
                "Work session binding is limited to the dedicated CrowdWorks source",
                code="work_binding_source_invalid",
            )

        return await self._prepare_validated_work_session_binding(
            db_session=db_session, row=row, owner=owner,
            requested_project=requested_project, requested_agent=requested_agent,
            requested_revision=requested_revision,
        )

    async def prepare_delegation_work_session_binding(
        self, *, db_session: Any, work_item: Any, delegation_service: Any,
    ) -> dict[str, Any]:
        """Admit only a currently authorized, fenced delegation phase Work."""
        from ..memory.models import AgentDelegation
        from .agent_delegation_runtime import ready_phase
        from .agent_delegation_service import (
            DelegationError, delegation_uuid, phase_identity, phase_sequence, source_revision,
        )

        row = await db_session.get(
            AgentWorkItem, _uuid(getattr(work_item, "work_item_id", None), "work_item_id"),
            with_for_update=True, populate_existing=True,
        )
        if row is None or row.source_type != "employee_delegation" or row.execution_adapter != "employee_delegation":
            raise ConversationAgentValidationError("Invalid delegation Work source", code="work_binding_source_invalid")
        order = await db_session.get(AgentDelegation, delegation_uuid(row.source_id), with_for_update=True, populate_existing=True)
        if order is None or order.cancelled_at or order.completed_at or row.intent_key not in phase_sequence(order):
            raise ConversationAgentConflictError("Delegation is unavailable", code="work_binding_source_invalid")
        phase, _ = await ready_phase(db_session, order)
        agent_id, revision_id = phase_identity(order, row.intent_key)
        if (phase != row.intent_key or row.source_revision != source_revision(order, row.intent_key)
                or row.assigned_agent_id != agent_id or row.agent_revision_id != revision_id
                or row.project_id != order.project_id or row.persona_id != order.persona_id
                or row.cancelled_at is not None or row.state != "running"
                or row.lease_token != getattr(work_item, "lease_token", None)
                or row.lease_owner != getattr(work_item, "lease_owner", None)
                or not row.lease_token or not row.lease_owner
                or row.lease_expires_at is None or row.lease_expires_at <= datetime.utcnow()
                or row.attempt_count != getattr(work_item, "attempt", None)
                or any(str(getattr(row, key, None) or "") != str(getattr(work_item, key, None) or "")
                       for key in ("source_type", "source_id", "source_revision", "intent_key", "execution_adapter", "assigned_agent_id", "agent_revision_id", "project_id", "persona_id"))):
            raise ConversationAgentConflictError("Delegation Work pins or lease changed", code="work_binding_source_invalid")
        try:
            await delegation_service.validate_authority(db_session, order)
        except DelegationError as exc:
            raise ConversationAgentConflictError("Delegation authority changed", code="work_binding_source_invalid") from exc
        metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
        existing_evidence = metadata.get("agent_binding")
        if not (isinstance(existing_evidence, Mapping)
                and existing_evidence.get("binding_id")
                and existing_evidence.get("session_id")
                and existing_evidence.get("snapshot_hash")):
            active_run = await db_session.scalar(select(AgentRun.id).where(
                AgentRun.work_item_id == row.id, AgentRun.status.in_(_BUSY_RUN_STATES),
            ).limit(1))
            if active_run is not None or row.active_agent_run_id is not None:
                raise ConversationAgentConflictError("Active Work Run has no binding", code="work_binding_mismatch")
        return await self._prepare_validated_work_session_binding(
            db_session=db_session, row=row, owner=order.created_by,
            requested_project=order.project_id, requested_agent=agent_id,
            requested_revision=revision_id,
        )

    async def _prepare_validated_work_session_binding(
        self, *, db_session: Any, row: Any, owner: Any,
        requested_project: Any, requested_agent: Any, requested_revision: Any,
    ) -> dict[str, Any]:
        stored_project = _uuid(getattr(row, "project_id", None), "project_id", required=False)
        if requested_project is None:
            requested_project = stored_project
        elif stored_project != requested_project:
            raise ConversationAgentConflictError(
                "Work project does not match its owner context",
                code="work_project_mismatch",
            )

        stored_agent = _uuid(
            getattr(row, "assigned_agent_id", None), "assigned_agent_id", required=False
        )
        if requested_agent is None:
            requested_agent = stored_agent
        elif stored_agent != requested_agent:
            raise ConversationAgentConflictError(
                "Work Agent does not match its owner context",
                code="work_agent_mismatch",
            )
        stored_revision = _uuid(
            getattr(row, "agent_revision_id", None),
            "agent_revision_id",
            required=False,
        )
        if requested_revision is None:
            requested_revision = stored_revision
        elif stored_revision != requested_revision:
            raise ConversationAgentConflictError(
                "Work AgentRevision does not match its owner context",
                code="work_agent_revision_mismatch",
            )
        if requested_agent is None or requested_revision is None:
            raise ConversationAgentValidationError(
                "Work must pin an exact AgentRevision",
                code="work_agent_binding_missing",
            )

        raw_metadata = getattr(row, "metadata_json", None)
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        stored_owner = _uuid(metadata.get("owner_user_id"), "owner_user_id", required=False)
        if stored_owner is not None and stored_owner != owner:
            raise ConversationAgentConflictError(
                "Work owner does not match its persisted owner context",
                code="work_owner_mismatch",
            )
        stored_metadata_project = _uuid(
            metadata.get("project_id"), "project_id", required=False
        )
        if stored_metadata_project != requested_project:
            # Missing metadata is filled below.  A non-null mismatch is a
            # stale source pin and must fail closed.
            if metadata.get("project_id") not in (None, ""):
                raise ConversationAgentConflictError(
                    "Work metadata project does not match its row",
                    code="work_project_mismatch",
                )

        session_id = _uuid(
            getattr(row, "conversation_session_id", None)
            or metadata.get("conversation_session_id"),
            "conversation_session_id",
            required=False,
        )
        binding_metadata = metadata.get("agent_binding")
        if binding_metadata is not None and not isinstance(binding_metadata, Mapping):
            raise ConversationAgentConflictError(
                "Work binding evidence is invalid",
                code="work_binding_mismatch",
            )
        binding_metadata = dict(binding_metadata or {})
        metadata_session_id = _uuid(
            binding_metadata.get("session_id"),
            "binding_session_id",
            required=False,
        )
        if metadata_session_id is not None and session_id != metadata_session_id:
            raise ConversationAgentConflictError(
                "Work binding session does not match its row",
                code="work_session_mismatch",
            )

        session_row = None
        if session_id is not None:
            session_row = await db_session.get(
                ConversationSession, session_id, with_for_update=True
            )
            if session_row is None or session_row.deleted_at is not None:
                raise ConversationAgentConflictError(
                    "Work ConversationSession is unavailable",
                    code="work_session_mismatch",
                )
            if str(session_row.user_id) != str(owner):
                raise ConversationAgentConflictError(
                    "Work ConversationSession owner does not match",
                    code="work_owner_mismatch",
                )
            if _uuid(session_row.project_id, "project_id", required=False) != requested_project:
                raise ConversationAgentConflictError(
                    "Work ConversationSession project does not match",
                    code="work_project_mismatch",
                )
            if _uuid(session_row.agent_id, "agent_id", required=False) not in (
                None,
                requested_agent,
            ):
                raise ConversationAgentConflictError(
                    "Work ConversationSession Agent does not match",
                    code="work_agent_mismatch",
                )
            if session_row.agent_id is None:
                session_row.agent_id = requested_agent
        else:
            session_row = ConversationSession(
                id=uuid4(),
                user_id=str(owner),
                character_name=("crowdworks_agent_scan" if row.source_type == "marketplace_crowdworks_agent_scan" else "employee_delegation"),
                title=("CrowdWorks Agent scan" if row.source_type == "marketplace_crowdworks_agent_scan" else "Employee delegation"),
                project_id=requested_project,
                agent_id=requested_agent,
                is_active=True,
            )
            db_session.add(session_row)
            await db_session.flush()
            session_id = session_row.id

        agent = await db_session.get(Agent, requested_agent, with_for_update=True)
        revision = await db_session.get(
            AgentRevision, requested_revision, with_for_update=True
        )
        if (
            agent is None
            or str(agent.state or "") != "active"
            or revision is None
            or _uuid(revision.agent_id, "agent_id") != requested_agent
            or int(revision.version or 0) <= 0
            or len(str(revision.content_hash or "")) != 64
            or any(value is None for value in (
                revision_model_overrides(revision), revision_tool_ids(revision),
                revision_delegate_policy(revision),
            ))
        ):
            raise ConversationAgentConflictError(
                "Work AgentRevision is unavailable",
                code="work_agent_revision_mismatch",
            )

        from .agent_identity_service import (
            AgentIdentityValidationError, _capability_list, _delegate_policy,
            _model_overrides, _tool_ids,
        )

        try:
            _model_overrides(revision_model_overrides(revision))
            _tool_ids(revision_tool_ids(revision), allow_concrete=True)
            _delegate_policy(revision_delegate_policy(revision))
            _capability_list(revision.capability_ceiling_json)
        except AgentIdentityValidationError as exc:
            raise ConversationAgentConflictError(
                "Work AgentRevision is invalid", code="work_agent_revision_mismatch"
            ) from exc

        binding_id = _uuid(
            binding_metadata.get("binding_id"), "binding_id", required=False
        )
        binding: AgentExecutionBindingSnapshot | None = None
        if binding_id is not None:
            binding_row = await self._load_binding_by_id(
                db_session,
                binding_id=binding_id,
                session_id=session_id,  # type: ignore[arg-type]
                agent_id=requested_agent,
                agent_revision_id=requested_revision,
            )
            if binding_row is None:
                raise ConversationAgentConflictError(
                    "Work binding evidence does not identify the exact row",
                    code="work_binding_mismatch",
                )
            binding = AgentExecutionBindingSnapshot.from_row(binding_row)
        else:
            pair_rows = (
                await db_session.execute(
                    select(AgentExecutionBinding)
                    .where(
                        AgentExecutionBinding.session_id == session_id,
                        AgentExecutionBinding.agent_id == requested_agent,
                        AgentExecutionBinding.agent_revision_id == requested_revision,
                    )
                    .order_by(
                        desc(AgentExecutionBinding.created_at),
                        desc(AgentExecutionBinding.id),
                    )
                    .limit(2)
                )
            ).scalars().all()
            if len(pair_rows) > 1:
                raise ConversationAgentConflictError(
                    "Work requires an exact immutable binding id",
                    code="work_binding_id_required",
                )
            if pair_rows:
                binding = AgentExecutionBindingSnapshot.from_row(pair_rows[0])
            else:
                binding = await self._persist_snapshot(
                    db_session,
                    row=session_row,
                    agent=agent,
                    revision=revision,
                    actor_id=owner,
                )

        if binding is None or binding.session_id != str(session_id):
            raise ConversationAgentConflictError(
                "Work binding session does not match",
                code="work_binding_mismatch",
            )
        for key, expected in (
            ("agent_id", requested_agent),
            ("agent_revision_id", requested_revision),
        ):
            if str(getattr(binding, key)) != str(expected):
                raise ConversationAgentConflictError(
                    "Work binding identity does not match",
                    code="work_binding_mismatch",
                )
        stored_hash = binding_metadata.get("snapshot_hash")
        if stored_hash not in (None, "") and str(stored_hash) != str(binding.snapshot_hash):
            raise ConversationAgentConflictError(
                "Work binding snapshot hash does not match",
                code="work_binding_mismatch",
            )

        evidence = {
            "binding_id": str(binding.binding_id),
            "session_id": str(session_id),
            "agent_id": str(requested_agent),
            "agent_revision_id": str(requested_revision),
            "revision_version": int(binding.revision_version),
            "revision_content_hash": str(binding.revision_content_hash),
            "snapshot_hash": str(binding.snapshot_hash),
        }
        metadata.update(
            {
                "owner_user_id": str(owner),
                "project_id": str(requested_project) if requested_project else None,
                "conversation_session_id": str(session_id),
                "agent_binding": evidence,
                "agent_binding_id": evidence["binding_id"],
                "agent_binding_snapshot_hash": evidence["snapshot_hash"],
            }
        )
        row.conversation_session_id = session_id
        row.metadata_json = metadata
        await db_session.flush()
        return {
            "owner_user_id": str(owner),
            "project_id": str(requested_project) if requested_project else None,
            "conversation_session_id": str(session_id),
            "agent_binding": evidence,
        }

    async def bind_session_agent(
        self,
        *,
        session_id: Any,
        agent_id: Any | None,
        actor_user_id: Any,
        actor_role: Any = None,
        expected_agent_id: Any | None = None,
    ) -> ConversationAgentBindingResult:
        """CAS-bind or unbind the canonical Agent for one conversation."""

        sid = _uuid(session_id, "session_id")
        target = _uuid(agent_id, "agent_id", required=False)
        expected = _uuid(expected_agent_id, "expected_agent_id", required=False)
        actor = self._actor_uuid(actor_user_id)
        session = await self._session()
        try:
            row = (
                await session.execute(
                    select(ConversationSession)
                    .options(selectinload(ConversationSession.participants))
                    .where(ConversationSession.id == sid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.deleted_at is not None:
                raise ConversationAgentNotFoundError(code="conversation_not_found")
            current = row.agent_id

            # Resolve the durable AgentPrivateRoot while the session lock is
            # held.  Repeating the current root Agent binding is idempotent;
            # detaching it or selecting another Agent would orphan the
            # canonical AoiDots counterpart and is a protected 409.
            from .agent_root_lifecycle import (
                AgentRootLifecycleError,
                guard_private_root_operation,
            )

            try:
                root_metadata = await guard_private_root_operation(
                    session,
                    row,
                    operation="agent_binding",
                    actor_user_id=actor,
                    requested_agent_id=target,
                )
            except AgentRootLifecycleError as exc:
                if exc.status_code == 403:
                    raise ConversationAgentAuthorizationError(
                        str(exc), code=exc.code
                    ) from exc
                raise ConversationAgentConflictError(
                    str(exc), code=exc.code
                ) from exc
            if (
                root_metadata is not None
                and target is not None
                and str(target) == str(current)
                and expected is None
            ):
                # Re-opening the canonical root may repeat its current Agent
                # selection without an optimistic token.  It is still a
                # no-op for identity purposes; detach/replacement remain
                # rejected above.
                expected = current
            if (str(current) if current else None) != (str(expected) if expected else None):
                raise ConversationAgentConflictError(
                    "Agent binding changed on the server", code="agent_binding_stale"
                )
            if bool(row.is_group_chat) and target is not None:
                raise ConversationAgentValidationError(
                    "Group chat sessions cannot bind a canonical Agent", code="group_agent_binding_forbidden"
                )
            if not self._session_can_manage(row, actor, actor_role):
                raise ConversationAgentAuthorizationError(
                    "Conversation management denied", code="conversation_manage_denied"
                )
            if row.project_id is not None and not await self._human_project_permission(
                session,
                project_id=row.project_id,
                actor_id=actor,
                actor_role=actor_role,
                permission="write",
            ):
                raise ConversationAgentAuthorizationError(
                    "Project write permission is required", code="project_write_denied"
                )

            busy = (
                await session.execute(
                    select(AgentRun.id)
                    .where(
                        AgentRun.session_id == row.id,
                        AgentRun.status.in_(_BUSY_RUN_STATES),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if busy is not None:
                raise ConversationAgentConflictError(
                    "Agent binding cannot change while a run is active", code="agent_binding_busy"
                )

            binding: AgentExecutionBindingSnapshot | None = None
            control_notices: list[dict[str, Any]] = []
            root_same_binding_noop = False
            explicit_human_binding_change = (
                (str(current) if current else None) != (str(target) if target else None)
                and str(actor_role or "").strip().casefold() != "agent"
            )
            if target is not None:
                agent, revision = await self._agent_for_selection(
                    session,
                    agent_id=target,
                    actor_id=actor,
                    actor_role=actor_role,
                    project_id=row.project_id,
                )
                row.agent_id = agent.id
                existing_binding = await self._load_binding(
                    session,
                    session_id=row.id,
                    agent_id=agent.id,
                    agent_revision_id=revision.id,
                )
                binding = (
                    AgentExecutionBindingSnapshot.from_row(existing_binding)
                    if existing_binding is not None
                    else await self._persist_snapshot(
                        session,
                        row=row,
                        agent=agent,
                        revision=revision,
                        actor_id=actor,
                    )
                )
                root_same_binding_noop = (
                    root_metadata is not None
                    and str(current) == str(target)
                    and existing_binding is not None
                    and str(existing_binding.agent_revision_id) == str(revision.id)
                )
            else:
                row.agent_id = None
            if explicit_human_binding_change:
                from .agent_root_lifecycle import fence_child_dialogues_for_binding_change

                control_notices = await fence_child_dialogues_for_binding_change(
                    session,
                    row,
                    actor_user_id=actor,
                    current_binding={
                        "agent_id": str(row.agent_id) if row.agent_id else None,
                        "agent_revision_id": (
                            str(binding.agent_revision_id)
                            if binding is not None
                            else None
                        ),
                    },
                    project_id=row.project_id,
                )
            if root_same_binding_noop:
                # The canonical Root already points at this exact Agent
                # revision.  Opening/reselecting it is a read-only no-op and
                # must not advance the binding mutation clock.
                return ConversationAgentBindingResult(
                    session=row,
                    binding=binding,
                    control_notices=tuple(control_notices),
                )
            row.agent_binding_updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(row)
            return ConversationAgentBindingResult(
                session=row,
                binding=binding,
                control_notices=tuple(control_notices),
            )
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _load_binding(
        self,
        session: Any,
        *,
        session_id: UUID,
        agent_id: UUID,
        agent_revision_id: UUID,
    ) -> AgentExecutionBinding | None:
        return (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == session_id,
                    AgentExecutionBinding.agent_id == agent_id,
                    AgentExecutionBinding.agent_revision_id == agent_revision_id,
                )
                .order_by(desc(AgentExecutionBinding.created_at), desc(AgentExecutionBinding.id))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _load_binding_by_id(
        self,
        session: Any,
        *,
        binding_id: UUID,
        session_id: UUID,
        agent_id: UUID,
        agent_revision_id: UUID,
    ) -> AgentExecutionBinding | None:
        """Load one exact immutable snapshot row; never fall back by pair."""

        return (
            await session.execute(
                select(AgentExecutionBinding).where(
                    AgentExecutionBinding.id == binding_id,
                    AgentExecutionBinding.session_id == session_id,
                    AgentExecutionBinding.agent_id == agent_id,
                    AgentExecutionBinding.agent_revision_id == agent_revision_id,
                )
            )
        ).scalar_one_or_none()

    async def resolve_execution_binding(
        self,
        *,
        session_id: Any,
        agent_id: Any,
        agent_revision_id: Any,
        binding_id: Any | None = None,
        allow_legacy: bool = False,
        db_session: Any | None = None,
    ) -> AgentExecutionBindingSnapshot:
        """Rebuild trusted execution inputs from an exact immutable snapshot.

        ``binding_id`` is required when a session has more than one snapshot
        for the same Agent/Revision pair (for example after a routing-config
        change).  This prevents a historical run from silently selecting the
        newest route snapshot.
        """

        sid = _uuid(session_id, "session_id")
        aid = _uuid(agent_id, "agent_id")
        rid = _uuid(agent_revision_id, "agent_revision_id")
        bid = _uuid(binding_id, "binding_id", required=False)
        own = db_session is None
        session = db_session or await self._session()
        try:
            if bid is not None:
                binding = await self._load_binding_by_id(
                    session,
                    binding_id=bid,
                    session_id=sid,  # type: ignore[arg-type]
                    agent_id=aid,  # type: ignore[arg-type]
                    agent_revision_id=rid,  # type: ignore[arg-type]
                )
                if binding is None:
                    raise ConversationAgentNotFoundError(
                        "Exact Agent binding was not found",
                        code="agent_binding_not_found",
                    )
            else:
                pair_rows = (
                    await session.execute(
                        select(AgentExecutionBinding)
                        .where(
                            AgentExecutionBinding.session_id == sid,
                            AgentExecutionBinding.agent_id == aid,
                            AgentExecutionBinding.agent_revision_id == rid,
                        )
                        .order_by(
                            desc(AgentExecutionBinding.created_at),
                            desc(AgentExecutionBinding.id),
                        )
                        .limit(2)
                    )
                ).scalars().all()
                if len(pair_rows) > 1:
                    raise ConversationAgentConflictError(
                        "Exact binding_id is required for this Agent/Revision pair",
                        code="agent_binding_id_required",
                    )
                binding = pair_rows[0] if pair_rows else None
            if binding is not None:
                return AgentExecutionBindingSnapshot.from_row(binding)
            if not allow_legacy:
                raise ConversationAgentNotFoundError(
                    "Exact Agent binding is required",
                    code="agent_binding_required",
                )
            # Legacy rows may have ``ConversationSession.agent_id`` without a
            # binding snapshot.  Rebuild from the exact pair only; never use
            # the latest revision when a run supplied a revision id.
            agent = await session.get(Agent, aid)
            revision = await session.get(AgentRevision, rid)
            if agent is None or revision is None or revision.agent_id != aid:
                raise ConversationAgentNotFoundError(code="agent_revision_not_found")
            return AgentExecutionBindingSnapshot.from_revision(
                session_id=sid,  # type: ignore[arg-type]
                agent=agent,
                revision=revision,
                config=self.config,
                created_by=None,
            )
        finally:
            if own:
                await session.close()

    async def resolve_execution_binding_by_id(
        self,
        *,
        binding_id: Any,
        session_id: Any,
        agent_id: Any,
        agent_revision_id: Any,
        db_session: Any | None = None,
    ) -> AgentExecutionBindingSnapshot:
        """Typed exact-row resolver for historical AgentRuns."""

        return await self.resolve_execution_binding(
            session_id=session_id,
            agent_id=agent_id,
            agent_revision_id=agent_revision_id,
            binding_id=binding_id,
            db_session=db_session,
        )

    async def pin_turn_binding(
        self,
        *,
        db_session: Any,
        session_id: Any,
        agent_id: Any | None = None,
        agent_revision_id: Any | None = None,
        actor_user_id: Any | None = None,
        actor_role: Any = None,
        config: Any | None = None,
        allow_legacy: bool = False,
        allow_pinned_revision: bool = False,
    ) -> AgentExecutionBindingSnapshot:
        """Create one immutable turn snapshot inside the caller's transaction.

        The current human/Project/grant ACL is re-validated. A stored
        snapshot never grants authority.

        The caller must commit this transaction together with the AgentRun.
        Every invocation creates a fresh binding row, even for the same
        Revision, so route changes between turns remain distinguishable by
        ``binding_id`` and ``snapshot_hash``.
        """

        sid = _uuid(session_id, "session_id")
        requested_agent = _uuid(agent_id, "agent_id", required=False)
        requested_revision = _uuid(
            agent_revision_id, "agent_revision_id", required=False
        )
        row = (
            await db_session.execute(
                select(ConversationSession)
                .where(ConversationSession.id == sid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.deleted_at is not None:
            raise ConversationAgentNotFoundError(code="conversation_not_found")
        canonical_agent = row.agent_id
        if canonical_agent is None:
            raise ConversationAgentValidationError(
                "Conversation has no canonical Agent", code="conversation_agent_missing"
            )
        if requested_agent is not None and str(canonical_agent) != str(requested_agent):
            raise ConversationAgentConflictError(
                "Conversation Agent changed on the server", code="conversation_agent_stale"
            )
        if actor_user_id is None and not allow_legacy:
            raise ConversationAgentAuthorizationError(
                "Human actor is required for Agent turn pinning",
                code="human_actor_required",
            )
        actor = self._actor_uuid(actor_user_id) if actor_user_id is not None else None
        if actor is not None:
            if not self._session_can_read(row, actor, actor_role):
                raise ConversationAgentAuthorizationError(
                    "Conversation read denied", code="conversation_read_denied"
                )
            if row.project_id is not None and not await self._human_project_permission(
                db_session,
                project_id=row.project_id,
                actor_id=actor,
                actor_role=actor_role,
                permission="read",
            ):
                raise ConversationAgentAuthorizationError(
                    "Project read permission is required", code="project_read_denied"
                )
        agent = (
            await db_session.execute(
                select(Agent)
                .where(Agent.id == canonical_agent)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if agent is None or str(agent.state or "") != "active":
            raise ConversationAgentAuthorizationError(
                "Agent is not active", code="agent_inactive"
            )
        if actor is not None:
            _selected_agent, revision = await self._agent_for_selection(
                db_session,
                agent_id=canonical_agent,
                actor_id=actor,
                actor_role=actor_role,
                project_id=row.project_id,
                revision_id=requested_revision if allow_pinned_revision else None,
            )
        else:
            revision = await self._latest_revision(db_session, canonical_agent)
        if (
            revision is None
            or str(revision.agent_id) != str(canonical_agent)
            or int(revision.version or 0) <= 0
            or len(str(revision.content_hash or "")) != 64
            or not revision_v2_fields_present(revision)
        ):
            raise ConversationAgentAuthorizationError(
                "Agent revision is invalid", code="agent_revision_invalid"
            )
        if requested_revision is not None and str(revision.id) != str(requested_revision):
            raise ConversationAgentConflictError(
                "Requested AgentRevision is stale",
                code="agent_revision_stale",
            )
        return await self._persist_snapshot(
            db_session,
            row=row,
            agent=agent,
            revision=revision,
            actor_id=actor,
            config=config,
        )

    async def list_available_agents(
        self,
        *,
        actor_user_id: Any,
        actor_role: Any = None,
        session_id: Any | None = None,
        project_id: Any | None = None,
        limit: int = 100,
        db_session: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Return active Agents usable by this human here, minus others' Private Roots."""

        actor = self._actor_uuid(actor_user_id)
        sid = _uuid(session_id, "session_id", required=False)
        pid = _uuid(project_id, "project_id", required=False)
        own = db_session is None
        session = db_session or await self._session()
        try:
            if sid is not None:
                row = (
                    await session.execute(
                        select(ConversationSession)
                        .options(selectinload(ConversationSession.participants))
                        .where(ConversationSession.id == sid)
                    )
                ).scalar_one_or_none()
                if row is None or row.deleted_at is not None:
                    raise ConversationAgentNotFoundError(code="conversation_not_found")
                if not self._session_can_manage(row, actor, actor_role):
                    raise ConversationAgentAuthorizationError(code="conversation_manage_denied")
                if pid is not None and row.project_id != pid:
                    raise ConversationAgentValidationError(code="project_session_mismatch")
                pid = row.project_id
                if pid is not None and not await self._human_project_permission(
                    session,
                    project_id=pid,
                    actor_id=actor,
                    actor_role=actor_role,
                    permission="read",
                ):
                    raise ConversationAgentAuthorizationError(code="project_read_denied")
            elif pid is not None and not await self._human_project_permission(
                session,
                project_id=pid,
                actor_id=actor,
                actor_role=actor_role,
                permission="read",
            ):
                raise ConversationAgentAuthorizationError(code="project_read_denied")

            safe_limit = min(max(int(limit or 100), 1), 200)
            # Private RootのAgentはowner限定で扱う。他者のPrivate Rootに登録された
            # Agentはadminのactorでも選択一覧へ出さない。呼び出し元自身のRootと、
            # Private Rootを持たない通常Agentは従来どおり可視のままとする。
            # 除外は下のAgent一括selectへ相関サブクエリとして埋め込み、
            # Agentごとの追加クエリ（N+1）を作らない。
            hidden_foreign_root = (
                select(AgentPrivateRoot.id)
                .where(
                    AgentPrivateRoot.agent_id == Agent.id,
                    AgentPrivateRoot.state != "deleted",
                    AgentPrivateRoot.owner_user_id != actor,
                )
                .exists()
            )
            agents = (
                await session.execute(
                    select(Agent)
                    .where(Agent.state == "active")
                    .where(~hidden_foreign_root)
                    .order_by(Agent.display_name, Agent.id)
                    .limit(safe_limit * 2)
                )
            ).scalars().all()
            result: list[dict[str, Any]] = []
            for agent in agents:
                revision = await self._latest_revision(session, agent.id)
                if revision is None:
                    continue
                try:
                    await self._agent_for_selection(
                        session,
                        agent_id=agent.id,
                        actor_id=actor,
                        actor_role=actor_role,
                        project_id=pid,
                        require_project_read=False,
                    )
                except ConversationAgentError:
                    continue
                result.append(
                    {
                        "id": str(agent.id),
                        "display_name": agent.display_name,
                        "slug": agent.slug,
                        "state": agent.state,
                        "latest_revision_id": str(revision.id),
                        "latest_revision_version": int(revision.version or 0),
                        "character_id": str(agent.character_id) if agent.character_id else None,
                        "created_by": str(agent.created_by) if agent.created_by else None,
                    }
                )
                if len(result) >= safe_limit:
                    break
            return result
        finally:
            if own:
                await session.close()

    async def list_agent_conversations(
        self,
        *,
        agent_id: Any,
        actor_user_id: Any,
        actor_role: Any = None,
        limit: int = 50,
        offset: int = 0,
        db_session: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Read bound history without making an inactive Agent executable."""

        aid = _uuid(agent_id, "agent_id")
        actor = self._actor_uuid(actor_user_id)
        own = db_session is None
        session = db_session or await self._session()
        try:
            from ..memory.models import User

            agent = await session.get(Agent, aid)
            if agent is None:
                raise ConversationAgentNotFoundError(code="agent_not_found")
            human = await session.get(User, actor)
            if (
                human is None
                or not human.is_active
                or str(actor_role or "").strip().casefold() == "agent"
            ):
                raise ConversationAgentAuthorizationError(
                    code="conversation_read_denied"
                )
            # Pausing/retiring stops selection, binding and turn admission,
            # not the human's existing history. Keep those execution paths on
            # _agent_for_selection's active-only gate, unchanged.
            if str(agent.state or "") not in {"active", "paused", "retired"}:
                return []
            revision = await self._latest_revision(session, aid)
            if (
                revision is None
                or revision.agent_id != aid
                or int(revision.version or 0) <= 0
                or len(str(revision.content_hash or "")) != 64
                or not revision_v2_fields_present(revision)
            ):
                return []
            foreign_root = (
                await session.execute(
                    select(AgentPrivateRoot.id)
                    .where(
                        AgentPrivateRoot.agent_id == aid,
                        AgentPrivateRoot.state != "deleted",
                        AgentPrivateRoot.owner_user_id != actor,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if foreign_root is not None:
                return []
            is_creator_or_admin = (
                self._actor_is_admin(actor_role) or agent.created_by == actor
            )
            safe_limit = min(max(int(limit or 50), 1), 200)
            safe_offset = max(int(offset or 0), 0)
            rows = (
                (
                    await session.execute(
                        select(ConversationSession)
                        .options(selectinload(ConversationSession.participants))
                        .where(
                            ConversationSession.agent_id == aid,
                            ConversationSession.deleted_at.is_(None),
                        )
                        .order_by(
                            desc(ConversationSession.last_activity),
                            desc(ConversationSession.session_start),
                            desc(ConversationSession.id),
                        )
                        .limit(safe_limit)
                        .offset(safe_offset)
                    )
                )
                .scalars()
                .all()
            )
            from .agent_root_lifecycle import attach_private_root_metadata

            await attach_private_root_metadata(session, rows)
            result: list[dict[str, Any]] = []
            for row in rows:
                root = getattr(row, "_private_agent_root_metadata", None)
                if isinstance(root, Mapping) and (
                    root.get("state") != "active"
                    or str(root.get("owner_user_id")) != str(actor)
                ):
                    continue
                if not self._session_can_read(row, actor, actor_role):
                    continue
                if row.project_id is not None:
                    if not await self._human_project_permission(
                        session,
                        project_id=row.project_id,
                        actor_id=actor,
                        actor_role=actor_role,
                        permission="read",
                    ):
                        continue
                if not is_creator_or_admin:
                    if row.project_id is None:
                        continue
                    grant = (
                        await session.execute(
                            select(AgentProjectGrant)
                            .where(
                                AgentProjectGrant.agent_id == aid,
                                AgentProjectGrant.project_id == row.project_id,
                                AgentProjectGrant.state == "active",
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    now = datetime.utcnow()
                    permissions = grant.permissions if grant is not None else None
                    if (
                        grant is None
                        or (grant.active_from is not None and grant.active_from > now)
                        or (grant.active_until is not None and grant.active_until < now)
                        or not isinstance(permissions, Mapping)
                        or permissions.get("read") is not True
                    ):
                        continue
                result.append(_agent_session_projection(row))
            return result
        finally:
            if own:
                await session.close()


async def pin_conversation_agent_turn(
    *,
    db_session: Any,
    session_id: Any,
    agent_id: Any | None = None,
    agent_revision_id: Any | None = None,
    actor_user_id: Any | None = None,
    actor_role: Any = None,
    config: Any | None = None,
    allow_legacy: bool = False,
    allow_pinned_revision: bool = False,
) -> AgentExecutionBindingSnapshot:
    """Typed module API for Run creation's transaction-local turn pin."""

    return await ConversationAgentService(config=config).pin_turn_binding(
        db_session=db_session,
        session_id=session_id,
        agent_id=agent_id,
        agent_revision_id=agent_revision_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        config=config,
        allow_legacy=allow_legacy,
        allow_pinned_revision=allow_pinned_revision,
    )


__all__ = [
    "AgentExecutionBindingSnapshot",
    "ConversationAgentBindingResult",
    "ConversationAgentAuthorizationError",
    "ConversationAgentConflictError",
    "ConversationAgentError",
    "ConversationAgentNotFoundError",
    "ConversationAgentService",
    "ConversationAgentValidationError",
    "pin_conversation_agent_turn",
]
