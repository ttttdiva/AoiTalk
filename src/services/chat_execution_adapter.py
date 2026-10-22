"""Request-scoped execution adapter for Agent-backed Chat turns.

The adapter is deliberately small: conversation identity is resolved from the
durable session/run boundary, while provider construction and work-lease
bookkeeping remain transport concerns.  Character and Persona state never
participate in this identity.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID

from .agent_model_routing import (
    AgentModelRoute,
    AgentModelRouteError,
    resolve_agent_role_route,
)
from .agent_dialogue_workspace import (
    AgentDialogueWorkspace,
    AgentDialogueWorkspaceDenied,
    bind_agent_dialogue_workspace,
    get_current_agent_dialogue_workspace,
    reset_agent_dialogue_workspace,
)
from ..llm.tool_policy import (
    reset_current_agent_tool_scope,
    set_current_agent_tool_scope,
)
from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentDialogue,
    AgentPrivateRoot,
    AgentRun,
    AgentWorkItem,
    ConversationMessage,
    ConversationSession,
)
from .turn_context import (
    get_turn_context,
    reset_turn_context,
    set_turn_context,
)

if TYPE_CHECKING:
    from .conversation_agent_service import AgentExecutionBindingSnapshot


class AgentDialogueBudgetUnsupported(RuntimeError):
    """The selected provider cannot enforce a configured Dialogue budget."""

    code = "agent_dialogue_budget_unsupported"

    def __init__(self, message: str = "provider cannot enforce the configured AgentDialogue budget") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class InitialAoiDotsRootProof:
    """Server-derived private Root identity for a Human turn without a Dialogue."""

    root_id: str
    owner_user_id: str
    session_id: str
    agent_id: str
    agent_revision_id: str
    run_id: str
    work_item_id: str

    def marker(self) -> dict[str, Any]:
        return {
            "scope_kind": "aoidots",
            "root_initial": True,
            "root_id": self.root_id,
            "agent_dialogue_id": self.root_id,
            "root_owner_user_id": self.owner_user_id,
            "root_session_id": self.session_id,
            "root_agent_id": self.agent_id,
            "root_agent_revision_id": self.agent_revision_id,
            "agent_run_id": self.run_id,
            "work_item_id": self.work_item_id,
        }


def _initial_aoidots_root_proof(
    *, root: Any, conversation: Any, run: Any, work: Any, human: Any,
    binding: Any, session_id: str, run_id: str | None,
) -> InitialAoiDotsRootProof | None:
    if any(row is None for row in (root, conversation, run, work, human, binding)):
        return None
    current_run_id = _clean(_read(run, "id"))
    owner = _clean(_read(run, "user_id"))
    agent = _clean(getattr(binding, "agent_id", None))
    revision = _clean(getattr(binding, "agent_revision_id", None))
    work_id = _clean(_read(run, "work_item_id"))
    if not all((current_run_id, owner, agent, revision, work_id)):
        return None
    if run_id and _clean(run_id) != current_run_id:
        return None
    if (
        str(root.purpose or "") != "aoidots" or str(root.state or "") != "active"
        or not _clean(root.id) or _clean(root.owner_user_id) != owner
        or _clean(root.session_id) != session_id or _clean(root.agent_id) != agent
        or _clean(conversation.id) != session_id or _clean(conversation.user_id) != owner
        or _clean(conversation.agent_id) != agent or conversation.project_id is not None
        or bool(getattr(conversation, "is_group_chat", False))
        or getattr(conversation, "deleted_at", None) is not None
        or _clean(_read(run, "session_id")) != session_id
        or _clean(_read(run, "agent_id")) != agent
        or _clean(_read(run, "agent_revision_id")) != revision
        or _read(run, "project_id") is not None
        or _clean(_read(run, "status")) not in {"queued", "running", "awaiting_approval"}
        or _clean(work.id) != work_id or _clean(work.active_agent_run_id) != current_run_id
        or _clean(work.conversation_session_id) != session_id
        or getattr(work, "project_id", None) is not None
        or _clean(work.assigned_agent_id) != agent or _clean(work.agent_revision_id) != revision
        or _clean(work.source_type) != "chat_turn" or _clean(work.source_id) != current_run_id
        or _clean(work.source_revision) != revision or _clean(work.intent_key) != "interactive_chat"
        or _clean(work.execution_adapter) != "interactive_chat"
        or _clean(human.id) != _clean(_read(run, "trigger_message_id"))
        or _clean(human.session_id) != session_id or human.role != "user"
        or human.sender_type not in {"user", "human"} or _clean(human.sender_id) != owner
    ):
        return None
    pending = (
        work.state == "pending" and int(work.attempt_count or 0) == 0
        and work.started_at is None and work.lease_owner in (None, "")
        and work.lease_token in (None, "") and work.lease_expires_at is None
    )
    expiry = work.lease_expires_at
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    leased = (
        work.state in {"claimed", "running"} and bool(_clean(work.lease_owner))
        and bool(_clean(work.lease_token)) and isinstance(expiry, datetime)
        and expiry > datetime.now(timezone.utc)
    )
    if not (pending or leased):
        return None
    return InitialAoiDotsRootProof(
        _clean(root.id), owner, session_id, agent, revision, current_run_id, work_id,
    )


def _initial_aoidots_root_applicable(
    *, root: Any, conversation: Any, run: Any, binding: Any, session_id: str,
) -> bool:
    """Return whether the server has identified this as the private Root lane."""

    if any(row is None for row in (root, conversation, run, binding)):
        return False
    owner = _clean(_read(run, "user_id"))
    agent = _clean(getattr(binding, "agent_id", None))
    revision = _clean(getattr(binding, "agent_revision_id", None))
    return bool(
        owner
        and agent
        and revision
        and str(getattr(root, "purpose", "") or "") == "aoidots"
        and str(getattr(root, "state", "") or "") == "active"
        and _clean(getattr(root, "owner_user_id", None)) == owner
        and _clean(getattr(root, "session_id", None)) == session_id
        and _clean(getattr(root, "agent_id", None)) == agent
        and _clean(getattr(conversation, "id", None)) == session_id
        and _clean(getattr(conversation, "user_id", None)) == owner
        and _clean(getattr(conversation, "agent_id", None)) == agent
        and getattr(conversation, "project_id", None) is None
        and getattr(conversation, "deleted_at", None) is None
        and not bool(getattr(conversation, "is_group_chat", False))
        and _clean(_read(run, "session_id")) == session_id
        and _clean(_read(run, "user_id")) == owner
        and _clean(_read(run, "agent_id")) == agent
        and _clean(_read(run, "agent_revision_id")) == revision
        and _read(run, "project_id") is None
        and _clean(_read(run, "status")).casefold()
        in {"queued", "running", "awaiting_approval"}
        and bool(_clean(_read(run, "work_item_id")))
    )


async def refresh_initial_aoidots_root_proof(
    execution: "ChatAgentExecution | None",
    *,
    session_id: str | None,
    run_id: str | None,
    dialogue_role: str | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    client: Any | None = None,
) -> InitialAoiDotsRootProof | None:
    """Refresh the initial Root proof after a direct WebSocket Human write.

    Direct WebSocket dispatch resolves the immutable Agent binding before the
    callback owns persistence.  The first resolve therefore legitimately has
    no ``trigger_message_id``/Human row to prove.  Once the callback has saved
    the canonical Human and bound the Run, read every authority row again from
    the database.  Only the existing binding object, exact Run id, and a
    projectless non-Dialogue execution may receive the refreshed proof.

    This helper never creates or updates durable rows, never re-resolves the
    route, and returns ``None`` on any incomplete or mismatched boundary so
    the existing authority guard keeps the turn denied.
    """

    if execution is None:
        return None
    binding = getattr(execution, "binding", None)
    if binding is None:
        return None
    if getattr(binding, "_initial_aoidots_root_applicable", False) is not True:
        return None
    normalized_role = _clean(dialogue_role).casefold()
    if normalized_role:
        # AgentDialogue root/worker turns already have their own typed marker.
        # The initial AoiDots proof is only for an ordinary projectless Human
        # turn and must never replace a Dialogue scope.
        return None
    sid = _clean(session_id)
    rid = _clean(run_id)
    if not sid or not rid or _clean(getattr(binding, "session_id", None)) != sid:
        return None
    try:
        from .agent_run_service import get_current_agent_run_id

        ambient_run_id = _clean(get_current_agent_run_id())
    except Exception:
        ambient_run_id = ""
    if ambient_run_id and ambient_run_id != rid:
        return None
    try:
        session_uuid = UUID(sid)
        run_uuid = UUID(rid)
        agent_uuid = UUID(_clean(getattr(binding, "agent_id", None)))
    except (TypeError, ValueError, AttributeError):
        return None

    for marker in (
        getattr(execution, "root_scope_marker", None),
        getattr(binding, "_root_scope_marker", None),
    ):
        if not isinstance(marker, Mapping):
            continue
        scope_kind = _clean(marker.get("scope_kind") or marker.get("dialogue_kind")).casefold()
        if not scope_kind and marker:
            return None
        if scope_kind and scope_kind != "aoidots":
            return None
        if scope_kind == "aoidots":
            if marker.get("root_initial") is not True:
                return None
            continue
        if marker.get("dialogue_role") or marker.get("agent_dialogue_id"):
            return None

    own = db_session is None
    session = db_session or await _session_db(db_manager)
    try:
        # Do not reuse the callback's pre-persistence Run DTO.  The direct WS
        # path binds trigger_message_id after the Human row is committed, and
        # Work admission can also change the persisted Work/lease shape.
        fresh_run = await session.get(AgentRun, run_uuid)
        if fresh_run is None:
            return None
        work_id = _clean(_read(fresh_run, "work_item_id"))
        human_id = _clean(_read(fresh_run, "trigger_message_id"))
        if not work_id or not human_id:
            return None
        work = await session.get(AgentWorkItem, UUID(work_id))
        human = await session.get(ConversationMessage, UUID(human_id))
        conversation = await session.get(ConversationSession, session_uuid)
        from sqlalchemy import select

        root_result = await session.execute(
            select(AgentPrivateRoot)
            .where(
                AgentPrivateRoot.session_id == session_uuid,
                AgentPrivateRoot.agent_id == agent_uuid,
                AgentPrivateRoot.purpose == "aoidots",
                AgentPrivateRoot.state == "active",
            )
            .limit(1)
        )
        root = root_result.scalars().first()
        proof = _initial_aoidots_root_proof(
            root=root,
            conversation=conversation,
            run=fresh_run,
            work=work,
            human=human,
            binding=binding,
            session_id=sid,
            run_id=rid,
        )
        if proof is None:
            return None

        if client is not None:
            client_binding = getattr(client, "_agent_execution_binding", binding)
            if client_binding is not binding:
                return None
        marker = proof.marker()
        # AgentExecutionBindingSnapshot and ChatAgentExecution are frozen
        # dataclasses, so these private request-local attributes are attached
        # deliberately with object.__setattr__.  The same binding object is
        # already held by TurnContext and the isolated provider client.
        object.__setattr__(binding, "_initial_aoidots_root_proof", proof)
        object.__setattr__(binding, "_root_scope_marker", dict(marker))
        object.__setattr__(execution, "root_scope_marker", dict(marker))
        if client is not None:
            client._agent_root_scope_marker = dict(marker)
        return proof
    except Exception:
        # A refresh is a best-effort post-persistence read.  Any unavailable,
        # stale, or malformed row must preserve fail-closed tool admission.
        return None
    finally:
        if own:
            await session.close()


def _clear_initial_aoidots_root_snapshot(
    execution: "ChatAgentExecution",
    *,
    client: Any | None = None,
) -> None:
    """Remove an incomplete request-local initial Root snapshot."""

    binding = getattr(execution, "binding", None)
    if binding is not None:
        for attribute in ("_initial_aoidots_root_proof", "_root_scope_marker"):
            try:
                object.__delattr__(binding, attribute)
            except AttributeError:
                pass
    try:
        object.__setattr__(execution, "root_scope_marker", None)
    except Exception:
        pass
    if client is not None and (
        binding is None
        or getattr(client, "_agent_execution_binding", binding) is binding
    ):
        try:
            client._agent_root_scope_marker = None
        except Exception:
            pass


async def bind_initial_aoidots_human_run(
    execution: "ChatAgentExecution | None",
    *,
    session_id: str | None,
    run_id: str | None,
    message_id: str | None,
    content: str | None,
    sender_user_id: str | None,
    client_message_id: str | None = None,
    dialogue_role: str | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    client: Any | None = None,
) -> bool:
    """Atomically bind a canonical Human row to a direct-WS AgentRun.

    The fallback WebSocket run is created before ``TerminalMode`` persists its
    Human row.  This narrow transaction is the server-owned handoff between
    those two durable records.  It only accepts the exact authenticated
    projectless Agent run and Human content supplied by the current callback;
    an existing different trigger, owner, sender, or client id fails closed.
    """

    if execution is None:
        return False
    binding = getattr(execution, "binding", None)
    if getattr(binding, "_initial_aoidots_root_applicable", False) is not True:
        return False
    normalized_role = _clean(dialogue_role).casefold()
    sid = _clean(session_id)
    rid = _clean(run_id)
    mid = _clean(message_id)
    sender = _clean(sender_user_id)
    if (
        binding is None
        or normalized_role
        or not sid
        or not rid
        or not mid
        or not sender
        or _clean(getattr(binding, "session_id", None)) != sid
    ):
        return False
    try:
        from .agent_run_service import get_current_agent_run_id

        ambient_run_id = _clean(get_current_agent_run_id())
    except Exception:
        ambient_run_id = ""
    if ambient_run_id and ambient_run_id != rid:
        return False
    try:
        session_uuid = UUID(sid)
        run_uuid = UUID(rid)
        message_uuid = UUID(mid)
        UUID(_clean(getattr(binding, "agent_id", None)))
        UUID(_clean(getattr(binding, "agent_revision_id", None)))
    except (TypeError, ValueError, AttributeError):
        return False

    for marker in (
        getattr(execution, "root_scope_marker", None),
        getattr(binding, "_root_scope_marker", None),
    ):
        if not isinstance(marker, Mapping):
            continue
        scope_kind = _clean(marker.get("scope_kind") or marker.get("dialogue_kind")).casefold()
        if not scope_kind and marker:
            return False
        if scope_kind and scope_kind != "aoidots":
            return False
        if scope_kind == "aoidots":
            if marker.get("root_initial") is not True:
                return False
            continue
        if marker.get("dialogue_role") or marker.get("agent_dialogue_id"):
            return False
    if client is not None and getattr(client, "_agent_execution_binding", binding) is not binding:
        return False

    own = db_session is None
    session = db_session or await _session_db(db_manager)
    try:
        from sqlalchemy import select

        run_result = await session.execute(
            select(AgentRun)
            .where(AgentRun.id == run_uuid)
            .with_for_update()
        )
        scalars = run_result.scalars()
        one = getattr(scalars, "one_or_none", None)
        run = one() if callable(one) else scalars.first()
        if run is None:
            _clear_initial_aoidots_root_snapshot(execution, client=client)
            return False

        expected_client = _clean(client_message_id)
        run_client = _clean(_read(run, "client_message_id"))
        if (
            _clean(_read(run, "id")) != rid
            or _clean(_read(run, "session_id")) != sid
            or _clean(_read(run, "user_id")) != sender
            or _clean(_read(run, "run_type")).casefold() != "chat_turn"
            or _read(run, "project_id") is not None
            or _clean(_read(run, "agent_id"))
            != _clean(getattr(binding, "agent_id", None))
            or _clean(_read(run, "agent_revision_id"))
            != _clean(getattr(binding, "agent_revision_id", None))
            or not _clean(_read(run, "work_item_id"))
            or _clean(_read(run, "status")).casefold()
            not in {"queued", "running", "awaiting_approval"}
            or (expected_client and run_client != expected_client)
        ):
            _clear_initial_aoidots_root_snapshot(execution, client=client)
            return False

        human = await session.get(ConversationMessage, message_uuid)
        if human is None:
            _clear_initial_aoidots_root_snapshot(execution, client=client)
            return False
        human_metadata = _read(human, "message_metadata") or _read(human, "metadata") or {}
        human_client = _clean(_read(human, "client_message_id"))
        if not human_client and isinstance(human_metadata, Mapping):
            human_client = _clean(human_metadata.get("client_message_id"))
        if (
            _clean(_read(human, "id")) != mid
            or _clean(_read(human, "session_id")) != sid
            or _clean(_read(human, "role")).casefold() != "user"
            or _clean(_read(human, "sender_type")).casefold() not in {"user", "human"}
            or _clean(_read(human, "sender_id")) != sender
            or str(_read(human, "content") or "") != str(content or "")
            or (expected_client and human_client != expected_client)
            or ((run_client or human_client) and run_client != human_client)
        ):
            _clear_initial_aoidots_root_snapshot(execution, client=client)
            return False

        trigger = _clean(_read(run, "trigger_message_id"))
        if trigger and trigger != mid:
            _clear_initial_aoidots_root_snapshot(execution, client=client)
            return False
        if not trigger:
            run.trigger_message_id = message_uuid
            run.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
        return True
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        _clear_initial_aoidots_root_snapshot(execution, client=client)
        return False
    finally:
        if own:
            await session.close()


@dataclass(frozen=True)
class ChatAgentExecution:
    """Immutable identity and routing snapshot for one Chat generation."""

    binding: "AgentExecutionBindingSnapshot"
    route: AgentModelRoute
    system_prompt: str
    workspace: AgentDialogueWorkspace | None = None
    root_scope_marker: Mapping[str, Any] | None = None
    dialogue_budget: Mapping[str, Any] | None = None

    @property
    def agent_id(self) -> str:
        return self.binding.agent_id

    @property
    def agent_revision_id(self) -> str:
        return self.binding.agent_revision_id

    def manifest(self) -> dict[str, Any]:
        pins = self.binding.routing_pins()
        manifest = {
            "schema_version": 1,
            "source": "conversation_agent",
            "agent_id": self.binding.agent_id,
            "agent_revision_id": self.binding.agent_revision_id,
            "agent_revision_version": self.binding.revision_version,
            "team_id": self.binding.agent_team_id,
            "execution_profile_id": self.binding.execution_profile_id,
            "capabilities": list(self.binding.capability_ceiling),
            "route_snapshot": dict(pins.route_snapshot or {}),
        }
        if self.workspace is not None:
            # Keep the workspace identity bounded and explicit.  The adapter
            # never places provider handles or unrestricted prompt data here.
            manifest["workspace"] = {
                "resource_key": self.workspace.resource_key,
                "project_id": self.workspace.project_id,
                "readonly": self.workspace.readonly,
                "provider_state_key": self.workspace.provider_state_key,
            }
        if self.root_scope_marker is not None:
            manifest["root_scope"] = dict(self.root_scope_marker)
        return manifest

    def metadata(self) -> dict[str, Any]:
        """Bounded audit metadata; prose stays in the request-local prompt."""

        return {
            "binding_id": self.binding.binding_id,
            "snapshot_hash": self.binding.snapshot_hash,
            "agent_id": self.binding.agent_id,
            "agent_revision_id": self.binding.agent_revision_id,
            "agent_revision_version": self.binding.revision_version,
            "agent_team_id": self.binding.agent_team_id,
            "execution_profile_id": self.binding.execution_profile_id,
            "allowed_subagent_ids": list(self.binding.allowed_subagent_ids),
            "capability_ceiling": list(self.binding.capability_ceiling),
            "route": self.route.provenance(),
            **(
                {
                    "workspace": {
                        "resource_key": self.workspace.resource_key,
                        "project_id": self.workspace.project_id,
                        "readonly": self.workspace.readonly,
                        "provider_state_key": self.workspace.provider_state_key,
                    }
                }
                if self.workspace is not None
                else {}
            ),
            **(
                {"root_scope": dict(self.root_scope_marker)}
                if self.root_scope_marker is not None
                else {}
            ),
            **(
                {"dialogue_budget": dict(self.dialogue_budget)}
                if self.dialogue_budget is not None
                else {}
            ),
        }


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _clean(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (str, UUID, int)):
        return str(value).strip()
    return ""


def _system_prompt(binding: "AgentExecutionBindingSnapshot") -> str:
    parts = [
        "あなたはAoiTalk Operations Agentです。Character/Personaとして振る舞わず、"
        "このAgentRevisionに記載された職務だけを実行してください。",
    ]
    for label, value in (
        ("Mission", binding.mission),
        ("Responsibility", binding.responsibility_summary),
        ("Operational instructions", binding.operational_instructions),
    ):
        text = _clean(value)
        if text:
            parts.append(f"{label}:\n{text[:30_000]}")
    return "\n\n".join(parts)


async def _session_db(db_manager: Any | None = None) -> Any:
    manager = db_manager or get_database_manager()
    value = manager.get_session()
    return await value if inspect.isawaitable(value) else value


async def resolve_canonical_session_agent_id(
    *,
    session_id: str | None,
    config: Any | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
) -> str | None:
    """Read only the canonical session Agent identity for expected-ID checks.

    This helper deliberately does not construct a revision snapshot or route.
    The durable run service owns the transaction-local pin; callers use this
    read only when an offline client supplied ``expected_agent_id``.
    """

    _ = config
    sid = _clean(session_id)
    if not sid:
        return None
    try:
        UUID(sid)
    except (TypeError, ValueError, AttributeError):
        # Lightweight legacy fakes use symbolic session IDs and provide no
        # canonical DB row.  Preserve their human-only compatibility path.
        return None
    own = db_session is None
    session = db_session or await _session_db(db_manager)
    try:
        row = await session.get(ConversationSession, sid)
        if row is None or getattr(row, "deleted_at", None) is not None:
            return None
        agent_id = getattr(row, "agent_id", None)
        return _clean(agent_id) or None
    finally:
        if own:
            await session.close()


_ROUTE_EVIDENCE_FIELDS = frozenset(
    {"provider", "model", "effort", "reasoning_effort", "target_type"}
)


def _manifest_has_route_fields(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(key in value and value.get(key) not in (None, "") for key in _ROUTE_EVIDENCE_FIELDS):
        return True
    return any(_manifest_has_route_fields(item) for item in value.values())


def _route_snapshot_matches(actual: Any, evidence: Any) -> bool:
    """Compare manifest route evidence without treating it as authority."""

    if not isinstance(actual, Mapping) or not isinstance(evidence, Mapping):
        return actual == evidence
    actual_roles = actual.get("roles") if isinstance(actual.get("roles"), Mapping) else actual
    evidence_roles = evidence.get("roles") if isinstance(evidence.get("roles"), Mapping) else evidence
    if not isinstance(actual_roles, Mapping) or not isinstance(evidence_roles, Mapping):
        return False
    for key, expected in evidence_roles.items():
        if key not in actual_roles:
            return False
        observed = actual_roles[key]
        if isinstance(expected, Mapping):
            if not _route_snapshot_matches(observed, expected):
                return False
        elif observed != expected:
            return False
    return True


async def resolve_chat_agent_execution(
    *,
    session_id: str | None,
    run: Mapping[str, Any] | Any | None = None,
    config: Any | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    workspace: AgentDialogueWorkspace | None = None,
    run_id: str | None = None,
    dialogue_role: str | None = None,
) -> ChatAgentExecution | None:
    """Resolve a canonical session Agent and exact run revision.

    A typed persisted run is authoritative.  A request payload never supplies
    the identity, and a run-less call returns no execution snapshot because
    the durable run service must pin the immutable binding transactionally.
    """

    if workspace is not None and not isinstance(workspace, AgentDialogueWorkspace):
        raise TypeError("workspace must be an AgentDialogueWorkspace")
    sid = _clean(session_id)
    if not sid:
        return None
    if workspace is not None and workspace.session_id != sid:
        raise AgentDialogueWorkspaceDenied(
            "Agent workspace session does not match the execution session"
        )
    if workspace is not None:
        workspace.assert_provider_entry()
        if workspace.run_id is not None and run_id in (None, ""):
            raise AgentDialogueWorkspaceDenied(
                "Agent execution requires an explicit run_id for the workspace"
            )
        if run_id is not None and workspace.run_id != str(run_id):
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace run does not match the execution run"
            )
    run_agent_id = _clean(_read(run, "agent_id"))
    run_revision_id = _clean(_read(run, "agent_revision_id"))
    dialogue_budget: Mapping[str, Any] | None = None
    dialogue_marker: Mapping[str, Any] | None = None
    # A durable human run has no typed Agent identity.  Do not query the
    # session binding in that case: a Character compatibility turn must remain
    # independent from whatever Agent is selected for a later turn.
    if run is not None and not run_agent_id:
        return None
    # A run-less lookup cannot prove the immutable transaction-local binding.
    # The durable AgentRun service pins it during run creation; execution
    # reconstruction must start from that persisted exact identity.
    if run is None:
        return None
    own = db_session is None
    session = db_session or await _session_db(db_manager)
    try:
        if run_agent_id and not run_revision_id:
            raise ValueError("typed Chat Agent run has no exact AgentRevision")
        if run_agent_id and run_revision_id:
            from .conversation_agent_service import ConversationAgentService

            run_metadata = _read(run, "run_metadata") or _read(run, "metadata")
            binding_metadata = (
                run_metadata.get("agent_binding")
                if isinstance(run_metadata, Mapping)
                else None
            )
            dialogue_marker = (
                run_metadata.get("agent_dialogue")
                if isinstance(run_metadata, Mapping)
                else None
            )
            raw_budget = (
                dialogue_marker.get("budget")
                if isinstance(dialogue_marker, Mapping)
                else None
            )
            if isinstance(raw_budget, Mapping):
                safe_budget: dict[str, Any] = {}
                for key in (
                    "max_turns",
                    "turn_count",
                    "max_attempts",
                    "attempt_count",
                    "max_tokens",
                    "total_tokens",
                    "remaining_tokens",
                    "provider_max_output_tokens",
                    "provider_max_tool_rounds",
                    "max_cost_micros",
                    "cost_micros",
                    "reserved_tokens",
                    "reserved_cost_micros",
                    "provider_reserved_cost_micros",
                    "usage_state",
                ):
                    value = raw_budget.get(key)
                    if key == "usage_state":
                        if value in {"known", "unknown", "cost_unknown", "token_unknown"}:
                            safe_budget[key] = value
                    elif value is not None:
                        try:
                            safe_budget[key] = max(0, int(value))
                        except (TypeError, ValueError, OverflowError):
                            continue
                raw_rate_card = raw_budget.get("server_rate_card")
                if isinstance(raw_rate_card, Mapping):
                    safe_rate_card: dict[str, str] = {}
                    for rate_key in (
                        "provider",
                        "model",
                        "input_per_1k",
                        "output_per_1k",
                        "cached_per_1k",
                        "currency",
                    ):
                        rate_value = raw_rate_card.get(rate_key)
                        if rate_value not in (None, ""):
                            safe_rate_card[rate_key] = str(rate_value)[:160]
                    if (
                        "input_per_1k" in safe_rate_card
                        and "output_per_1k" in safe_rate_card
                    ):
                        safe_budget["server_rate_card"] = safe_rate_card
                dialogue_budget = safe_budget or None
            binding_id = (
                binding_metadata.get("binding_id")
                if isinstance(binding_metadata, Mapping)
                else None
            )
            snapshot_hash = (
                binding_metadata.get("snapshot_hash")
                if isinstance(binding_metadata, Mapping)
                else None
            )
            if not binding_id:
                raise ValueError("typed AgentRun is missing its immutable binding_id")
            if not snapshot_hash:
                raise ValueError("typed AgentRun is missing its immutable snapshot_hash")
            binding = await ConversationAgentService(
                db_manager=db_manager,
                config=config,
            ).resolve_execution_binding(
                session_id=sid,
                agent_id=run_agent_id,
                agent_revision_id=run_revision_id,
                binding_id=binding_id,
                db_session=session,
            )
            actual_snapshot_hash = _clean(getattr(binding, "snapshot_hash", None))
            if actual_snapshot_hash != _clean(snapshot_hash):
                raise ValueError("typed AgentRun binding snapshot_hash mismatch")
            current_agent = await session.get(Agent, run_agent_id)
            if current_agent is None or str(getattr(current_agent, "state", "")) != "active":
                raise ValueError("conversation Agent is no longer active")
            persisted_manifest = _read(run, "resolved_execution_manifest") or _read(
                run, "execution_manifest"
            )
            if isinstance(persisted_manifest, Mapping):
                manifest_route = persisted_manifest.get("route_snapshot")
                if isinstance(manifest_route, Mapping) and _manifest_has_route_fields(
                    manifest_route
                ):
                    if not _route_snapshot_matches(
                        binding.routing_pins().route_snapshot,
                        manifest_route,
                    ):
                        raise ValueError("typed AgentRun route snapshot mismatch")
        if binding is None:
            return None
        pins = binding.routing_pins()
        route = None
        route_errors: list[str] = []
        normalized_dialogue_role = _clean(dialogue_role).casefold()
        if not normalized_dialogue_role and isinstance(dialogue_marker, Mapping):
            normalized_dialogue_role = _clean(
                dialogue_marker.get("role") or dialogue_marker.get("dialogue_role")
            ).casefold()
        if normalized_dialogue_role == "worker":
            # AgentDialogue worker turns are admitted/readied against the
            # pinned ``general_worker`` role.  Keep that role first so an
            # available operator route cannot silently change the worker's
            # provider/model capability surface.  The remaining roles are
            # compatibility fallbacks for historical snapshots that did not
            # carry a general_worker role.
            route_roles = (
                "general_worker",
                "writer",
                "researcher",
                "explorer",
                "reviewer",
                "planner",
                "data_worker",
                "code_worker",
                "operator",
            )
        else:
            route_roles = (
                "operator",
                "general_worker",
                "researcher",
                "explorer",
                "reviewer",
                "planner",
                "writer",
                "data_worker",
                "code_worker",
            )
        for role in route_roles:
            try:
                route = resolve_agent_role_route(config, pins, role)
                break
            except AgentModelRouteError as exc:
                route_errors.append(exc.code)
        if route is None:
            raise AgentModelRouteError(
                route_errors[-1] if route_errors else "agent_route_role_unavailable"
            )
        selected_workspace = workspace or get_current_agent_dialogue_workspace()
        if selected_workspace is not None:
            if selected_workspace.agent_id != _clean(getattr(binding, "agent_id", None)):
                raise AgentDialogueWorkspaceDenied(
                    "Agent workspace identity does not match the resolved binding"
                )
            if selected_workspace.provider_id.casefold() != str(route.provider).casefold():
                raise AgentDialogueWorkspaceDenied(
                    "Agent workspace provider does not match the resolved route"
                )
            if selected_workspace.model_id != str(route.model):
                raise AgentDialogueWorkspaceDenied(
                    "Agent workspace model does not match the resolved route"
                )
        root_scope_marker: Mapping[str, Any] | None = None
        try:
            from sqlalchemy import select

            root_dialogue_requested = normalized_dialogue_role == "root"
            if root_dialogue_requested:
                marker_dialogue_id = _clean(
                    dialogue_marker.get("dialogue_id")
                    or dialogue_marker.get("agent_dialogue_id")
                    if isinstance(dialogue_marker, Mapping)
                    else ""
                )
                private_root = (
                    await session.execute(
                        select(AgentPrivateRoot)
                        .where(
                            AgentPrivateRoot.session_id == UUID(sid),
                            AgentPrivateRoot.purpose == "aoidots",
                        )
                        .limit(1)
                    )
                ).scalars().first()
                root_dialogue = (
                    await session.get(AgentDialogue, UUID(marker_dialogue_id))
                    if marker_dialogue_id
                    else None
                )
                root_session = await session.get(
                    ConversationSession,
                    UUID(sid),
                )
                work_item_id = _clean(_read(run, "work_item_id"))
                root_work = (
                    await session.get(AgentWorkItem, UUID(work_item_id))
                    if work_item_id
                    else None
                )
                run_status = _clean(_read(run, "status")).casefold()
                run_user_id = _clean(_read(run, "user_id"))
                root_work_state = (
                    str(getattr(root_work, "state", "") or "").casefold()
                    if root_work is not None
                    else ""
                )
                work_lease_expires_at = (
                    getattr(root_work, "lease_expires_at", None)
                    if root_work is not None
                    else None
                )
                pending_work_shape = (
                    root_work_state == "pending"
                    and int(getattr(root_work, "attempt_count", 0) or 0) == 0
                    and getattr(root_work, "started_at", None) is None
                    and getattr(root_work, "lease_owner", None) in (None, "")
                    and getattr(root_work, "lease_token", None) in (None, "")
                    and getattr(root_work, "lease_expires_at", None) is None
                )
                leased_work_shape = (
                    root_work_state in {"claimed", "running"}
                    and str(getattr(root_work, "lease_owner", "") or "").strip()
                    and str(getattr(root_work, "lease_token", "") or "").strip()
                    and work_lease_expires_at is not None
                    and work_lease_expires_at > datetime.utcnow()
                )
                if (
                    root_dialogue is not None
                    and private_root is not None
                    and str(getattr(private_root, "purpose", "") or "")
                    == "aoidots"
                    and str(getattr(private_root, "state", "") or "")
                    == "active"
                    and _clean(private_root.session_id) == sid
                    and _clean(private_root.agent_id)
                    == _clean(getattr(binding, "agent_id", None))
                    and _clean(private_root.owner_user_id) == run_user_id
                    and _clean(root_dialogue.root_session_id) == sid
                    and _clean(root_dialogue.root_agent_id)
                    == _clean(getattr(binding, "agent_id", None))
                    and _clean(root_dialogue.root_agent_revision_id)
                    == _clean(getattr(binding, "agent_revision_id", None))
                    and str(root_dialogue.state or "").casefold()
                    in {"queued", "running", "waiting"}
                    and root_session is not None
                    and _clean(root_session.id) == sid
                    and _clean(root_session.user_id) == run_user_id
                    and _clean(root_session.agent_id)
                    == _clean(getattr(binding, "agent_id", None))
                    and getattr(root_session, "project_id", None) is None
                    and _clean(root_dialogue.owner_user_id) == run_user_id
                    and run_status in {"queued", "running", "awaiting_approval"}
                    and _clean(_read(run, "session_id")) == sid
                    and _clean(_read(run, "agent_id"))
                    == _clean(getattr(binding, "agent_id", None))
                    and _clean(_read(run, "agent_revision_id"))
                    == _clean(getattr(binding, "agent_revision_id", None))
                    and _clean(_read(run, "project_id")) == ""
                    and root_work is not None
                    and (pending_work_shape or leased_work_shape)
                    and _clean(root_work.active_agent_run_id)
                    == _clean(_read(run, "id") or run_id)
                    and _clean(root_work.conversation_session_id) == sid
                    and _clean(root_work.assigned_agent_id)
                    == _clean(getattr(binding, "agent_id", None))
                    and _clean(root_work.agent_revision_id)
                    == _clean(getattr(binding, "agent_revision_id", None))
                    and str(getattr(root_work, "source_type", "") or "")
                    == "chat_turn"
                    and _clean(root_work.source_id)
                    == _clean(_read(run, "id") or run_id)
                    and _clean(root_work.source_revision)
                    == _clean(getattr(binding, "agent_revision_id", None))
                    and str(getattr(root_work, "intent_key", "") or "")
                    == "interactive_chat"
                    and str(getattr(root_work, "execution_adapter", "") or "")
                    == "interactive_chat"
                ):
                    root_scope_marker = {
                        "scope_kind": "agent_dialogue",
                        "agent_dialogue_id": str(root_dialogue.id),
                        "dialogue_role": "root",
                        "root_session_id": str(root_dialogue.root_session_id),
                        "root_agent_id": str(root_dialogue.root_agent_id),
                        "root_agent_revision_id": str(
                            root_dialogue.root_agent_revision_id
                        ),
                        "agent_run_id": _clean(
                            _read(run, "id") or run_id
                        ),
                        "work_item_id": _clean(_read(run, "work_item_id")),
                    }
            elif not root_dialogue_requested:
                root = (
                    await session.execute(
                        select(AgentPrivateRoot)
                        .where(
                            AgentPrivateRoot.session_id == UUID(sid),
                            AgentPrivateRoot.agent_id == UUID(_clean(getattr(binding, "agent_id", None))),
                            AgentPrivateRoot.purpose == "aoidots",
                            AgentPrivateRoot.state == "active",
                        )
                        .limit(1)
                    )
                ).scalars().first()
                if root is not None:
                    root_session = await session.get(ConversationSession, UUID(sid))
                    if (
                        not normalized_dialogue_role
                        and _initial_aoidots_root_applicable(
                            root=root,
                            conversation=root_session,
                            run=run,
                            binding=binding,
                            session_id=sid,
                        )
                    ):
                        # This is a server-derived applicability fact, not a
                        # public/config marker.  Terminal uses it to keep
                        # ordinary projectless Agents and group chats out of
                        # the private AoiDots Human→Run handoff.
                        object.__setattr__(
                            binding,
                            "_initial_aoidots_root_applicable",
                            True,
                        )
                    work_id = _clean(_read(run, "work_item_id"))
                    human_id = _clean(_read(run, "trigger_message_id"))
                    proof = _initial_aoidots_root_proof(
                        root=root, conversation=root_session, run=run,
                        work=await session.get(AgentWorkItem, UUID(work_id)) if work_id else None,
                        human=await session.get(ConversationMessage, UUID(human_id)) if human_id else None,
                        binding=binding, session_id=sid, run_id=run_id,
                    )
                    if proof is not None:
                        # A typed request-local proof cannot be supplied by
                        # public config JSON. Execution still rechecks ORM ACLs.
                        object.__setattr__(binding, "_initial_aoidots_root_proof", proof)
                        root_scope_marker = proof.marker()
            if root_scope_marker is None and not root_dialogue_requested:
                worker_dialogue = (
                    await session.execute(
                        select(AgentDialogue)
                        .where(
                            AgentDialogue.worker_session_id == UUID(sid),
                            AgentDialogue.worker_agent_id == UUID(
                                _clean(getattr(binding, "agent_id", None))
                            ),
                            AgentDialogue.worker_agent_revision_id == UUID(
                                _clean(getattr(binding, "agent_revision_id", None))
                            ),
                            AgentDialogue.state.in_(("queued", "running", "waiting")),
                        )
                        .order_by(AgentDialogue.updated_at.desc(), AgentDialogue.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if worker_dialogue is not None:
                    root_scope_marker = {
                        "scope_kind": "agent_dialogue",
                        "agent_dialogue_id": str(worker_dialogue.id),
                        "dialogue_role": "worker",
                        "root_session_id": str(worker_dialogue.root_session_id),
                        "worker_session_id": str(worker_dialogue.worker_session_id),
                        "worker_agent_id": str(worker_dialogue.worker_agent_id),
                        "worker_agent_revision_id": str(
                            worker_dialogue.worker_agent_revision_id
                        ),
                    }
        except Exception:
            # Lightweight/fake DB sessions may not expose the root table. The
            # authority guard will fail closed when a dialogue tool is used.
            root_scope_marker = None
        if root_scope_marker is not None:
            # TerminalMode carries this immutable binding into TurnContext
            # directly, while its normal Config remains an object rather than
            # a Mapping.  Keep the server-issued marker on the request-local
            # snapshot; the guard still revalidates the durable ORM rows.
            try:
                object.__setattr__(binding, "_root_scope_marker", dict(root_scope_marker))
            except Exception:
                root_scope_marker = None
        return ChatAgentExecution(
            binding=binding,
            route=route,
            system_prompt=_system_prompt(binding),
            workspace=selected_workspace,
            root_scope_marker=root_scope_marker,
            dialogue_budget=dialogue_budget,
        )
    finally:
        if own:
            await session.close()


async def preflight_chat_agent_native_provider(
    provider_id: str,
    *,
    allow_host_probe: bool,
) -> None:
    """Check Claude readiness after admission and before native scope issue.

    The caller must derive allow_host_probe from the original, unshadowed
    run scope. to_thread preserves the current ContextVars as an additional
    fence; the backend never probes while any run scope is still bound.
    Only the existing version/help probe runs here, without auth/generation.
    """

    if str(provider_id or "").strip().casefold() not in {
        "claude", "claude-code", "claude-cli",
    }:
        return
    from ..llm.cli_backends.claude import ClaudeCLIBackend

    supported = await asyncio.to_thread(
        ClaudeCLIBackend.formal_mcp_configuration_supported,
        allow_probe=bool(allow_host_probe),
    )
    if not supported:
        raise AgentDialogueWorkspaceDenied(
            "Claude strict MCP configuration preflight is unavailable"
        )


def build_agent_chat_turn_client(
    source_client: Any,
    config: Any,
    execution: ChatAgentExecution,
    *,
    workspace: AgentDialogueWorkspace | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    tool_generation_max_tokens: int | None = None,
) -> Any:
    """Create a provider client isolated from the long-lived Character client.

    ``tool_generation_max_tokens`` is a per-task explicit opt-in (persisted
    with the admitted task); it is projected only onto this request-local
    target config and never read from global settings.
    """

    from ..llm.manager import create_llm_client_for_target
    from .pricing.providers import is_cli_provider

    budget = execution.dialogue_budget or {}
    configured_ceiling = (
        budget.get("max_tokens") not in (None, "")
        or budget.get("max_cost_micros") not in (None, "")
    )
    if configured_ceiling and is_cli_provider(execution.route.provider):
        # Current Claude/Codex/Antigravity/Grok adapters do not expose a
        # verified hard completion-token/cost flag at this boundary.  Prompt
        # instructions and post-hoc truncation cannot enforce a finite
        # Dialogue ceiling, so deny before the subprocess/provider starts.
        raise AgentDialogueBudgetUnsupported()

    def configured_route_output_cap() -> int | None:
        """Read an existing per-call cap only for the exact selected route."""

        source_provider = str(getattr(source_client, "provider_label", "") or "").strip().casefold()
        source_model = str(getattr(source_client, "model_name", "") or "").strip()
        route_provider = str(execution.route.provider or "").strip().casefold()
        route_model = str(execution.route.model or "").strip()
        if (
            not source_provider
            or not source_model
            or source_provider != route_provider
            or source_model != route_model
        ):
            return None
        for candidate in (
            config,
            getattr(source_client, "config", None),
        ):
            getter = getattr(candidate, "get", None)
            if not callable(getter):
                continue
            try:
                raw = getter("runtime.target_max_output_tokens", None)
                value = int(raw) if raw not in (None, "") else None
            except (TypeError, ValueError, OverflowError):
                continue
            if value is not None and value > 0:
                return value
        return None

    aggregate_output_cap = None
    if execution.dialogue_budget is not None:
        raw_aggregate_cap = budget.get("provider_max_output_tokens")
        try:
            aggregate_output_cap = (
                int(raw_aggregate_cap)
                if raw_aggregate_cap not in (None, "")
                else None
            )
        except (TypeError, ValueError, OverflowError):
            aggregate_output_cap = None
        if aggregate_output_cap is not None and aggregate_output_cap <= 0:
            aggregate_output_cap = None
    configured_output_cap = configured_route_output_cap()
    if aggregate_output_cap is None:
        target_output_cap = configured_output_cap
    elif configured_output_cap is None:
        target_output_cap = aggregate_output_cap
    else:
        target_output_cap = min(aggregate_output_cap, configured_output_cap)

    selected_workspace = (
        workspace
        or execution.workspace
        or getattr(source_client, "_agent_dialogue_workspace", None)
        or get_current_agent_dialogue_workspace()
    )
    if selected_workspace is not None and not isinstance(
        selected_workspace, AgentDialogueWorkspace
    ):
        raise TypeError("workspace must be an AgentDialogueWorkspace")
    if selected_workspace is not None:
        selected_workspace.assert_provider_entry()
        if session_id in (None, ""):
            raise AgentDialogueWorkspaceDenied(
                "Agent client factory requires an explicit session_id"
            )
        if str(session_id) != selected_workspace.session_id:
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace session does not match the explicit Chat session"
            )
        if project_id in (None, "") and selected_workspace.project_id is not None:
            raise AgentDialogueWorkspaceDenied(
                "Agent client factory requires an explicit Project id"
            )
        if str(project_id or "") != str(selected_workspace.project_id or ""):
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace Project does not match the explicit Chat Project"
            )
        if run_id is not None and selected_workspace.run_id != str(run_id):
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace run does not match the explicit execution run"
            )
        if selected_workspace.run_id is not None and run_id in (None, ""):
            raise AgentDialogueWorkspaceDenied(
                "Agent client factory requires an explicit execution run"
            )
        if selected_workspace.agent_id != execution.agent_id:
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace identity does not match the pinned Agent"
            )
        if selected_workspace.provider_id.casefold() != str(execution.route.provider).casefold():
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace provider does not match the pinned route"
            )
        if selected_workspace.model_id != str(execution.route.model):
            raise AgentDialogueWorkspaceDenied(
                "Agent workspace model does not match the pinned route"
            )

    tool_generation_optin = (
        int(tool_generation_max_tokens)
        if tool_generation_max_tokens is not None and int(tool_generation_max_tokens) > 0
        else None
    )

    client = create_llm_client_for_target(
        config,
        provider=execution.route.provider,
        model=execution.route.model,
        effort=execution.route.effort,
        provider_options={
            "ephemeral_session_client": True,
            **(
                {"max_output_tokens": target_output_cap}
                if target_output_cap is not None
                and target_output_cap > 0
                else {}
            ),
            **(
                {"tool_generation_max_tokens": tool_generation_optin}
                if tool_generation_optin is not None
                else {}
            ),
        },
    )
    for attr in (
        "current_session_id",
        "session_user_id",
        "current_project_id",
        "current_include_project_context",
        "current_command_capabilities",
        "current_tool_required",
        "current_edit_message_id",
        "planning_policy",
        "external_persistence_enabled",
        "_privacy_session_context",
        "_privacy_project_metadata",
        "_native_tools_enabled",
    ):
        if selected_workspace is not None and attr in {
            "current_session_id",
            "current_project_id",
        }:
            continue
        if hasattr(source_client, attr):
            value = getattr(source_client, attr)
            try:
                setattr(client, attr, copy.deepcopy(value))
            except Exception:
                setattr(client, attr, value)
    # Session/project privacy snapshots are request-scoped and may belong to a
    # different Character turn on the long-lived source client.  The normal
    # turn setup will populate the current authenticated scope below.
    client._privacy_session_context = {}
    client._privacy_project_metadata = {}
    # The revision route is authoritative.  A response-model picker or session
    # Main route must never override it on this client.
    client.current_response_model = None
    client._agent_execution_binding = execution.binding
    client._agent_route = execution.route.provenance()
    client._agent_dialogue_budget = (
        dict(execution.dialogue_budget)
        if execution.dialogue_budget is not None
        else None
    )
    # OpenAI-compatible/local engines receive the target Config rather than
    # this client when they enter the shared tool loop.  Project the same
    # server-issued round ceiling onto that request-local Config, while the
    # CLI client reads the budget marker directly.  TargetConfig exposes
    # ``set``; lightweight test doubles may expose only a plain attribute.
    if execution.dialogue_budget is not None:
        projected_rounds = execution.dialogue_budget.get(
            "provider_max_tool_rounds"
        )
        if projected_rounds not in (None, ""):
            target_config = getattr(client, "config", None)
            try:
                projected_rounds = max(1, int(projected_rounds))
            except (TypeError, ValueError, OverflowError):
                projected_rounds = None
            if projected_rounds is not None and target_config is not None:
                setter = getattr(target_config, "set", None)
                if callable(setter):
                    setter(
                        "runtime.agent_dialogue_max_tool_rounds",
                        projected_rounds,
                    )
                else:
                    try:
                        setattr(
                            target_config,
                            "_agent_dialogue_max_tool_rounds",
                            projected_rounds,
                        )
                    except Exception:
                        pass
    # The CLI/native boundary uses this marker together with the trusted
    # TurnContext binding.  It must never infer a repository scope from the
    # request/project payload; an already-issued AgentRunScope is required for
    # native filesystem/shell execution, otherwise the provider is reduced to
    # an attested tool-free conversation lane (or denied before subprocess).
    client._agent_native_scope_required = True
    client._agent_dialogue_workspace = selected_workspace
    client._agent_dialogue_working_directory = (
        str(selected_workspace.working_directory)
        if selected_workspace is not None
        else None
    )
    client._agent_dialogue_scratch_directory = (
        str(selected_workspace.runtime_cache_directory)
        if selected_workspace is not None
        else None
    )
    client._agent_dialogue_resource_key = (
        selected_workspace.resource_key if selected_workspace is not None else None
    )
    client._agent_dialogue_native_allowed = bool(
        selected_workspace.native_allowed if selected_workspace is not None else False
    )
    client._agent_dialogue_readonly = bool(
        selected_workspace.readonly if selected_workspace is not None else False
    )
    client._agent_dialogue_capability_ceiling = tuple(
        execution.binding.capability_ceiling
    )
    client._agent_dialogue_native_scope = (
        selected_workspace.native_scope if selected_workspace is not None else None
    )
    client._agent_root_scope_marker = (
        dict(execution.root_scope_marker)
        if execution.root_scope_marker is not None
        else None
    )
    if selected_workspace is not None:
        # The long-lived source client may still point at another Character or
        # Agent session while this isolated target is being built.  Pin the
        # provider continuation identity to the workspace snapshot before the
        # normal turn setup runs.
        client.current_session_id = str(session_id)
        client.current_project_id = project_id
    # Install the lease/workspace and finite-budget guard even when this
    # Agent turn has no repository workspace.  API/local provider calls still
    # need the per-invocation input/context/tool-schema cap.
    _install_agent_workspace_provider_guard(client, selected_workspace)
    client.agent_id = execution.agent_id
    client.agent_revision_id = execution.agent_revision_id
    registry = getattr(client, "_tool_registry", None)
    if registry is not None:
        try:
            from ..tools.agent_activity import build_agent_activity_tool

            registry.register(build_agent_activity_tool())
        except Exception:
            # The central Agent guard still fails closed when the explicit
            # activity entry cannot be installed on a lightweight provider.
            pass
    # Provider adapters with request-local effective-prompt builders must use
    # their isolated override entrypoint.  Calling the legacy
    # ``set_system_prompt`` on a configured Gemini/Ollama/local client
    # intentionally rebuilds the Character prompt and would leave the Agent's
    # mission only in a mutable display attribute.  CLI/legacy clients keep
    # the regular setter fallback for compatibility.
    from ..llm.harness_instructions import with_common_harness_instructions

    composed_prompt = with_common_harness_instructions(execution.system_prompt)
    isolated_setter = getattr(client, "set_isolated_system_prompt", None)
    if callable(isolated_setter):
        isolated_setter(composed_prompt)
    else:
        setter = getattr(client, "set_system_prompt", None)
        if callable(setter):
            setter(composed_prompt)
        elif hasattr(client, "system_prompt"):
            client.system_prompt = composed_prompt
    # Never share provider history/continuation state with the Character
    # client.  The terminal layer primes this client's history from the
    # session transcript for the current turn.
    if hasattr(client, "history_manager"):
        history = getattr(client, "history_manager", None)
        if history is not None and hasattr(history, "clear"):
            history.clear()
    if hasattr(client, "_provider_state"):
        client._provider_state = {}
    return client


def _install_agent_workspace_provider_guard(
    client: Any,
    workspace: AgentDialogueWorkspace | None,
) -> None:
    """Recheck lease/scope at every provider generation entry.

    This covers API/local clients whose execution does not pass through the
    CLI native gate.  The wrapper is installed on the isolated target client
    only; the long-lived Character client is never modified.
    """

    if getattr(client, "_agent_dialogue_provider_guard_installed", False):
        return
    for name in (
        "_generate_async",
        "_generate_response_async_impl",
        "_generate_async_impl",
        "generate_response_async",
    ):
        original = getattr(client, name, None)
        if not callable(original):
            continue

        @wraps(original)
        async def guarded_async(*args: Any, _original=original, **kwargs: Any) -> Any:
            if workspace is not None:
                workspace.assert_provider_entry()
            try:
                original_parameters = inspect.signature(_original).parameters
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in original_parameters.values()
                )
                supported_parameters = set(original_parameters)
            except (TypeError, ValueError):
                accepts_kwargs = True
                supported_parameters = None
            args, kwargs = _apply_agent_dialogue_provider_budget(
                client,
                args,
                kwargs,
                supported_parameters=supported_parameters,
                accepts_kwargs=accepts_kwargs,
            )
            result = _original(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        setattr(client, name, guarded_async)

    original_sync = getattr(client, "generate_response", None)
    if callable(original_sync):
        @wraps(original_sync)
        def guarded_sync(*args: Any, _original=original_sync, **kwargs: Any) -> Any:
            if workspace is not None:
                workspace.assert_provider_entry()
            try:
                original_parameters = inspect.signature(_original).parameters
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in original_parameters.values()
                )
                supported_parameters = set(original_parameters)
            except (TypeError, ValueError):
                accepts_kwargs = True
                supported_parameters = None
            args, kwargs = _apply_agent_dialogue_provider_budget(
                client,
                args,
                kwargs,
                supported_parameters=supported_parameters,
                accepts_kwargs=accepts_kwargs,
            )
            return _original(*args, **kwargs)

        setattr(client, "generate_response", guarded_sync)
    client._agent_dialogue_provider_guard_installed = True


def _apply_agent_dialogue_provider_budget(
    client: Any,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    supported_parameters: set[str] | None = None,
    accepts_kwargs: bool = True,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Apply the server total budget immediately before provider entry."""

    budget = getattr(client, "_agent_dialogue_budget", None)
    if not isinstance(budget, Mapping):
        return args, dict(kwargs)
    from ..llm.agent_runtime import dialogue_provider_invocation_budget

    mutable_kwargs = dict(kwargs)
    user_input = (
        args[0]
        if args
        else mutable_kwargs.get("user_input")
        or mutable_kwargs.get("prompt")
    )
    requested = mutable_kwargs.get("max_tokens")
    if requested in (None, ""):
        requested = mutable_kwargs.get("max_output_tokens")
    configured_output_cap = None
    config = getattr(client, "config", None)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            raw_configured_cap = getter("runtime.target_max_output_tokens", None)
            configured_output_cap = (
                int(raw_configured_cap)
                if raw_configured_cap not in (None, "")
                else None
            )
        except (TypeError, ValueError, OverflowError):
            configured_output_cap = None
        if configured_output_cap is not None and configured_output_cap <= 0:
            configured_output_cap = None
    if configured_output_cap is not None:
        if requested in (None, ""):
            requested = configured_output_cap
        else:
            try:
                requested_value = int(requested)
                requested = (
                    min(requested_value, configured_output_cap)
                    if requested_value > 0
                    else configured_output_cap
                )
            except (TypeError, ValueError, OverflowError):
                requested = configured_output_cap
    cap = dialogue_provider_invocation_budget(
        client,
        user_input=str(user_input or ""),
        requested=requested,
    )
    max_output = cap.get("max_output_tokens")
    if max_output is not None:
        try:
            max_output = int(max_output)
        except (TypeError, ValueError, OverflowError):
            max_output = 0
        if max_output <= 0:
            raise AgentDialogueBudgetUnsupported(
                "provider input/context already consumes the configured AgentDialogue budget"
            )
        setter = getattr(config, "set", None)
        if callable(setter):
            setter("runtime.target_max_output_tokens", max_output)
        else:
            try:
                setattr(config, "_agent_dialogue_max_output_tokens", max_output)
            except Exception:
                pass
        # ResponseHandler normally omits the output argument.  Inject the
        # exact accepted parameter so providers do not rebuild their 1024-token
        # default after this wrapper has computed the smaller server cap.
        if supported_parameters is None:
            supported_parameters = {"max_tokens", "max_output_tokens"}
        accepted_token_keys = [
            key
            for key in ("max_tokens", "max_output_tokens")
            if key in supported_parameters or accepts_kwargs
        ]
        for key in accepted_token_keys:
            if key not in mutable_kwargs:
                continue
            try:
                mutable_kwargs[key] = min(
                    int(mutable_kwargs[key]), max_output
                )
            except (TypeError, ValueError, OverflowError):
                mutable_kwargs[key] = max_output
        if accepted_token_keys:
            token_key = accepted_token_keys[0]
            if token_key not in mutable_kwargs:
                mutable_kwargs[token_key] = max_output
        try:
            setattr(client, "_agent_dialogue_provider_max_output_tokens", max_output)
        except Exception:
            pass
    return args, mutable_kwargs


