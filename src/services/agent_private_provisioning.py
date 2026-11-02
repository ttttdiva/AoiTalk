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
from types import SimpleNamespace
from typing import Any, Mapping

logger = logging.getLogger(__name__)

from sqlalchemy import desc, func, select
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
    AOI_DEFAULT_AGENT_KIND,
    AgentIdentityAuthorizationError,
    AgentIdentityConflictError,
    AgentIdentityNotFoundError,
    AgentIdentityService,
    AgentIdentityValidationError,
    aoi_default_agent_idempotency_key,
)


AOIDOTS_PURPOSE = "aoidots"
AOIDOTS_DEFAULT_MAIN_ROUTE = {
    "provider": "codex-cli",
    # A ChatGPT-login Codex rejects the spark model with model_not_found.
    "model": "gpt-6-luna",
    "effort": "",
}
_PRIVATE_NAMESPACE = uuid.UUID("5a1a7ea5-1e3c-4e0c-9d0f-8e9f6c0e22b9")
_OWNER_PROVISION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_OWNER_PROVISION_LOCKS_GUARD = threading.Lock()
_ORDINARY_CHARACTER_ALIASES = {"project_management_assistant": "project_manager"}

# The normal Chat surface can retain deferred packs between turns.  Keep the
# logical groups in the P2 projection so a later pack load remains eligible;
# the concrete names are added from the actual configured registry below.
AOI_DEFAULT_CAPABILITY_CEILING = (
    "aoi_tools",
    "browser_qa",
    "command_execute",
    "docs_read",
    "docs_write",
    "media",
    "project_read",
    "project_write",
    "repo_map",
    "web_read",
    "workspace_read",
    "workspace_write",
)
AOI_DEFAULT_TOOL_IDS = (
    "chat_default",
    "aoi_tools",
    "browser",
    "delegate",
    "browser_qa",
    "command_execute",
    "docs_read",
    "docs_write",
    "media",
    "project_read",
    "project_write",
    "repo_map",
    "web_read",
    "workspace_read",
    "workspace_write",
)
AOI_DEFAULT_MISSION = (
    "ユーザーの依頼と会話の履歴を踏まえ、AoiTalk の利用可能な機能を使って、"
    "正確で役立つ回答を返す。"
)
AOI_DEFAULT_OPERATIONAL_INSTRUCTIONS = (
    "普通の Chat として、現在の会話の文脈とユーザーの依頼を優先して応答する。"
    "利用可能な tool は権限の範囲内だけで使い、実行結果を確認してから完了を伝える。"
)


