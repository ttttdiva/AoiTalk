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
from uuid import UUID
from types import MappingProxyType

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentExecutionBinding,
    AgentProjectGrant,
    AgentRevision,
    AgentRun,
    ConversationSession,
)
from ..memory.project_repository import ProjectRepository
from ..services.agent_model_routing import (
    AgentRoutingPins,
    build_agent_routing_snapshot,
)


_BUSY_RUN_STATES = frozenset({"queued", "running", "awaiting_approval", "uncertain"})


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

    def __post_init__(self) -> None:
        # Freeze the outer routing map as well as the scalar/tuple fields. The
        # persisted row is immutable; callers must not be able to mutate the
        # object handed to run creation between ACL validation and dispatch.
        object.__setattr__(
            self,
            "route_snapshot",
            _freeze_json(deepcopy(dict(self.route_snapshot or {}))),
        )

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
        pins = AgentRoutingPins.from_revision(revision)
        route_snapshot = build_agent_routing_snapshot(
            config,
            pins,
            main_route_override=main_route_override,
        )
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
            agent_team_id=_text(revision.agent_team_id, limit=100),
            execution_profile_id=_text(revision.execution_profile_id, limit=100),
            allowed_subagent_ids=tuple(_safe_list(revision.allowed_subagent_ids)),
            capability_ceiling=tuple(_safe_list(revision.capability_ceiling_json)),
            route_snapshot=route_snapshot,
            revision_content_hash=_text(revision.content_hash, limit=64),
            created_by=str(created_by) if created_by else None,
            created_at=created_at.isoformat() if created_at else None,
        )

    def to_revision_dict(self) -> dict[str, Any]:
        """Return the server-owned revision projection for run creation."""

        return {
            "id": self.agent_revision_id,
            "agent_id": self.agent_id,
            "version": self.revision_version,
            "display_name": self.display_name,
            "mission": self.mission,
            "responsibility_summary": self.responsibility_summary,
            "operational_instructions": self.operational_instructions,
            "agent_team_id": self.agent_team_id,
            "execution_profile_id": self.execution_profile_id,
            "allowed_subagent_ids": list(self.allowed_subagent_ids),
            "capability_ceiling": list(self.capability_ceiling),
            "content_hash": self.revision_content_hash,
        }

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
    ) -> tuple[Agent, AgentRevision]:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise ConversationAgentNotFoundError(code="agent_not_found")
        if str(agent.state or "") != "active":
            raise ConversationAgentAuthorizationError("Agent is not active", code="agent_inactive")
        revision = await self._latest_revision(session, agent_id)
        if revision is None:
            raise ConversationAgentAuthorizationError(
                "Agent has no usable revision", code="agent_revision_missing"
            )
        if (
            int(revision.version or 0) <= 0
            or len(str(revision.content_hash or "")) != 64
            or not str(revision.agent_team_id or "").strip()
            or not str(revision.execution_profile_id or "").strip()
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
    ) -> AgentExecutionBindingSnapshot:
        """Create one immutable turn snapshot inside the caller's transaction.

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
            )
        else:
            revision = await self._latest_revision(db_session, canonical_agent)
        if (
            revision is None
            or str(revision.agent_id) != str(canonical_agent)
            or int(revision.version or 0) <= 0
            or len(str(revision.content_hash or "")) != 64
            or not str(revision.agent_team_id or "").strip()
            or not str(revision.execution_profile_id or "").strip()
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
        """Return only active Agents usable by this human in this scope."""

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
            agents = (
                await session.execute(
                    select(Agent)
                    .where(Agent.state == "active")
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
        """Return only readable sessions currently bound to one Agent."""

        aid = _uuid(agent_id, "agent_id")
        actor = self._actor_uuid(actor_user_id)
        own = db_session is None
        session = db_session or await self._session()
        try:
            if await session.get(Agent, aid) is None:
                raise ConversationAgentNotFoundError(code="agent_not_found")
            safe_limit = min(max(int(limit or 50), 1), 200)
            safe_offset = max(int(offset or 0), 0)
            rows = (
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
            ).scalars().all()
            from .agent_root_lifecycle import attach_private_root_metadata

            await attach_private_root_metadata(session, rows)
            result: list[dict[str, Any]] = []
            for row in rows:
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
                try:
                    await self._agent_for_selection(
                        session,
                        agent_id=aid,
                        actor_id=actor,
                        actor_role=actor_role,
                        project_id=row.project_id,
                        require_project_read=True,
                    )
                except ConversationAgentError:
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
