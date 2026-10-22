"""Owner-only provisioning for the private AoiDots Agent root.

This service composes ordinary ``Agent``, ``AgentRevision``,
``ConversationSession``, ``ConversationParticipant`` and the immutable normal
Agent binding.  It owns no provider, scheduler, memory ledger, or alternate
Agent type.  The four dialogue ledger models are consumed by the separate
dialogue service.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Mapping

logger = logging.getLogger(__name__)

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentExecutionBinding,
    AgentPrivateRoot,
    AgentRevision,
    ConversationParticipant,
    ConversationSession,
    User,
)
from .agent_identity_service import (
    AgentIdentityAuthorizationError,
    AgentIdentityConflictError,
    AgentIdentityNotFoundError,
    AgentIdentityService,
    AgentIdentityValidationError,
)


AOIDOTS_PURPOSE = "aoidots"
_PRIVATE_NAMESPACE = uuid.UUID("5a1a7ea5-1e3c-4e0c-9d0f-8e9f6c0e22b9")
_OWNER_PROVISION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_OWNER_PROVISION_LOCKS_GUARD = threading.Lock()
_ORDINARY_CHARACTER_ALIASES = {"project_management_assistant": "project_manager"}


def _stable_id(kind: str, owner_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_PRIVATE_NAMESPACE, f"{AOIDOTS_PURPOSE}:{kind}:{owner_id}")


def _owner_provision_lock(owner_id: uuid.UUID) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), str(owner_id))
    with _OWNER_PROVISION_LOCKS_GUARD:
        lock = _OWNER_PROVISION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _OWNER_PROVISION_LOCKS[key] = lock
        return lock


def _release_owner_provision_lock(owner_id: uuid.UUID, lock: asyncio.Lock) -> None:
    """Prune an idle per-loop lock so closed test loops cannot retain it."""

    key = (id(asyncio.get_running_loop()), str(owner_id))
    waiters = getattr(lock, "_waiters", None)
    with _OWNER_PROVISION_LOCKS_GUARD:
        if (
            _OWNER_PROVISION_LOCKS.get(key) is lock
            and not lock.locked()
            and not waiters
        ):
            _OWNER_PROVISION_LOCKS.pop(key, None)


def _revision_projection(row: AgentRevision) -> dict[str, Any]:
    """Project revision identity and routing metadata without prose."""

    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "version": int(row.version or 0),
        "display_name": row.display_name,
        "agent_team_id": row.agent_team_id,
        "execution_profile_id": row.execution_profile_id,
        "allowed_subagent_ids": list(row.allowed_subagent_ids or []),
        "capability_ceiling": list(row.capability_ceiling_json or []),
        "content_hash": row.content_hash,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _session_projection(
    row: ConversationSession,
    participants: list[ConversationParticipant] | None = None,
) -> dict[str, Any]:
    """Keep the root session response to ordinary identity metadata."""

    participant_projection = []
    for participant in list(
        participants if participants is not None else getattr(row, "participants", []) or []
    ):
        participant_projection.append(
            {
                "id": str(participant.id),
                "participant_type": participant.participant_type,
                "participant_id": participant.participant_id,
                "role": participant.role,
                "status": participant.status,
            }
        )
    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "character_name": row.character_name,
        "title": row.title or "",
        "is_active": bool(row.is_active),
        "is_group_chat": bool(row.is_group_chat),
        "project_id": str(row.project_id) if row.project_id else None,
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        "participants": participant_projection,
    }


def _config_value(config: Any | None, key: str) -> str:
    if isinstance(config, Mapping):
        value = config.get(key)
    else:
        getter = getattr(config, "get", None)
        value = getter(key) if callable(getter) else getattr(config, key, None)
    return str(value or "").strip()


async def _ordinary_character_name(session: Any, owner_id: uuid.UUID, config: Any | None) -> str:
    """Resolve the existing ordinary user/default Character label.

    A private Agent must not invent an AoiDots Character.  The same precedence
    used by request character resolution is applied to the durable owner
    preference and configured default; the final ``Assistant`` label keeps
    minimal test/legacy databases usable without adding a Character row.
    """

    existing = (
        await session.execute(
            select(ConversationSession.character_name)
            .where(
                ConversationSession.user_id == str(owner_id),
                ConversationSession.is_active.is_(True),
                ConversationSession.deleted_at.is_(None),
                ConversationSession.is_group_chat.is_(False),
            )
            .order_by(
                desc(ConversationSession.last_activity).nullslast(),
                desc(ConversationSession.session_start).nullslast(),
                desc(ConversationSession.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    user = await session.get(User, owner_id)
    preferred = str(getattr(user, "preferred_character", "") or "").strip() if user else ""
    selected = str(existing or "").strip() or preferred or _config_value(config, "default_character") or "Assistant"
    return _ORDINARY_CHARACTER_ALIASES.get(selected, selected)


class AgentPrivateProvisioningService:
    """Atomic, idempotent get-or-create for one user's private AoiDots root."""

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        identity_service: AgentIdentityService | None = None,
        config: Any | None = None,
    ) -> None:
        self._db_manager = db_manager
        self.config = config
        self.identity = identity_service or AgentIdentityService(db_manager, config=config)

    async def _open_session(self, session_factory: Any | None) -> tuple[Any, bool]:
        source = session_factory
        if source is None:
            manager = self._db_manager or get_database_manager()
            value = manager.get_session()
            owned = True
        elif hasattr(source, "get_session"):
            value = source.get_session()
            owned = True
        elif callable(source):
            value = source()
            owned = True
        else:
            # Accept an explicitly supplied AsyncSession for callers that
            # already own a transaction.  Such a session is never closed here.
            value = source
            owned = False
        if inspect.isawaitable(value):
            value = await value
        return value, owned

    @staticmethod
    def _config_for_identity(config: Any | None) -> Any | None:
        # The normal identity validator consumes the application config for
        # Team/Profile references.  Never merge request data into it here.
        return config

    async def _validate_existing_root(
        self,
        session: Any,
        root: AgentPrivateRoot,
        *,
        owner_id: uuid.UUID,
        config: Any | None,
    ) -> dict[str, Any]:
        if root.owner_user_id != owner_id:
            logger.warning(
                "Private AoiDots root owner mismatch: owner_id=%s, root_owner_id=%s, root_id=%s",
                owner_id,
                root.owner_user_id,
                root.id,
            )
            raise AgentIdentityAuthorizationError("private root owner mismatch")
        if root.purpose != AOIDOTS_PURPOSE:
            logger.warning(
                "Private AoiDots root purpose invalid: owner_id=%s, root_id=%s, purpose=%s",
                owner_id,
                root.id,
                root.purpose,
            )
            raise AgentIdentityConflictError("private root purpose is invalid")
        if root.state != "active":
            logger.warning(
                "Private AoiDots root not active: owner_id=%s, root_id=%s, state=%s",
                owner_id,
                root.id,
                root.state,
            )
            raise AgentIdentityConflictError("private AoiDots root is not active")

        agent = await session.get(Agent, root.agent_id)
        revision = None
        if agent is not None:
            revision = (
                await session.execute(
                    select(AgentRevision)
                    .where(AgentRevision.agent_id == agent.id)
                    .order_by(AgentRevision.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        conversation = (
            await session.execute(
                select(ConversationSession)
                .options(selectinload(ConversationSession.participants))
                .where(ConversationSession.id == root.session_id)
            )
        ).scalar_one_or_none()
        if agent is None or revision is None or conversation is None:
            missing: list[str] = []
            if agent is None:
                missing.append("agent")
            if revision is None:
                missing.append("revision")
            if conversation is None:
                missing.append("conversation")
            logger.warning(
                "Private AoiDots root target unavailable: owner_id=%s, root_id=%s, missing=%s",
                owner_id,
                root.id,
                missing,
            )
            raise AgentIdentityNotFoundError("private AoiDots root target is unavailable")
        self.identity._assert_revision_content_hash(revision)
        self.identity._assert_private_general_worker_route(revision, config=config)
        if str(agent.state or "") != "active" or agent.created_by != owner_id:
            logger.warning(
                "Private AoiDots Agent inactive or foreign: owner_id=%s, root_id=%s, agent_id=%s, state=%s, created_by=%s",
                owner_id,
                root.id,
                agent.id,
                agent.state,
                agent.created_by,
            )
            raise AgentIdentityConflictError("private AoiDots Agent is inactive or foreign")
        session_violations: list[str] = []
        if str(conversation.user_id) != str(owner_id):
            session_violations.append("owner")
        if conversation.project_id is not None:
            session_violations.append("project")
        if conversation.deleted_at is not None:
            session_violations.append("deleted")
        if bool(conversation.is_group_chat):
            session_violations.append("group")
        if conversation.agent_id != agent.id:
            session_violations.append("agent")
        if session_violations:
            logger.warning(
                "Private AoiDots root session binding invalid: owner_id=%s, root_id=%s, session_id=%s, agent_id=%s, violations=%s",
                owner_id,
                root.id,
                conversation.id,
                agent.id,
                session_violations,
            )
            raise AgentIdentityConflictError(
                f"private AoiDots root session binding is invalid: {','.join(session_violations)}"
            )
        owner_participant = next(
            (
                item
                for item in list(conversation.participants or [])
                if item.participant_type == "user"
                and str(item.participant_id) == str(owner_id)
                and item.role == "owner"
                and item.status == "joined"
            ),
            None,
        )
        if owner_participant is None:
            logger.warning(
                "Private AoiDots owner participant is missing: owner_id=%s, root_id=%s, session_id=%s",
                owner_id,
                root.id,
                conversation.id,
            )
            raise AgentIdentityConflictError("private AoiDots owner participant is missing")
        binding = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                    AgentExecutionBinding.agent_revision_id == revision.id,
                )
                .order_by(AgentExecutionBinding.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if binding is None:
            # A normal Agent can receive a new current revision.  Preserve
            # every historical binding and create one immutable snapshot for
            # that current revision so the next turn sees the new settings.
            # The Agent ID and root/session pointers remain unchanged.
            from .conversation_agent_service import ConversationAgentService

            binding = await ConversationAgentService(config=config)._persist_snapshot(
                session,
                row=conversation,
                agent=agent,
                revision=revision,
                actor_id=owner_id,
                config=config,
            )
        revision_data = _revision_projection(revision)
        logger.info(
            "Private AoiDots root validated successfully: owner_id=%s, root_id=%s, session_id=%s, agent_id=%s, revision_version=%s",
            owner_id,
            root.id,
            conversation.id,
            agent.id,
            revision.version,
        )
        return {
            "root": root.to_safe_dict(),
            "session": _session_projection(conversation),
            "agent": agent.to_safe_dict(),
            "revision": revision_data,
            "agent_revision": revision_data,
        }

    async def _ensure_once(
        self,
        session: Any,
        *,
        owner_id: uuid.UUID,
        config: Any | None,
    ) -> dict[str, Any]:
        await self.identity._assert_active_human_owner(session, owner_id)
        root = (
            await session.execute(
                select(AgentPrivateRoot)
                .where(
                    AgentPrivateRoot.owner_user_id == owner_id,
                    AgentPrivateRoot.purpose == AOIDOTS_PURPOSE,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if root is not None:
            logger.debug(
                "Validating existing private AoiDots root: owner_id=%s, root_id=%s",
                owner_id,
                root.id,
            )
            return await self._validate_existing_root(
                session,
                root,
                owner_id=owner_id,
                config=config,
            )

        agent_id = _stable_id("agent", owner_id)
        revision_id = _stable_id("revision", owner_id)
        logger.info(
            "Bootstrapping new private AoiDots root: owner_id=%s, agent_id=%s, revision_id=%s",
            owner_id,
            agent_id,
            revision_id,
        )
        agent, revision = await self.identity._create_private_agent_and_revision(
            session,
            owner_user_id=owner_id,
            purpose=AOIDOTS_PURPOSE,
            config=self._config_for_identity(config),
            agent_id=agent_id,
            revision_id=revision_id,
        )
        session_id = _stable_id("session", owner_id)
        character_name = await _ordinary_character_name(session, owner_id, config)
        conversation = ConversationSession(
            id=session_id,
            user_id=str(owner_id),
            character_name=character_name,
            title="AoiDots",
            is_active=True,
            message_count=0,
            context={},
            current_summary="",
            project_id=None,
            agent_id=agent.id,
            agent_binding_updated_at=datetime.utcnow(),
            is_group_chat=False,
            group_character_names=[],
            rp_settings={},
        )
        session.add(conversation)
        await session.flush()
        owner_participant = ConversationParticipant(
            session_id=conversation.id,
            participant_type="user",
            participant_id=str(owner_id),
            display_name="",
            role="owner",
            status="joined",
            auto_respond=False,
            participant_metadata={},
        )
        session.add(owner_participant)
        await session.flush()
        # Persist the same immutable ordinary Agent binding used by Chat
        # selection.  The root itself still stores only the Agent/session
        # pointers, so later turns can resolve the current Agent revision.
        from .conversation_agent_service import ConversationAgentService

        await ConversationAgentService(config=config)._persist_snapshot(
            session,
            row=conversation,
            agent=agent,
            revision=revision,
            actor_id=owner_id,
            config=config,
        )
        root = AgentPrivateRoot(
            id=_stable_id("root", owner_id),
            owner_user_id=owner_id,
            purpose=AOIDOTS_PURPOSE,
            agent_id=agent.id,
            session_id=conversation.id,
            state="active",
        )
        session.add(root)
        await session.flush()
        # Flush the participant before the projection so the returned object
        # is a complete ordinary session even with autoflush disabled.
        await session.flush()
        revision_data = _revision_projection(revision)
        logger.info(
            "Bootstrapped new private AoiDots root successfully: owner_id=%s, root_id=%s, session_id=%s, agent_id=%s, revision_id=%s",
            owner_id,
            root.id,
            conversation.id,
            agent.id,
            revision.id,
        )
        return {
            "root": root.to_safe_dict(),
            "session": _session_projection(conversation, [owner_participant]),
            "agent": agent.to_safe_dict(),
            "revision": revision_data,
            "agent_revision": revision_data,
        }

    async def _ensure_worker_once(
        self,
        session: Any,
        *,
        owner_id: uuid.UUID,
        config: Any | None,
    ) -> dict[str, Any]:
        """Provision the stable ordinary fallback worker when needed.

        Callers should invoke this only after the normal eligible-worker
        lookup is empty.  The worker has no ``AgentPrivateRoot`` row; its
        owner-scoped idempotency key is the complete durable identity.
        """

        await self.identity._assert_active_human_owner(session, owner_id)
        agent_key = f"private-worker:{owner_id}:general:v1"
        revision_key = f"{agent_key}:revision"
        agent_id = _stable_id("worker-agent", owner_id)
        revision_id = _stable_id("worker-revision", owner_id)
        agent, _initial_revision = await self.identity._create_private_agent_and_revision(
            session,
            owner_user_id=owner_id,
            purpose=AOIDOTS_PURPOSE,
            config=config,
            agent_id=agent_id,
            revision_id=revision_id,
            private_role="general_worker",
            agent_idempotency_key=agent_key,
            revision_idempotency_key=revision_key,
        )
        if agent.id == _stable_id("agent", owner_id):
            raise AgentIdentityConflictError("private worker Agent cannot be the AoiDots root Agent")
        current_revision = (
            await session.execute(
                select(AgentRevision)
                .where(AgentRevision.agent_id == agent.id)
                .order_by(AgentRevision.version.desc(), AgentRevision.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if current_revision is None:
            raise AgentIdentityNotFoundError("private worker Agent revision is unavailable")
        self.identity._assert_revision_content_hash(current_revision)
        self.identity._assert_private_general_worker_route(current_revision, config=config)
        revision_data = _revision_projection(current_revision)
        return {
            "agent": agent.to_safe_dict(),
            "worker_agent": agent.to_safe_dict(),
            "revision": revision_data,
            "agent_revision": revision_data,
            "worker_revision": revision_data,
        }

    async def _ensure_private_root_unlocked(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
    ) -> dict[str, Any]:
        """Atomically get-or-create the owner's projectless AoiDots root.

        ``owner_user_id`` is always a real human User UUID.  Replays return
        the existing active root, while a deleted/invalid root fails closed
        instead of being resurrected or silently rebound to another owner.
        """

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc

        effective_config = self.config if config is None else config
        for attempt in range(3):
            session, owned = await self._open_session(session_factory)
            try:
                if session.in_transaction():
                    result = await self._ensure_once(session, owner_id=owner_id, config=effective_config)
                else:
                    async with session.begin():
                        result = await self._ensure_once(session, owner_id=owner_id, config=effective_config)
                return result
            except IntegrityError as exc:
                if session.in_transaction():
                    await session.rollback()
                logger.warning(
                    "Private root bootstrap conflict: category=db_integrity_conflict, attempt=%s",
                    attempt + 1,
                )
                if attempt >= 2:
                    raise AgentIdentityConflictError("private AoiDots root creation conflicted") from exc
            except OperationalError:
                if session.in_transaction():
                    await session.rollback()
                logger.warning(
                    "Private root bootstrap retry: category=db_operational_error, attempt=%s",
                    attempt + 1,
                )
                if attempt >= 2:
                    raise
            finally:
                if owned:
                    await session.close()
        logger.warning(
            "Private root bootstrap failed: category=retry_exhausted, attempts=3"
        )
        raise AgentIdentityConflictError("private AoiDots root creation failed")

    async def ensure_private_root(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
    ) -> dict[str, Any]:
        """Serialize same-owner bootstrap across SQLite requests."""

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc
        lock = _owner_provision_lock(owner_id)
        try:
            async with lock:
                return await self._ensure_private_root_unlocked(
                    owner_id,
                    config=config,
                    session_factory=session_factory,
                )
        finally:
            _release_owner_provision_lock(owner_id, lock)

    async def _ensure_private_worker_unlocked(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
    ) -> dict[str, Any]:
        """Get-or-create one owner-scoped ordinary general worker Agent.

        Existing eligible workers are selected by the dialogue service before
        this method is called.  This method only supplies the stable private
        fallback and never creates a root pointer or a second Agent type.
        """

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc

        effective_config = self.config if config is None else config
        for attempt in range(3):
            session, owned = await self._open_session(session_factory)
            try:
                if session.in_transaction():
                    result = await self._ensure_worker_once(
                        session,
                        owner_id=owner_id,
                        config=effective_config,
                    )
                else:
                    async with session.begin():
                        result = await self._ensure_worker_once(
                            session,
                            owner_id=owner_id,
                            config=effective_config,
                        )
                return result
            except IntegrityError as exc:
                if session.in_transaction():
                    await session.rollback()
                if attempt >= 2:
                    raise AgentIdentityConflictError("private worker Agent creation conflicted") from exc
            except OperationalError:
                if session.in_transaction():
                    await session.rollback()
                if attempt >= 2:
                    raise
            finally:
                if owned:
                    await session.close()
        raise AgentIdentityConflictError("private worker Agent creation failed")

    async def ensure_private_worker(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
    ) -> dict[str, Any]:
        """Serialize same-owner worker fallback provisioning."""

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc
        lock = _owner_provision_lock(owner_id)
        try:
            async with lock:
                return await self._ensure_private_worker_unlocked(
                    owner_id,
                    config=config,
                    session_factory=session_factory,
                )
        finally:
            _release_owner_provision_lock(owner_id, lock)


__all__ = ["AOIDOTS_PURPOSE", "AgentPrivateProvisioningService"]