def _optional_revision_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a P2 revision field without requiring the P2 model columns."""

    for candidate in (name, f"{name}_json"):
        if hasattr(row, candidate):
            return getattr(row, candidate, default)
    return default


def _set_optional_revision_value(row: Any, name: str, value: Any) -> bool:
    """Set a P2 revision field only when this worktree has that column."""

    for candidate in (name, f"{name}_json"):
        if hasattr(row, candidate):
            setattr(row, candidate, value)
            return True
    return False


def _runtime_default_chat_projection(
    config: Any | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(capabilities, tool_ids)`` from the real Chat registry.

    P4 provisioning has no provider client or project session at this point,
    so this captures the configured baseline catalog.  Request-local Chat
    filtering still applies the current feature, pack, ACL, and policy gates.
    A real Chat configuration must produce a concrete catalog; otherwise the
    caller falls back to legacy Chat instead of storing a misleading narrow
    projection.  ``config=None`` remains a minimal/test-only static path.
    """

    if config is None:
        return AOI_DEFAULT_CAPABILITY_CEILING, AOI_DEFAULT_TOOL_IDS
    try:
        from ..llm.generation_policy import DEFAULT_GENERATION_POLICY
        from ..llm.runtime_tool_registry import build_runtime_tool_registry
        from ..llm.tool_exposure import filter_tools_for_client
        from ..llm.tool_packs import ensure_load_tool_pack_tool
        from ..llm.tool_policy import agent_capability_ids_for_tool
        from .agent_capabilities import AGENT_CAPABILITY_CATALOG as AGENT_TEAM_CAPABILITY_CATALOG

        registry = build_runtime_tool_registry(config)
        client = SimpleNamespace(
            config=config,
            generation_policy=DEFAULT_GENERATION_POLICY,
            current_session_id="aoi-default-provisioning",
            _tool_registry=registry,
        )
        ensure_load_tool_pack_tool(registry, client)
        all_names = [
            str(name).strip()
            for name in registry.get_names()
            if str(name).strip()
        ]
        # Materialize one ordinary Chat exposure pass so the same feature and
        # deferred-pack gates are exercised while the baseline registry is
        # inspected.  The stored projection keeps all registered concrete
        # names; the next request-local filter decides which are visible now.
        filter_tools_for_client(client, all_names)
        names: list[str] = []
        capabilities: set[str] = set()
        for item in registry.get_all():
            name = str(getattr(item, "name", item) or "").strip()
            if not name:
                continue
            names.append(name)
            definition = registry.get(name)
            capabilities.update(
                capability
                for capability in agent_capability_ids_for_tool(name, definition)
                if capability in AGENT_TEAM_CAPABILITY_CATALOG
            )
        if not names:
            raise AgentIdentityConflictError("default Chat tool catalog is empty")
        owners = {
            str(getattr(registry.get(name), "owner", "") or "").strip()
            for name in names
            if str(getattr(registry.get(name), "owner", "") or "").strip()
        }
        # Aoi keeps ordinary Chat's whole, growing tool surface: a snapshot of
        # concrete names would hide tools added later and would be lost the
        # first time the revision is saved from the Operations tool picker.
        del owners
        return tuple(sorted(capabilities)), AOI_DEFAULT_TOOL_IDS
    except AgentIdentityConflictError:
        raise
    except Exception as exc:
        logger.debug(
            "Default Chat tool catalog could not be materialized; provisioning is unavailable",
            exc_info=True,
        )
        raise AgentIdentityConflictError(
            "default Chat tool catalog is unavailable"
        ) from exc


def _stable_id(kind: str, owner_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_PRIVATE_NAMESPACE, f"{AOIDOTS_PURPOSE}:{kind}:{owner_id}")


def aoidots_worker_session_context(
    context: Any,
    *,
    root_context: Any,
) -> dict[str, Any]:
    """Give a child Worker the Root's saved route or AoiDots default."""

    from .conversation_session_selection import (
        merge_session_llm_settings,
        read_session_llm_settings,
        session_main_route_override_for_binding,
    )

    route = session_main_route_override_for_binding(
        read_session_llm_settings(root_context)
    ) or AOIDOTS_DEFAULT_MAIN_ROUTE
    return merge_session_llm_settings(context, {"main_route": route})


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