def _root_scope_config(
    config: Any,
    marker: Mapping[str, Any] | None,
) -> Any:
    """Overlay a server-issued Root marker without copying provider config.

    ``Config`` is an object in the normal app and a Mapping in lightweight
    providers.  The marker is intentionally the only data added to the
    turn-local mapping; arbitrary config/provider state never becomes scope
    authority.
    """

    if marker is None:
        return config
    result = dict(config) if isinstance(config, Mapping) else {}
    result.update({str(key): str(value) for key, value in marker.items()})
    return result


@asynccontextmanager
async def bind_chat_agent_execution(
    execution: ChatAgentExecution,
    *,
    user_id: str | None,
    session_id: str | None,
    project_id: str | None,
    message_id: str | None = None,
    client_message_id: str | None = None,
    config: Any | None = None,
    workspace: AgentDialogueWorkspace | None = None,
):
    """Bind Agent identity, prompt authority, and capability ceiling locally."""

    current = get_turn_context()
    selected_root_scope = execution.root_scope_marker
    bound_config = _root_scope_config(
        config if config is not None else current.agent_execution_config,
        selected_root_scope,
    )
    context_token = set_turn_context(
        user_id=current.user_id or user_id,
        project_id=current.project_id or project_id,
        include_project_context=current.include_project_context,
        session_id=current.session_id or session_id,
        message_id=current.message_id or message_id,
        client_message_id=current.client_message_id or client_message_id,
        task_id=current.task_id,
        docs_reference_ids=current.docs_reference_ids,
        explicit_references=current.explicit_references,
        verified_project_attachment=current.verified_project_attachment,
        suppress_automatic_context=current.suppress_automatic_context,
        strict_project_scope=current.strict_project_scope,
        cloud_advisor_origin=current.cloud_advisor_origin,
        cloud_advisor_assessment=current.cloud_advisor_assessment,
        agent_id=execution.agent_id,
        agent_revision_id=execution.agent_revision_id,
        agent_execution_binding=execution.binding,
        agent_role=execution.route.role or "operator",
        agent_execution_config=(
            bound_config
        ),
        agent_dialogue_id=current.agent_dialogue_id,
        agent_dialogue_role=current.agent_dialogue_role,
        agent_dialogue_input_id=current.agent_dialogue_input_id,
        agent_dialogue_control_revision=current.agent_dialogue_control_revision,
        agent_dialogue_human_anchor=current.agent_dialogue_human_anchor,
        agent_dialogue_typed_resume_answer=(
            current.agent_dialogue_typed_resume_answer
        ),
        agent_dialogue_typed_resume_answer_id=(
            current.agent_dialogue_typed_resume_answer_id
        ),
    )
    from .agent_authority_guard import guard_agent_tool_call_sync

    def _revalidate(tool_name: str, tool_args: Mapping[str, Any], tool_definition: Any) -> bool:
        return guard_agent_tool_call_sync(
            tool_name,
            tool_args,
            tool_definition=tool_definition,
            config=config,
        ).allowed

    selected_workspace = workspace or execution.workspace or get_current_agent_dialogue_workspace()
    if selected_workspace is not None:
        if not isinstance(selected_workspace, AgentDialogueWorkspace):
            raise TypeError("workspace must be an AgentDialogueWorkspace")
        selected_workspace.assert_lease_active()
    tool_scope_token = set_current_agent_tool_scope(
        execution.binding.capability_ceiling,
        allowed_subagent_ids=execution.binding.allowed_subagent_ids,
        revalidate=_revalidate,
    )
    workspace_token = (
        bind_agent_dialogue_workspace(selected_workspace)
        if selected_workspace is not None
        else None
    )
    try:
        if selected_workspace is not None and not bool(
            getattr(selected_workspace, "native_allowed", False)
        ):
            # API/local Agent turns use the server-issued private workspace as
            # the FileExplorer root. Native turns keep their existing
            # AgentRunScope roots, which may intentionally expose additional
            # read roots and must not be narrowed here.
            from ..tools.file_explorer import file_explorer_service
            from ..tools.file_explorer.file_explorer_service import (
                workspace_root_context as bind_workspace_root,
            )

            selected_workspace_root = selected_workspace.workspace_path
            if not file_explorer_service.is_safe_workspace_path(
                selected_workspace_root.parent, selected_workspace_root
            ):
                raise RuntimeError("Agent workspace root is not safe")
            with bind_workspace_root(selected_workspace_root):
                yield execution
        else:
            yield execution
    finally:
        if workspace_token is not None:
            reset_agent_dialogue_workspace(workspace_token)
        reset_current_agent_tool_scope(tool_scope_token)
        reset_turn_context(context_token)