def _revision_projection(
    row: AgentRevision,
    *,
    identity: AgentIdentityService | None = None,
) -> dict[str, Any]:
    """Project revision identity and routing metadata without prose."""

    payload = {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "version": int(row.version or 0),
        "display_name": row.display_name,
        "capability_ceiling": list(row.capability_ceiling_json or []),
        # P2 is expand-only; keep these keys nullable for pre-migration rows.
        "model_overrides": _optional_revision_value(row, "model_overrides"),
        "tool_ids": _optional_revision_value(row, "tool_ids"),
        "delegate_policy": _optional_revision_value(row, "delegate_policy"),
        "content_hash": row.content_hash,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    legacy = {
        "agent_team_id": row.agent_team_id,
        "execution_profile_id": row.execution_profile_id,
        "allowed_subagent_ids": list(row.allowed_subagent_ids or []),
    }
    if any(value not in (None, "", []) for value in legacy.values()):
        payload["legacy"] = legacy
    payload["effective_model_routes"] = (
        identity._effective_model_routes(row.to_safe_dict()) if identity is not None else {}
    )
    return payload


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

    async def _repair_private_root_binding_cutoff(
        self,
        session: Any,
        root: AgentPrivateRoot,
        conversation: ConversationSession,
        agent: Agent,
        revision: AgentRevision,
        *,
        owner_id: uuid.UUID,
    ) -> bool:
        """Repair only the known private-root cutoff-before-first-binding defect.

        The cutoff is an identity boundary, not a clock to refresh on every
        open. Advance it only when the owner-scoped private root has no
        binding at or before the cutoff and the earliest binding for the
        current Agent revision is a unique, intact owner-created snapshot.
        """

        def _skip(reason_code: str) -> bool:
            logger.warning(
                "Private AoiDots root binding cutoff repair skipped: reason=%s, owner_id=%s, root_id=%s",
                reason_code,
                owner_id,
                root.id,
            )
            return False

        if (
            root.owner_user_id != owner_id
            or root.purpose != AOIDOTS_PURPOSE
            or root.state != "active"
            or root.id != _stable_id("root", owner_id)
            or root.agent_id != _stable_id("agent", owner_id)
            or root.session_id != _stable_id("session", owner_id)
            or conversation.id != root.session_id
            or str(conversation.user_id) != str(owner_id)
            or conversation.project_id is not None
            or conversation.deleted_at is not None
            or bool(conversation.is_group_chat)
            or conversation.agent_id != root.agent_id
            or agent.id != root.agent_id
            or agent.created_by != owner_id
            or revision.agent_id != agent.id
            or conversation.agent_binding_updated_at is None
        ):
            return False

        bindings = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                )
                .order_by(AgentExecutionBinding.created_at, AgentExecutionBinding.id)
            )
        ).scalars().all()
        if not bindings or any(binding.created_at is None for binding in bindings):
            return _skip("binding_timestamp_unavailable")
        if any(
            str(binding.created_by or "") != str(owner_id)
            for binding in bindings
        ):
            return _skip("binding_owner_mismatch")

        cutoff = conversation.agent_binding_updated_at
        if any(binding.created_at <= cutoff for binding in bindings):
            return False

        current_revision_bindings = [
            binding for binding in bindings
            if str(binding.agent_revision_id) == str(revision.id)
        ]
        if not current_revision_bindings:
            return _skip("current_revision_binding_missing")

        candidate_created_at = current_revision_bindings[0].created_at
        candidates_at_cutoff = [
            binding for binding in bindings
            if binding.created_at == candidate_created_at
        ]
        # The catalog resolves equal timestamps by descending binding ID. A
        # tie could select an older revision at the repaired cutoff, so fail
        # closed even if only one tied row has the current revision.
        if len(candidates_at_cutoff) != 1:
            return _skip("current_revision_binding_created_at_tie")

        from .conversation_agent_service import (
            AgentExecutionBindingSnapshot,
            ConversationAgentError,
        )

        binding = current_revision_bindings[0]
        try:
            snapshot = AgentExecutionBindingSnapshot.from_row(binding)
        except ConversationAgentError:
            return _skip("current_revision_snapshot_invalid")

        if str(binding.created_by or "") != str(owner_id):
            return _skip("current_revision_binding_owner_mismatch")
        if (
            str(binding.session_id) != str(conversation.id)
            or str(binding.agent_id) != str(agent.id)
            or str(binding.agent_revision_id) != str(revision.id)
            or snapshot.session_id != str(conversation.id)
            or snapshot.agent_id != str(agent.id)
            or snapshot.agent_revision_id != str(revision.id)
            or snapshot.revision_version != int(revision.version or 0)
            or snapshot.revision_content_hash != str(revision.content_hash or "")
        ):
            return _skip("current_revision_binding_identity_mismatch")

        bound_revision = await session.get(AgentRevision, binding.agent_revision_id)
        if (
            bound_revision is None
            or bound_revision.agent_id != agent.id
            or bound_revision.id != revision.id
            or str(bound_revision.content_hash or "") != snapshot.revision_content_hash
        ):
            return _skip("current_revision_record_mismatch")

        conversation.agent_binding_updated_at = candidate_created_at
        await session.flush()
        logger.info(
            "Private AoiDots root binding cutoff repaired from earliest canonical snapshot: owner_id=%s, root_id=%s, session_id=%s",
            owner_id,
            root.id,
            conversation.id,
        )
        return True

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
                .with_for_update()
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
        from .conversation_session_selection import (
            merge_session_llm_settings,
            read_session_llm_settings,
            session_main_route_override_for_binding,
        )

        if session_main_route_override_for_binding(
            read_session_llm_settings(conversation.context)
        ) is None:
            conversation.context = merge_session_llm_settings(
                conversation.context,
                {"main_route": AOIDOTS_DEFAULT_MAIN_ROUTE},
            )
        await self._repair_private_root_binding_cutoff(
            session,
            root,
            conversation,
            agent,
            revision,
            owner_id=owner_id,
        )
        binding = await self._ensure_current_private_root_binding(
            session,
            conversation=conversation,
            agent=agent,
            revision=revision,
            owner_id=owner_id,
            config=config,
        )
        revision_data = _revision_projection(revision, identity=self.identity)
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

    async def _ensure_current_private_root_binding(
        self,
        session: Any,
        *,
        conversation: ConversationSession,
        agent: Agent,
        revision: AgentRevision,
        owner_id: uuid.UUID,
        config: Any | None,
    ) -> AgentExecutionBinding | None:
        """Point the Root cutoff at the latest revision and saved route.

        Run snapshots remain immutable history.  This pointer selects the
        current session revision for catalog/bootstrap reads, so a v3 snapshot
        at the cutoff must not shadow a newly published v4 revision.
        """

        from .conversation_agent_service import (
            AgentExecutionBindingSnapshot,
            ConversationAgentService,
        )
        from .conversation_session_selection import (
            read_session_llm_settings,
            session_main_route_override_for_binding,
        )

        expected = AgentExecutionBindingSnapshot.from_revision(
            session_id=conversation.id,
            agent=agent,
            revision=revision,
            config=config,
            created_by=owner_id,
            main_route_override=session_main_route_override_for_binding(
                read_session_llm_settings(conversation.context)
            ),
        )
        expected_route = expected.route_snapshot
        cutoff = conversation.agent_binding_updated_at
        current = None
        if cutoff is not None:
            current = (
                await session.execute(
                    select(AgentExecutionBinding)
                    .where(
                        AgentExecutionBinding.session_id == conversation.id,
                        AgentExecutionBinding.agent_id == agent.id,
                        AgentExecutionBinding.created_at <= cutoff,
                    )
                    .order_by(
                        AgentExecutionBinding.created_at.desc(),
                        AgentExecutionBinding.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        if current is not None:
            try:
                current_snapshot = AgentExecutionBindingSnapshot.from_row(current)
            except Exception:
                return current
            if (
                str(current.agent_revision_id) == str(revision.id)
                and int(current.revision_version or 0) == int(revision.version or 0)
                and str(current.revision_content_hash or "")
                == str(revision.content_hash or "")
                and current_snapshot.route_snapshot == expected_route
            ):
                return current

        history = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                )
                .order_by(
                    AgentExecutionBinding.created_at,
                    AgentExecutionBinding.id,
                )
            )
        ).scalars().all()
        if not history or any(binding.created_at is None for binding in history):
            return current
        for historical in history:
            if str(historical.created_by or "") != str(owner_id):
                return current
            try:
                historical_snapshot = AgentExecutionBindingSnapshot.from_row(
                    historical
                )
            except Exception:
                return current
            historical_revision = await session.get(
                AgentRevision,
                historical.agent_revision_id,
            )
            try:
                if historical_revision is not None:
                    self.identity._assert_revision_content_hash(historical_revision)
            except AgentIdentityConflictError:
                return current
            if (
                historical_revision is None
                or historical_revision.agent_id != agent.id
                or historical_snapshot.agent_revision_id
                != str(historical_revision.id)
                or historical_snapshot.revision_version
                != int(historical_revision.version or 0)
                or historical_snapshot.revision_content_hash
                != str(historical_revision.content_hash or "")
            ):
                return current

        # Reuse an intact snapshot for the exact current route where possible.
        # Otherwise create it through the same immutable snapshot writer used
        # by ordinary ConversationAgent binding and turn admission.
        candidates = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                    AgentExecutionBinding.agent_revision_id == revision.id,
                )
                .order_by(
                    AgentExecutionBinding.created_at.desc(),
                    AgentExecutionBinding.id.desc(),
                )
            )
        ).scalars().all()
        binding = None
        for candidate in candidates:
            try:
                snapshot = AgentExecutionBindingSnapshot.from_row(candidate)
            except Exception:
                continue
            if (
                int(candidate.revision_version or 0) == int(revision.version or 0)
                and str(candidate.revision_content_hash or "")
                == str(revision.content_hash or "")
                and snapshot.route_snapshot == expected_route
                and str(candidate.created_by or "") == str(owner_id)
            ):
                binding = candidate
                break
        if binding is None:
            snapshot = await ConversationAgentService(config=config)._persist_snapshot(
                session,
                row=conversation,
                agent=agent,
                revision=revision,
                actor_id=owner_id,
                config=config,
            )
            binding = await session.get(
                AgentExecutionBinding,
                uuid.UUID(str(snapshot.binding_id)),
            )
        if binding is None or binding.created_at is None:
            return current
        same_time_history = [
            item for item in history if item.created_at == binding.created_at
        ]
        already_in_history = any(item.id == binding.id for item in same_time_history)
        if len(same_time_history) > (1 if already_in_history else 0):
            return current

        conversation.agent_binding_updated_at = binding.created_at
        await session.flush()
        selected = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                    AgentExecutionBinding.created_at
                    <= conversation.agent_binding_updated_at,
                )
                .order_by(
                    AgentExecutionBinding.created_at.desc(),
                    AgentExecutionBinding.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if selected is None or selected.id != binding.id:
            return current
        return binding

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
        from .conversation_session_selection import merge_session_llm_settings

        conversation = ConversationSession(
            id=session_id,
            user_id=str(owner_id),
            character_name=character_name,
            title="AoiDots",
            is_active=True,
            message_count=0,
            context=merge_session_llm_settings(
                {},
                {"main_route": AOIDOTS_DEFAULT_MAIN_ROUTE},
            ),
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
        initial_binding = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == conversation.id,
                    AgentExecutionBinding.agent_id == agent.id,
                    AgentExecutionBinding.agent_revision_id == revision.id,
                )
                .order_by(
                    desc(AgentExecutionBinding.created_at),
                    desc(AgentExecutionBinding.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if initial_binding is None or initial_binding.created_at is None:
            raise AgentIdentityConflictError("private AoiDots Agent binding was not persisted")
        if (
            conversation.agent_binding_updated_at is None
            or initial_binding.created_at > conversation.agent_binding_updated_at
        ):
            # Use the timestamp read back from the persisted binding, not a
            # second application clock sample. This remains correct if the DB
            # timestamp is later than the value set during session creation.
            conversation.agent_binding_updated_at = initial_binding.created_at
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
        revision_data = _revision_projection(revision, identity=self.identity)
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
        current_revision = await self._ensure_private_worker_workspace_read_revision(
            session,
            agent=agent,
            revision=current_revision,
            owner_id=owner_id,
        )
        self.identity._assert_private_general_worker_route(current_revision, config=config)
        revision_data = _revision_projection(current_revision, identity=self.identity)
        return {
            "agent": agent.to_safe_dict(),
            "worker_agent": agent.to_safe_dict(),
            "revision": revision_data,
            "agent_revision": revision_data,
            "worker_revision": revision_data,
        }

    async def _ensure_private_worker_workspace_read_revision(
        self,
        session: Any,
        *,
        agent: Agent,
        revision: AgentRevision,
        owner_id: uuid.UUID,
    ) -> AgentRevision:
        """Give the stable private Worker the scope capability its runner requires.

        Older private fallback Workers predate ``workspace_read``. Preserve
        their revision history and every existing field while publishing one
        canonical next revision for the AoiDots session-scoped read workspace.
        """

        capabilities = [
            str(item).strip()
            for item in (revision.capability_ceiling_json or [])
            if str(item).strip()
        ]
        if "workspace_read" in capabilities:
            return revision

        revision_key = (
            f"private-worker:{owner_id}:general:v1:workspace-read:"
            f"{int(revision.version or 0)}:{str(revision.content_hash or '')[:16]}"
        )
        existing = (
            await session.execute(
                select(AgentRevision)
                .where(
                    AgentRevision.agent_id == agent.id,
                    AgentRevision.idempotency_key == revision_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            self.identity._assert_revision_content_hash(existing)
            if "workspace_read" not in {
                str(item).strip()
                for item in (existing.capability_ceiling_json or [])
                if str(item).strip()
            }:
                raise AgentIdentityConflictError(
                    "private Worker read-scope revision is invalid"
                )
            return existing

        capabilities.append("workspace_read")
        refreshed = AgentRevision(
            id=uuid.uuid4(),
            agent_id=agent.id,
            version=int(revision.version or 0) + 1,
            display_name=revision.display_name,
            mission=revision.mission,
            responsibility_summary=revision.responsibility_summary,
            operational_instructions=revision.operational_instructions,
            # P5 retired topology pins are retained only on historical rows.
            agent_team_id=None,
            execution_profile_id=None,
            allowed_subagent_ids=None,
            capability_ceiling_json=capabilities,
            model_overrides=dict(revision.model_overrides or {}),
            tool_ids=list(revision.tool_ids or capabilities),
            delegate_policy=dict(revision.delegate_policy or {"agents": [], "workers": []}),
            wake_policy_json=dict(revision.wake_policy_json or {}),
            budget_policy_json=dict(revision.budget_policy_json or {}),
            concurrency_policy_json=dict(revision.concurrency_policy_json or {}),
            content_hash="",
            idempotency_key=revision_key,
            created_by=owner_id,
        )
        refreshed.content_hash = self.identity._revision_content_hash(refreshed)
        session.add(refreshed)
        await session.flush()
        return refreshed

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

    async def _ensure_default_chat_once(
        self,
        session: Any,
        *,
        owner_id: uuid.UUID,
        config: Any | None,
        capability_ceiling: list[str] | tuple[str, ...] | None,
        tool_ids: list[str] | tuple[str, ...] | None,
    ) -> dict[str, Any]:
        """Provision the owner's ordinary Chat Agent without a transcript root."""

        await self.identity._assert_active_human_owner(session, owner_id)
        agent_key = aoi_default_agent_idempotency_key(owner_id)
        agent_id = _stable_id(AOI_DEFAULT_AGENT_KIND, owner_id)
        revision_id = _stable_id(f"{AOI_DEFAULT_AGENT_KIND}:revision", owner_id)
        existing_agent = (
            await session.execute(
                select(Agent)
                .where(
                    Agent.idempotency_key == agent_key,
                    Agent.created_by == owner_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_agent is None:
            # Only the first creation needs the expensive concrete registry
            # projection. Replays validate and reuse the immutable persisted
            # revision below, so a Chat turn does not rebuild the catalog.
            derived_capabilities, derived_tool_ids = _runtime_default_chat_projection(config)
            capabilities = list(capability_ceiling or derived_capabilities)
            visible_tools = list(tool_ids or derived_tool_ids)
        else:
            capabilities = None
            visible_tools = None
        agent, _initial_revision = await self.identity._create_private_agent_and_revision(
            session,
            owner_user_id=owner_id,
            purpose=AOIDOTS_PURPOSE,
            config=config,
            agent_id=agent_id,
            revision_id=revision_id,
            private_role=AOI_DEFAULT_AGENT_KIND,
            agent_idempotency_key=agent_key,
            revision_idempotency_key=f"{agent_key}:revision",
            display_name="Aoi",
            mission=AOI_DEFAULT_MISSION,
            responsibility_summary="ユーザー本人の普通の Chat を担う既定 Agent。",
            operational_instructions=AOI_DEFAULT_OPERATIONAL_INSTRUCTIONS,
            capability_ceiling=capabilities,
            tool_ids=visible_tools,
            model_overrides={},
            delegate_policy={"agents": "any_owned", "workers": []},
        )
        if agent.id != agent_id:
            raise AgentIdentityConflictError("default Chat Agent identity is not owner-stable")
        current_revision = (
            await session.execute(
                select(AgentRevision)
                .where(AgentRevision.agent_id == agent.id)
                .order_by(AgentRevision.version.desc(), AgentRevision.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if current_revision is None:
            raise AgentIdentityNotFoundError("default Chat Agent revision is unavailable")
        self.identity._assert_revision_content_hash(current_revision)

        # P2 keeps these columns nullable while rolling out. Upgrade an older
        # Aoi revision by appending one immutable revision after the columns are
        # available; never patch the original row. Replaying this bootstrap is
        # idempotent because the latest revision will then have all three
        # values populated.
        p2_fields_available = all(
            hasattr(AgentRevision, field)
            for field in ("model_overrides", "tool_ids", "delegate_policy")
        )
        if p2_fields_available:
            from .agent_revision_v2 import (
                revision_delegate_policy,
                revision_model_overrides,
                revision_tool_ids,
            )

            if any(
                value is None
                for value in (
                    revision_model_overrides(current_revision),
                    revision_tool_ids(current_revision),
                    revision_delegate_policy(current_revision),
                )
            ):
                stored_tool_ids = revision_tool_ids(current_revision)
                if config is None and stored_tool_ids is not None:
                    current_chat_tool_ids = stored_tool_ids
                else:
                    _current_capabilities, current_chat_tool_ids = (
                        _runtime_default_chat_projection(config)
                    )
                latest_version = (
                    await session.execute(
                        select(func.max(AgentRevision.version)).where(
                            AgentRevision.agent_id == agent.id
                        )
                    )
                ).scalar_one()
                next_version = int(latest_version or 0) + 1
                repaired_revision = AgentRevision(
                    id=_stable_id(
                        f"{AOI_DEFAULT_AGENT_KIND}:p2-revision:{next_version}",
                        owner_id,
                    ),
                    agent_id=agent.id,
                    version=next_version,
                    display_name=current_revision.display_name,
                    mission=current_revision.mission,
                    responsibility_summary=current_revision.responsibility_summary,
                    operational_instructions=current_revision.operational_instructions,
                    # Keep old topology only on the historical source row;
                    # repaired/new revisions are role-routed v2 rows.
                    agent_team_id=None,
                    execution_profile_id=None,
                    allowed_subagent_ids=None,
                    capability_ceiling_json=list(
                        current_revision.capability_ceiling_json or []
                    ),
                    wake_policy_json=dict(current_revision.wake_policy_json or {}),
                    budget_policy_json=dict(current_revision.budget_policy_json or {}),
                    concurrency_policy_json=dict(
                        current_revision.concurrency_policy_json or {}
                    ),
                    content_hash="0" * 64,
                    idempotency_key=f"{agent_key}:revision:p2-defaults:v1",
                    created_by=owner_id,
                )
                _set_optional_revision_value(repaired_revision, "model_overrides", {})
                _set_optional_revision_value(
                    repaired_revision,
                    "tool_ids",
                    list(current_chat_tool_ids),
                )
                _set_optional_revision_value(
                    repaired_revision,
                    "delegate_policy",
                    {"agents": "any_owned", "workers": []},
                )
                repaired_revision.content_hash = self.identity._revision_content_hash(
                    repaired_revision
                )
                session.add(repaired_revision)
                await session.flush()
                current_revision = repaired_revision

        self.identity._assert_private_general_worker_route(current_revision, config=config)
        revision_data = _revision_projection(current_revision, identity=self.identity)
        return {
            "agent": agent.to_safe_dict() | {"is_default_chat_agent": True},
            "revision": revision_data,
            "agent_revision": revision_data,
            "is_default_chat_agent": True,
        }

    async def _ensure_default_chat_unlocked(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
        capability_ceiling: list[str] | tuple[str, ...] | None = None,
        tool_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc

        effective_config = self.config if config is None else config
        for attempt in range(3):
            session, owned = await self._open_session(session_factory)
            try:
                kwargs = {
                    "owner_id": owner_id,
                    "config": effective_config,
                    "capability_ceiling": capability_ceiling,
                    "tool_ids": tool_ids,
                }
                if session.in_transaction():
                    result = await self._ensure_default_chat_once(session, **kwargs)
                else:
                    async with session.begin():
                        result = await self._ensure_default_chat_once(session, **kwargs)
                return result
            except IntegrityError as exc:
                if session.in_transaction():
                    await session.rollback()
                logger.debug(
                    "Default Chat Agent bootstrap conflict: category=db_integrity_conflict, attempt=%s",
                    attempt + 1,
                )
                if attempt >= 2:
                    raise AgentIdentityConflictError("default Chat Agent creation conflicted") from exc
            except OperationalError:
                if session.in_transaction():
                    await session.rollback()
                logger.debug(
                    "Default Chat Agent bootstrap retry: category=db_operational_error, attempt=%s",
                    attempt + 1,
                )
                if attempt >= 2:
                    raise
            finally:
                if owned:
                    await session.close()
        raise AgentIdentityConflictError("default Chat Agent creation failed")

    async def ensure_default_chat_agent(
        self,
        owner_user_id: Any,
        config: Any | None = None,
        session_factory: Any | None = None,
        *,
        capability_ceiling: list[str] | tuple[str, ...] | None = None,
        tool_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Get or create the owner's stable default Chat Agent."""

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc
        lock = _owner_provision_lock(owner_id)
        try:
            async with lock:
                return await self._ensure_default_chat_unlocked(
                    owner_id,
                    config=config,
                    session_factory=session_factory,
                    capability_ceiling=capability_ceiling,
                    tool_ids=tool_ids,
                )
        finally:
            _release_owner_provision_lock(owner_id, lock)

    async def get_default_chat_agent(
        self,
        owner_user_id: Any,
        *,
        config: Any | None = None,
        session_factory: Any | None = None,
    ) -> dict[str, Any] | None:
        """Read an existing default Chat Agent without creating it."""

        try:
            owner_id = uuid.UUID(str(owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentIdentityValidationError("invalid owner_user_id") from exc
        session, owned = await self._open_session(session_factory)
        try:
            await self.identity._assert_active_human_owner(session, owner_id)
            agent = (
                await session.execute(
                    select(Agent)
                    .where(
                        Agent.idempotency_key == aoi_default_agent_idempotency_key(owner_id),
                        Agent.created_by == owner_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if agent is None:
                return None
            if str(agent.state or "") != "active":
                raise AgentIdentityConflictError("default Chat Agent is inactive")
            revision = (
                await session.execute(
                    select(AgentRevision)
                    .where(AgentRevision.agent_id == agent.id)
                    .order_by(AgentRevision.version.desc(), AgentRevision.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if revision is None:
                raise AgentIdentityNotFoundError("default Chat Agent revision is unavailable")
            self.identity._assert_revision_content_hash(revision)
            self.identity._assert_private_general_worker_route(
                revision,
                config=self.config if config is None else config,
            )
            revision_data = _revision_projection(revision, identity=self.identity)
            return {
                "agent": agent.to_safe_dict() | {"is_default_chat_agent": True},
                "revision": revision_data,
                "agent_revision": revision_data,
                "is_default_chat_agent": True,
            }
        finally:
            if owned:
                await session.close()


__all__ = [
    "AOIDOTS_PURPOSE",
    "AOI_DEFAULT_AGENT_KIND",
    "AOI_DEFAULT_CAPABILITY_CEILING",
    "AOI_DEFAULT_TOOL_IDS",
    "AOI_DEFAULT_MISSION",
    "AOI_DEFAULT_OPERATIONAL_INSTRUCTIONS",
    "AOIDOTS_DEFAULT_MAIN_ROUTE",
    "AgentPrivateProvisioningService",
    "aoidots_worker_session_context",
]