async def start_interactive_chat_work(
    run: Mapping[str, Any] | Any | None,
    *,
    db_manager: Any | None = None,
) -> Any | None:
    """Start the exact admitted interactive work item for a typed run."""

    work_item_id = _clean(_read(run, "work_item_id"))
    run_id = _clean(_read(run, "id"))
    if not work_item_id or not run_id:
        return None
    from .agent_work_runtime import AgentWorkCoordinator

    coordinator = AgentWorkCoordinator(
        db_manager,
        enabled=True,
        worker_id="interactive-chat",
    )
    return await coordinator.start_interactive(work_item_id, run_id=run_id)


async def renew_interactive_chat_work(claim: Any) -> bool:
    if claim is None:
        return True
    coordinator = getattr(claim, "_coordinator", None)
    if coordinator is None:
        from .agent_work_runtime import AgentWorkCoordinator

        coordinator = AgentWorkCoordinator(enabled=True, worker_id="interactive-chat")
    return bool(await coordinator.renew_interactive(claim))


async def settle_interactive_chat_work(
    claim: Any,
    *,
    success: bool,
    cancelled: bool = False,
    awaiting_approval: bool = False,
    error: str | None = None,
) -> Any:
    if claim is None:
        return None
    from .agent_work_runtime import AgentWorkCoordinator, ExecutionOutcome

    coordinator = getattr(claim, "_coordinator", None) or AgentWorkCoordinator(
        enabled=True,
        worker_id="interactive-chat",
    )
    if success:
        outcome = ExecutionOutcome.from_value(
            {
                "classification": "succeeded",
                "success": True,
                "result": {"interactive": True},
            }
        )
    elif cancelled:
        outcome = ExecutionOutcome.from_value(
            {
                "classification": "cancelled",
                "success": False,
                "error_code": "cancelled",
            }
        )
    elif awaiting_approval:
        outcome = ExecutionOutcome.from_value(
            {
                "classification": "awaiting_approval",
                "success": False,
                "error_code": "approval_already_pending",
                "error_message": error or "approval_already_pending",
            }
        )
    else:
        outcome = ExecutionOutcome.from_value(
            {
                "classification": "permanent",
                "success": False,
                "error_code": "interactive_chat_failed",
                "error_message": error or "interactive chat failed",
            }
        )
    return await coordinator.settle_interactive(claim, outcome)


__all__ = [
    "ChatAgentExecution",
    "bind_chat_agent_execution",
    "build_agent_chat_turn_client",
    "bind_initial_aoidots_human_run",
    "resolve_canonical_session_agent_id",
    "resolve_chat_agent_execution",
    "refresh_initial_aoidots_root_proof",
    "settle_interactive_chat_work",
    "start_interactive_chat_work",
    "renew_interactive_chat_work",
]
