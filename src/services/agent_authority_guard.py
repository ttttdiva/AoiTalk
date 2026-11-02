"""Central per-invocation authority guard for Agent-backed tool calls.

The guard is deliberately placed below provider/native/unified adapters.  A
tool can therefore be exposed by any provider, but it cannot execute while the
current AgentRevision, Team intersection, resource scope, or Agent grant is no
longer valid.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from .agent_authority import AgentAuthorityResolver
from .turn_context import get_turn_context, is_explicit_reference_in_turn


@dataclass(frozen=True)
class AgentToolAuthorityDecision:
    allowed: bool
    reason: str = ""
    capability: str | None = None
    required_capabilities: tuple[str, ...] = ()
    reason_code: str = ""
    tool_name: str = ""
    decision_layer: str = ""


def _canonical_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(UUID(text))
    except (TypeError, ValueError, AttributeError):
        return text.casefold()


_AGENT_MEMORY_READ_TOOLS = frozenset(
    {"memory_search", "memory_get", "memory_explain"}
)
_AGENT_MEMORY_WRITE_TOOLS = frozenset(
    {
        "memory_upsert",
        "memory_update",
        "memory_forget",
        "memory_move_scope",
        "memory_promote_to_project_information",
    }
)
_AGENT_MEMORY_TOOLS = _AGENT_MEMORY_READ_TOOLS | _AGENT_MEMORY_WRITE_TOOLS
_AGENT_MEMORY_SCOPES = frozenset({"project", "task", "session"})
_AGENT_DIALOGUE_TOOLS = frozenset(
    {
        "list_agent_workers",
        "delegate",
        "delegate_agent_work",
        "get_agent_work_status",
        "send_agent_work_message",
        "answer_agent_work_question",
        "escalate_agent_work_question",
        "stop_agent_work",
        "resume_agent_work",
    }
)
_AGENT_WORKER_DIALOGUE_TOOLS = frozenset(
    {"get_agent_work_status", "send_agent_work_message"}
)
_AGENT_DIALOGUE_QUESTION_TOOLS = frozenset({"ask_user_question"})
_ROOT_SCOPE_KINDS = frozenset({"aoidots", "agent_dialogue"})
_ACTIVE_AGENT_RUN_STATES = frozenset({"queued", "running", "awaiting_approval"})
_ACTIVE_AGENT_WORK_STATES = frozenset({"claimed", "running"})
_AUTHORITY_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _authority_reason_code(reason: str, *, fallback: str = "authority_scope_denied") -> str:
    """Map guard diagnostics to fixed audit codes without persisting free text."""

    lowered = str(reason or "").casefold()
    if "unknown agent capability" in lowered or "unknown agent capability/resource" in lowered:
        return "capability_unresolved"
    if "tool pack" in lowered and "unknown" in lowered:
        return "pack_unknown"
    if "tool pack" in lowered or "tool pack" in lowered.replace("agent ", ""):
        return "pack_not_allowed"
    if "work lease" in lowered:
        return "work_lease_invalid"
    if "human" in lowered and ("anchor" in lowered or "authority" in lowered):
        return "human_anchor_mismatch"
    if "scope marker" in lowered or "canonical agentdialogue scope" in lowered:
        return "run_scope_missing"
    if "marker mismatch" in lowered or "marker is not canonical" in lowered:
        return "run_scope_mismatch"
    if "agentrun" in lowered and ("required" in lowered or "is required" in lowered):
        return "run_missing"
    if "agentrun" in lowered or "binding" in lowered:
        return "run_scope_invalid"
    if "capability" in lowered and any(
        marker in lowered for marker in ("ceiling", "intersection", "outside")
    ):
        return "capability_not_in_ceiling"
    if "grant" in lowered or "permission" in lowered:
        return "capability_grant_invalid"
    if "could not be revalidated" in lowered or "could not be loaded" in lowered:
        return "authority_revalidation_failed"
    if "subagent" in lowered or "worker" in lowered:
        return "subagent_scope_denied"
    if "memory" in lowered:
        return "memory_scope_denied"
    if "project" in lowered or "task" in lowered or "session" in lowered:
        return "project_scope_denied"
    if "resource" in lowered or "docs" in lowered:
        return "resource_scope_denied"
    return fallback


def _delegate_worker_required_capabilities(args: Mapping[str, Any]) -> frozenset[str]:
    """Resolve the shared kind and workspace capability ceiling."""

    from .agent_delegate_policy import worker_delegate_required_capabilities

    kind = str(args.get("kind") or "").strip().casefold()
    scope = args.get("workspace_scope")
    mode = str(scope.get("mode") or "").strip().casefold() if isinstance(scope, Mapping) else ""
    return worker_delegate_required_capabilities(kind, mode)


def _authority_decision_layer(reason_code: str, fallback: str) -> str:
    if reason_code.startswith("run_"):
        return "run_scope"
    if reason_code == "work_lease_invalid":
        return "work_lease"
    if reason_code == "human_anchor_mismatch":
        return "human_anchor"
    if reason_code.startswith("capability_") or reason_code == "tool_owner_unknown":
        return "capability_resolver"
    if reason_code.startswith("pack_"):
        return "tool_pack"
    if reason_code == "memory_scope_denied":
        return "memory_scope"
    if reason_code == "project_scope_denied":
        return "project_scope"
    if reason_code == "resource_scope_denied":
        return "resource_scope"
    if reason_code == "subagent_scope_denied":
        return "subagent_scope"
    if reason_code == "authority_revalidation_failed":
        return fallback
    return fallback


def _annotate_authority_denial(
    decision: AgentToolAuthorityDecision,
    *,
    tool_name: str,
    tool_definition: Any,
    layer: str,
    required_capabilities: tuple[str, ...] = (),
    reason_code: str = "",
) -> AgentToolAuthorityDecision:
    if decision.allowed:
        return decision
    canonical_tool = str(getattr(tool_definition, "name", "") or "").strip().casefold()
    if not _AUTHORITY_TOOL_NAME_RE.fullmatch(canonical_tool):
        canonical_tool = "unknown_tool"
    selected_code = reason_code or decision.reason_code or _authority_reason_code(
        decision.reason
    )
    return replace(
        decision,
        reason_code=selected_code,
        tool_name=canonical_tool,
        decision_layer=_authority_decision_layer(selected_code, layer),
        required_capabilities=(
            decision.required_capabilities
            or required_capabilities
        ),
    )


def _is_projectless_agent_dialogue_turn(context: Any) -> bool:
    """Return whether this trusted turn is the projectless AoiDots root.

    ``TurnContext`` intentionally stays compatible with ordinary Chat.  The
    bridge therefore carries this marker in the server-owned execution config;
    model text and tool arguments are never consulted here.
    """

    if context is None or _canonical_id(getattr(context, "project_id", None)):
        return False
    if not _canonical_id(getattr(context, "session_id", None)):
        return False
    config = getattr(context, "agent_execution_config", None)
    if not isinstance(config, Mapping):
        return False
    kind = str(
        config.get("scope_kind")
        or config.get("conversation_kind")
        or config.get("dialogue_kind")
        or ""
    ).strip().casefold()
    dialogue_id = config.get("agent_dialogue_id") or config.get("dialogue_id")
    return kind in {"agent_dialogue", "aoidots"} and bool(_canonical_id(dialogue_id))


def _execution_scope_marker(context: Any, config: Any) -> Mapping[str, Any]:
    """Return only the server-bound scope marker for one Agent turn.

    The normal Config object is deliberately not treated as a marker.  The
    adapter may pass a small Mapping overlay, while direct/fake providers may
    pass that overlay as ``config`` instead of carrying a ToolDefinition.
    """

    current = getattr(context, "agent_execution_config", None)
    if isinstance(current, Mapping):
        return current
    binding = getattr(context, "agent_execution_binding", None)
    binding_marker = getattr(binding, "_root_scope_marker", None)
    if isinstance(binding_marker, Mapping):
        return binding_marker
    if isinstance(config, Mapping):
        return config
    dialogue_id = getattr(context, "agent_dialogue_id", None)
    dialogue_role = getattr(context, "agent_dialogue_role", None)
    if dialogue_id and dialogue_role:
        # These fields are only a lookup hint.  Worker/Root ORM proof below
        # remains mandatory before any dialogue or question tool is allowed.
        return {
            "scope_kind": "agent_dialogue",
            "agent_dialogue_id": str(dialogue_id),
            "dialogue_role": str(dialogue_role),
        }
    return {}


async def _projectless_root_dialogue_decision(
    context: Any,
    *,
    config: Any,
) -> AgentToolAuthorityDecision | None:
    """Revalidate the canonical projectless AoiDots Root before dialogue tools.

    A marker only selects the lookup.  Permission comes from the durable
    AgentPrivateRoot, ordinary session/Agent/Revision/Binding rows, the exact
    current AgentRun, and its active WorkItem lease.  This keeps projectless
    Root control separate from the child Project/Task ACLs owned by the
    dialogue service.
    """

    if _canonical_id(getattr(context, "project_id", None)):
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root dialogue tools require a projectless Root session",
        )
    marker = _execution_scope_marker(context, config)
    kind = str(
        marker.get("scope_kind")
        or marker.get("conversation_kind")
        or marker.get("dialogue_kind")
        or ""
    ).strip().casefold()
    if kind not in _ROOT_SCOPE_KINDS:
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root dialogue scope marker is required",
        )
    marker_id = _canonical_id(
        marker.get("agent_dialogue_id")
        or marker.get("dialogue_id")
        or marker.get("root_id")
    )
    if not marker_id:
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root dialogue scope marker is incomplete",
        )
    user_id = _canonical_id(getattr(context, "user_id", None))
    session_id = _canonical_id(getattr(context, "session_id", None))
    agent_id = _canonical_id(getattr(context, "agent_id", None))
    revision_id = _canonical_id(getattr(context, "agent_revision_id", None))
    if not user_id or not session_id or not agent_id or not revision_id:
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root execution identity is incomplete",
        )
    role = str(getattr(context, "agent_dialogue_role", "") or marker.get("role") or "").strip().casefold()
    if role == "worker":
        return AgentToolAuthorityDecision(
            False,
            "AgentDialogue worker turns cannot control the AoiDots Root",
        )

    declared_run_id = _canonical_id(
        marker.get("agent_run_id")
        or marker.get("source_agent_run_id")
        or marker.get("source_run_id")
        or marker.get("run_id")
    )
    try:
        from .agent_run_service import get_current_agent_run_id

        ambient_run_id = _canonical_id(get_current_agent_run_id())
    except Exception:
        ambient_run_id = ""
    if declared_run_id and ambient_run_id and declared_run_id != ambient_run_id:
        return AgentToolAuthorityDecision(False, "AoiDots Root AgentRun marker mismatch")
    if not ambient_run_id:
        return AgentToolAuthorityDecision(False, "current AoiDots Root AgentRun is required")
    run_id = ambient_run_id

    try:
        from sqlalchemy import select

        from ..memory.database import get_database_manager
        from ..memory.models import (
            Agent,
            AgentDialogue,
            AgentExecutionBinding,
            AgentPrivateRoot,
            AgentRevision,
            AgentRun,
            AgentWorkItem,
            ConversationSession,
        )

        manager = get_database_manager()
        raw_session = manager.get_session()
        db = await raw_session if inspect.isawaitable(raw_session) else raw_session
    except Exception:
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root authority could not be loaded",
        )
    try:
        try:
            root = (
                await db.execute(
                    select(AgentPrivateRoot)
                    .where(
                        AgentPrivateRoot.session_id == UUID(session_id),
                        AgentPrivateRoot.purpose == "aoidots",
                        AgentPrivateRoot.state == "active",
                    )
                    .limit(1)
                )
            ).scalars().first()
        except Exception:
            root = None
        if root is None:
            return AgentToolAuthorityDecision(False, "canonical AoiDots Root is unavailable")
        if (
            _canonical_id(root.owner_user_id) != user_id
            or _canonical_id(root.session_id) != session_id
            or _canonical_id(root.agent_id) != agent_id
        ):
            return AgentToolAuthorityDecision(False, "canonical AoiDots Root binding mismatch")
        if marker_id != _canonical_id(root.id):
            dialogue = await db.get(AgentDialogue, UUID(marker_id))
            if (
                dialogue is None
                or _canonical_id(dialogue.owner_user_id) != user_id
                or _canonical_id(dialogue.root_session_id) != session_id
                or _canonical_id(dialogue.root_agent_id) != agent_id
                or _canonical_id(dialogue.root_agent_revision_id) != revision_id
            ):
                return AgentToolAuthorityDecision(False, "AoiDots Root dialogue marker is not canonical")

        root_session = await db.get(ConversationSession, UUID(session_id))
        if (
            root_session is None
            or getattr(root_session, "deleted_at", None) is not None
            or _canonical_id(root_session.user_id) != user_id
            or _canonical_id(root_session.agent_id) != agent_id
            or getattr(root_session, "project_id", None) is not None
        ):
            return AgentToolAuthorityDecision(False, "AoiDots Root session binding is invalid")
        agent = await db.get(Agent, UUID(agent_id))
        if (
            agent is None
            or str(getattr(agent, "state", "")).casefold() != "active"
            or getattr(agent, "created_by", None) not in (None, UUID(user_id))
        ):
            return AgentToolAuthorityDecision(False, "AoiDots Root Agent binding is invalid")
        revision = await db.get(AgentRevision, UUID(revision_id))
        if revision is None or _canonical_id(revision.agent_id) != agent_id:
            return AgentToolAuthorityDecision(False, "AoiDots Root AgentRevision binding is invalid")
        current_revision = (
            await db.execute(
                select(AgentRevision)
                .where(AgentRevision.agent_id == UUID(agent_id))
                .order_by(AgentRevision.version.desc(), AgentRevision.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if current_revision is None or _canonical_id(current_revision.id) != revision_id:
            return AgentToolAuthorityDecision(False, "AoiDots Root AgentRevision is stale")
        binding = (
            await db.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == UUID(session_id),
                    AgentExecutionBinding.agent_id == UUID(agent_id),
                    AgentExecutionBinding.agent_revision_id == UUID(revision_id),
                )
                .order_by(AgentExecutionBinding.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if binding is None:
            return AgentToolAuthorityDecision(False, "AoiDots Root execution binding is unavailable")

        run = await db.get(AgentRun, UUID(run_id))
        if (
            run is None
            or str(getattr(run, "status", "")).casefold() not in _ACTIVE_AGENT_RUN_STATES
            or _canonical_id(run.session_id) != session_id
            or _canonical_id(run.agent_id) != agent_id
            or _canonical_id(run.agent_revision_id) != revision_id
            or _canonical_id(run.user_id) != user_id
            or getattr(run, "project_id", None) is not None
            or run.work_item_id is None
        ):
            return AgentToolAuthorityDecision(False, "AoiDots Root AgentRun is not active for this binding")
        work = await db.get(AgentWorkItem, run.work_item_id)
        now = datetime.utcnow()
        if (
            work is None
            or str(getattr(work, "state", "")).casefold() not in _ACTIVE_AGENT_WORK_STATES
            or _canonical_id(work.active_agent_run_id) != run_id
            or _canonical_id(work.conversation_session_id) != session_id
            or _canonical_id(work.assigned_agent_id) != agent_id
            or _canonical_id(work.agent_revision_id) != revision_id
            or not str(getattr(work, "lease_owner", "") or "").strip()
            or not str(getattr(work, "lease_token", "") or "").strip()
            or getattr(work, "lease_expires_at", None) is None
            or work.lease_expires_at <= now
        ):
            return AgentToolAuthorityDecision(False, "AoiDots Root Work lease is not active")
        declared_work_id = _canonical_id(
            marker.get("work_item_id") or marker.get("agent_work_item_id")
        )
        if declared_work_id and declared_work_id != _canonical_id(work.id):
            return AgentToolAuthorityDecision(False, "AoiDots Root Work marker mismatch")
        return None
    except (asyncio.CancelledError,):
        raise
    except Exception:
        return AgentToolAuthorityDecision(
            False,
            "AoiDots Root authority could not be revalidated",
        )
    finally:
        await db.close()


async def _generic_agent_dialogue_controller_decision(
    context: Any,
    *,
    tool_name: str,
    config: Any,
) -> AgentToolAuthorityDecision | None:
    """Prove a current ordinary Agent run before generic Dialogue controls.

    The dialogue service remains responsible for the requested Dialogue's
    owner/root match. This boundary proves only that the caller is the exact
    active Agent revision and session allowed to request that check.
    """

    user_id = _canonical_id(getattr(context, "user_id", None))
    session_id = _canonical_id(getattr(context, "session_id", None))
    agent_id = _canonical_id(getattr(context, "agent_id", None))
    revision_id = _canonical_id(getattr(context, "agent_revision_id", None))
    if not user_id or not session_id or not agent_id or not revision_id:
        return AgentToolAuthorityDecision(
            False,
            "generic Agent Dialogue controller identity is incomplete",
        )
    try:
        from .agent_run_service import get_current_agent_run_id

        run_id = _canonical_id(get_current_agent_run_id())
    except Exception:
        run_id = ""
    if not run_id:
        return AgentToolAuthorityDecision(
            False,
            "current generic AgentRun is required for Dialogue controls",
            reason_code="run_scope_missing",
        )
    try:
        from sqlalchemy import select

        from ..memory.database import get_database_manager
        from ..memory.models import (
            Agent,
            AgentExecutionBinding,
            AgentRevision,
            AgentRun,
            AgentWorkItem,
            ConversationSession,
        )
        from .agent_delegate_policy import (
            revision_has_new_delegate_policy,
        )
        from .agent_revision_v2 import revision_tool_ids

        manager = get_database_manager()
        raw_session = manager.get_session()
        db = await raw_session if inspect.isawaitable(raw_session) else raw_session
    except Exception:
        return AgentToolAuthorityDecision(
            False,
            "generic Agent Dialogue controller could not load its authority rows",
        )
    try:
        run = await db.get(AgentRun, UUID(run_id))
        session = await db.get(ConversationSession, UUID(session_id))
        agent = await db.get(Agent, UUID(agent_id))
        revision = await db.get(AgentRevision, UUID(revision_id))
        current_run_binding = bool(
            run is not None
            and str(getattr(run, "status", "")).casefold()
            in _ACTIVE_AGENT_RUN_STATES
            and _canonical_id(run.id) == run_id
            and _canonical_id(run.session_id) == session_id
            and _canonical_id(run.user_id) == user_id
            and _canonical_id(run.agent_id) == agent_id
            and _canonical_id(run.agent_revision_id) == revision_id
            and run.work_item_id is not None
            and session is not None
            and getattr(session, "deleted_at", None) is None
            and _canonical_id(session.user_id) == user_id
            and _canonical_id(session.agent_id) == agent_id
            and _canonical_id(session.project_id) == _canonical_id(run.project_id)
            and agent is not None
            and str(getattr(agent, "state", "")).casefold() == "active"
            and revision is not None
            and _canonical_id(revision.agent_id) == agent_id
        )
        if current_run_binding and not revision_has_new_delegate_policy(revision):
            return AgentToolAuthorityDecision(
                False,
                "AoiDots Root dialogue scope marker is required",
                reason_code="run_scope_missing",
            )
        if not current_run_binding or not revision_has_new_delegate_policy(revision):
            return AgentToolAuthorityDecision(
                False,
                "generic Agent Dialogue controller is not bound to the current run",
            )
        binding = (
            await db.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == UUID(session_id),
                    AgentExecutionBinding.agent_id == UUID(agent_id),
                    AgentExecutionBinding.agent_revision_id == UUID(revision_id),
                )
                .order_by(AgentExecutionBinding.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if binding is None:
            return AgentToolAuthorityDecision(
                False,
                "generic Agent execution binding is unavailable",
            )
        work = await db.get(AgentWorkItem, run.work_item_id)
        if (
            work is None
            or str(getattr(work, "state", "")).casefold()
            not in _ACTIVE_AGENT_WORK_STATES
            or _canonical_id(work.active_agent_run_id) != run_id
            or _canonical_id(work.conversation_session_id) != session_id
            or _canonical_id(work.assigned_agent_id) != agent_id
            or _canonical_id(work.agent_revision_id) != revision_id
            or not str(getattr(work, "lease_owner", "") or "").strip()
            or not str(getattr(work, "lease_token", "") or "").strip()
            or getattr(work, "lease_expires_at", None) is None
            or work.lease_expires_at <= datetime.utcnow()
        ):
            return AgentToolAuthorityDecision(
                False,
                "generic Agent Dialogue controller Work lease is not active",
            )
        if getattr(session, "project_id", None) is not None:
            from ..memory.project_repository import ProjectRepository

            if not await ProjectRepository.has_permission(
                db,
                session.project_id,
                UUID(user_id),
                "read",
            ):
                return AgentToolAuthorityDecision(
                    False,
                    "generic Agent Dialogue controller Project access was revoked",
                )
        if tool_name == "delegate_agent_work":
            tool_ids = revision_tool_ids(revision)
            if tool_ids is None or not {
                "delegate",
                "delegate_agent_work",
            }.intersection({
                item.strip().casefold()
                for item in tool_ids
                if isinstance(item, str) and item.strip()
            }):
                return AgentToolAuthorityDecision(
                    False,
                    "AgentRevision does not expose the compatibility alias",
                )
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        return AgentToolAuthorityDecision(
            False,
            "generic Agent Dialogue controller authority could not be revalidated",
        )
    finally:
        await db.close()


async def _projectless_worker_dialogue_decision(
    context: Any,
    *,
    config: Any,
    tool_name: str,
    tool_args: Mapping[str, Any],
) -> AgentToolAuthorityDecision | None:
    """Revalidate one projectless worker Dialogue turn independently.

    A worker never inherits the Root proof.  The Dialogue row is the source
    of truth for the worker session/Agent/Revision and owner; the current
    worker Run/Work lease and its parent Root Run are re-read before the
    worker-safe read/message/question lane is admitted.
    """

    if tool_name not in _AGENT_WORKER_DIALOGUE_TOOLS | _AGENT_DIALOGUE_QUESTION_TOOLS:
        return AgentToolAuthorityDecision(
            False,
            "Worker Agent cannot control the Root AgentDialogue",
        )
    projectless_turn = not _canonical_id(getattr(context, "project_id", None))
    marker = _execution_scope_marker(context, config)
    kind = str(
        marker.get("scope_kind")
        or marker.get("conversation_kind")
        or marker.get("dialogue_kind")
        or ""
    ).strip().casefold()
    role = str(
        getattr(context, "agent_dialogue_role", "")
        or marker.get("dialogue_role")
        or marker.get("role")
        or ""
    ).strip().casefold()
    if kind != "agent_dialogue" or role != "worker":
        return AgentToolAuthorityDecision(
            False,
            "canonical worker Dialogue scope marker is required",
        )
    marker_id = _canonical_id(
        marker.get("agent_dialogue_id") or marker.get("dialogue_id")
    )
    user_id = _canonical_id(getattr(context, "user_id", None))
    session_id = _canonical_id(getattr(context, "session_id", None))
    agent_id = _canonical_id(getattr(context, "agent_id", None))
    revision_id = _canonical_id(getattr(context, "agent_revision_id", None))
    context_dialogue_id = _canonical_id(getattr(context, "agent_dialogue_id", None))
    if (
        not marker_id
        or not user_id
        or not session_id
        or not agent_id
        or not revision_id
        or (context_dialogue_id and context_dialogue_id != marker_id)
    ):
        return AgentToolAuthorityDecision(False, "worker Dialogue execution identity is incomplete")
    context_control_revision = getattr(context, "agent_dialogue_control_revision", None)
    marker_control_revision = marker.get("control_revision")

    try:
        from .agent_run_service import get_current_agent_run_id

        ambient_run_id = _canonical_id(get_current_agent_run_id())
    except Exception:
        ambient_run_id = ""
    declared_run_id = _canonical_id(
        marker.get("agent_run_id")
        or marker.get("source_agent_run_id")
        or marker.get("run_id")
    )
    if not ambient_run_id:
        return AgentToolAuthorityDecision(False, "current worker AgentRun is required")
    if declared_run_id and declared_run_id != ambient_run_id:
        return AgentToolAuthorityDecision(False, "worker AgentRun marker mismatch")

    try:
        from sqlalchemy import select

        from ..memory.database import get_database_manager
        from ..memory.models import (
            Agent,
            AgentDialogue,
            AgentRevision,
            AgentRun,
            AgentWorkItem,
            ConversationSession,
        )

        manager = get_database_manager()
        raw_session = manager.get_session()
        db = await raw_session if inspect.isawaitable(raw_session) else raw_session
    except Exception:
        return AgentToolAuthorityDecision(False, "worker Dialogue authority could not be loaded")

    try:
        dialogue = await db.get(AgentDialogue, UUID(marker_id))
        if (
            dialogue is None
            or str(dialogue.state or "").casefold()
            not in {"queued", "running", "waiting", "blocked"}
            or _canonical_id(dialogue.id) != marker_id
            or _canonical_id(dialogue.owner_user_id) != user_id
            or _canonical_id(dialogue.worker_session_id) != session_id
            or _canonical_id(dialogue.worker_agent_id) != agent_id
            or _canonical_id(dialogue.worker_agent_revision_id) != revision_id
        ):
            return AgentToolAuthorityDecision(False, "worker Dialogue binding is invalid")
        if (
            context_control_revision is not None
            and int(context_control_revision) != int(dialogue.control_revision or 0)
        ) or (
            marker_control_revision not in (None, "")
            and int(marker_control_revision) != int(dialogue.control_revision or 0)
        ):
            return AgentToolAuthorityDecision(False, "worker Dialogue control revision is stale")

        worker_session = await db.get(ConversationSession, UUID(session_id))
        if (
            worker_session is None
            or getattr(worker_session, "deleted_at", None) is not None
            or _canonical_id(worker_session.user_id) != user_id
            or _canonical_id(worker_session.agent_id) != agent_id
        ):
            return AgentToolAuthorityDecision(False, "worker session binding is invalid")
        worker = await db.get(Agent, UUID(agent_id))
        worker_revision = await db.get(AgentRevision, UUID(revision_id))
        if (
            worker is None
            or str(getattr(worker, "state", "")).casefold() != "active"
            or getattr(worker, "created_by", None) not in (None, UUID(user_id))
            or worker_revision is None
            or _canonical_id(worker_revision.agent_id) != agent_id
        ):
            return AgentToolAuthorityDecision(False, "worker Agent binding is invalid")

        # A worker may use this lane only when its pinned capability ceiling
        # contains the neutral aoi_tools capability. This is a ceiling check,
        # never a new grant.
        worker_ceiling = {
            str(item).strip()
            for item in (getattr(worker_revision, "capability_ceiling_json", None) or [])
            if str(item).strip()
        }
        if "aoi_tools" not in worker_ceiling:
            return AgentToolAuthorityDecision(False, "worker revision has no aoi_tools capability")

        root_session_id = _canonical_id(dialogue.root_session_id)
        root_agent_id = _canonical_id(dialogue.root_agent_id)
        root_revision_id = _canonical_id(dialogue.root_agent_revision_id)
        root_session = await db.get(ConversationSession, UUID(root_session_id))
        root_agent = await db.get(Agent, UUID(root_agent_id))
        root_revision = await db.get(AgentRevision, UUID(root_revision_id))
        if (
            root_session is None
            or getattr(root_session, "deleted_at", None) is not None
            or _canonical_id(root_session.user_id) != user_id
            or _canonical_id(root_session.agent_id) != root_agent_id
            or root_agent is None
            or str(getattr(root_agent, "state", "")).casefold() != "active"
            or getattr(root_agent, "created_by", None) not in (None, UUID(user_id))
            or root_revision is None
            or _canonical_id(root_revision.agent_id) != root_agent_id
        ):
            return AgentToolAuthorityDecision(False, "worker Dialogue Root binding is invalid")
        from .agent_delegate_policy import revision_has_new_delegate_policy

        generic_dialogue = revision_has_new_delegate_policy(root_revision)
        if not projectless_turn and not generic_dialogue:
            return AgentToolAuthorityDecision(
                False,
                "projectless worker Dialogue scope is required",
            )
        if generic_dialogue:
            root_project_id = _canonical_id(getattr(root_session, "project_id", None))
            context_project_id = _canonical_id(getattr(context, "project_id", None))
            dialogue_project_id = _canonical_id(getattr(dialogue, "project_id", None))
            if root_project_id != context_project_id or dialogue_project_id != context_project_id:
                return AgentToolAuthorityDecision(
                    False,
                    "worker Dialogue Project binding is invalid",
                )
            if root_project_id:
                from ..memory.project_repository import ProjectRepository

                if not await ProjectRepository.has_permission(
                    db,
                    UUID(root_project_id),
                    UUID(user_id),
                    "read",
                ):
                    return AgentToolAuthorityDecision(
                        False,
                        "worker Dialogue Project access was revoked",
                    )
        root_ceiling = {
            str(item).strip()
            for item in (getattr(root_revision, "capability_ceiling_json", None) or [])
            if str(item).strip()
        }
        if "aoi_tools" not in root_ceiling:
            return AgentToolAuthorityDecision(False, "source Root revision has no aoi_tools capability")

        worker_run = await db.get(AgentRun, UUID(ambient_run_id))
        if (
            worker_run is None
            or str(getattr(worker_run, "status", "")).casefold() not in _ACTIVE_AGENT_RUN_STATES
            or _canonical_id(worker_run.session_id) != session_id
            or _canonical_id(worker_run.agent_id) != agent_id
            or _canonical_id(worker_run.agent_revision_id) != revision_id
            or _canonical_id(worker_run.user_id) != user_id
            or worker_run.work_item_id is None
        ):
            return AgentToolAuthorityDecision(False, "current worker AgentRun is not bound to this Dialogue")
        source_run_id = _canonical_id(worker_run.parent_run_id) or _canonical_id(
            marker.get("source_run_id") or marker.get("root_run_id") or dialogue.human_run_id
        )
        if not source_run_id:
            return AgentToolAuthorityDecision(False, "worker source Root AgentRun is required")
        source_run = await db.get(AgentRun, UUID(source_run_id))
        if (
            source_run is None
            or _canonical_id(source_run.session_id) != root_session_id
            or _canonical_id(source_run.agent_id) != root_agent_id
            or _canonical_id(source_run.agent_revision_id) != root_revision_id
            or _canonical_id(source_run.user_id) != user_id
        ):
            return AgentToolAuthorityDecision(False, "worker source Root AgentRun is invalid")
        source_metadata = source_run.run_metadata if isinstance(source_run.run_metadata, Mapping) else {}
        source_dialogue_metadata = source_metadata.get("agent_dialogue") if isinstance(source_metadata.get("agent_dialogue"), Mapping) else {}
        if str(source_dialogue_metadata.get("role") or "").strip().casefold() == "worker":
            return AgentToolAuthorityDecision(False, "worker source Run cannot be another worker")
        work = await db.get(AgentWorkItem, worker_run.work_item_id)
        now = datetime.utcnow()
        if (
            work is None
            or str(getattr(work, "state", "")).casefold() not in _ACTIVE_AGENT_WORK_STATES
            or _canonical_id(work.active_agent_run_id) != ambient_run_id
            or _canonical_id(work.conversation_session_id) != session_id
            or _canonical_id(work.assigned_agent_id) != agent_id
            or _canonical_id(work.agent_revision_id) != revision_id
            or not str(getattr(work, "lease_owner", "") or "").strip()
            or not str(getattr(work, "lease_token", "") or "").strip()
            or getattr(work, "lease_expires_at", None) is None
            or work.lease_expires_at <= now
        ):
            return AgentToolAuthorityDecision(False, "worker Work lease is not active")
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        return AgentToolAuthorityDecision(False, "worker Dialogue authority could not be revalidated")
    finally:
        await db.close()


def _scope_id_matches_current_or_explicit(
    context: Any,
    *,
    kind: str,
    value: Any,
) -> bool:
    candidate = _canonical_id(value)
    if not candidate:
        return False
    current = _canonical_id(getattr(context, f"{kind}_id", None))
    if current and candidate == current:
        return True
    return is_explicit_reference_in_turn(kind, str(value), context=context)


async def _memory_row_scope_decision(
    context: Any,
    db: Any,
    row: Any,
) -> AgentToolAuthorityDecision | None:
    """Validate a persisted memory row against canonical Agent turn scope.

    The row is the authority for its scope metadata.  Model arguments never
    override it, and project/task/session parents are re-read before the
    Agent capability resolver runs.
    """

    scope_type = str(getattr(row, "scope_type", "") or "").strip().casefold()
    if scope_type not in _AGENT_MEMORY_SCOPES:
        return AgentToolAuthorityDecision(
            False,
            "Agent memory access is limited to project/task/current session scope",
        )
    current_project = _canonical_id(getattr(context, "project_id", None))
    projectless_dialogue = _is_projectless_agent_dialogue_turn(context)
    if not current_project and not projectless_dialogue:
        return AgentToolAuthorityDecision(False, "Agent memory requires a canonical Project scope")
    persisted_scope_id = _canonical_id(getattr(row, "scope_id", None))
    if scope_type == "project":
        if projectless_dialogue:
            return AgentToolAuthorityDecision(False, "projectless Agent dialogue memory is session-only")
        if (
            persisted_scope_id != current_project
            or _canonical_id(getattr(row, "project_id", None)) != current_project
        ):
            return AgentToolAuthorityDecision(False, "memory row is outside the current Agent Project scope")
        return None

    if scope_type == "task":
        if projectless_dialogue:
            return AgentToolAuthorityDecision(False, "projectless Agent dialogue memory is session-only")
        task_id = _canonical_id(getattr(row, "task_id", None))
        if not task_id or persisted_scope_id != task_id:
            return AgentToolAuthorityDecision(False, "memory row has invalid canonical Task scope")
        if not _scope_id_matches_current_or_explicit(context, kind="task", value=task_id):
            return AgentToolAuthorityDecision(False, "memory row is outside the current Agent Task scope")
        try:
            from ..memory.models import Task

            task = await db.get(Task, UUID(task_id))
        except Exception:
            task = None
        if (
            task is None
            or getattr(task, "deleted_at", None) is not None
            or _canonical_id(getattr(task, "project_id", None)) != current_project
        ):
            return AgentToolAuthorityDecision(False, "memory Task parent is unavailable or outside the Agent Project")
        if getattr(row, "project_id", None) not in (None, "") and _canonical_id(getattr(row, "project_id", None)) != current_project:
            return AgentToolAuthorityDecision(False, "memory row Project metadata is inconsistent")
        return None

    session_id = _canonical_id(getattr(row, "session_id", None))
    current_session = _canonical_id(getattr(context, "session_id", None))
    if not session_id or persisted_scope_id != session_id or not current_session or session_id != current_session:
        return AgentToolAuthorityDecision(False, "memory row is outside the current Agent session scope")
    try:
        from ..memory.models import ConversationSession

        conversation = await db.get(ConversationSession, UUID(session_id))
    except Exception:
        conversation = None
    conversation_project = _canonical_id(getattr(conversation, "project_id", None)) if conversation is not None else None
    project_matches = (
        conversation_project == current_project
        if current_project
        else projectless_dialogue and conversation_project is None
    )
    if (
        conversation is None
        or getattr(conversation, "deleted_at", None) is not None
        or not project_matches
        or _canonical_id(getattr(conversation, "user_id", None))
        != _canonical_id(getattr(context, "user_id", None))
    ):
        return AgentToolAuthorityDecision(False, "memory session parent is unavailable or outside the current human scope")
    if current_project and getattr(row, "project_id", None) not in (None, "") and _canonical_id(getattr(row, "project_id", None)) != current_project:
        return AgentToolAuthorityDecision(False, "memory row Project metadata is inconsistent")
    if projectless_dialogue and getattr(row, "project_id", None) not in (None, ""):
        return AgentToolAuthorityDecision(False, "projectless Agent dialogue memory row cannot carry a Project")
    return None


async def _memory_scope_decision(
    context: Any,
    tool_name: str,
    args: Mapping[str, Any],
) -> AgentToolAuthorityDecision | None:
    """Check Agent memory scope before any memory service/ACL body access."""

    if tool_name not in _AGENT_MEMORY_TOOLS:
        return None
    current_project = _canonical_id(getattr(context, "project_id", None))
    projectless_dialogue = _is_projectless_agent_dialogue_turn(context)
    if not current_project and not projectless_dialogue:
        return AgentToolAuthorityDecision(False, "Agent memory requires a canonical Project scope")

    def optional_id_matches(kind: str, value: Any) -> bool:
        if value in (None, ""):
            return True
        if projectless_dialogue and kind != "session":
            return False
        return _scope_id_matches_current_or_explicit(
            context,
            kind=kind,
            value=value,
        )

    if tool_name in {"memory_search", "memory_upsert"}:
        scope = str(args.get("scope") or "").strip().casefold()
        if scope not in _AGENT_MEMORY_SCOPES:
            return AgentToolAuthorityDecision(False, "Agent memory scope must be project, task, or session")
        if projectless_dialogue and scope != "session":
            return AgentToolAuthorityDecision(False, "projectless Agent dialogue memory is session-only")
        if not optional_id_matches("project", args.get("project_id")):
            return AgentToolAuthorityDecision(False, "memory Project target is outside the current Agent scope")
        if not optional_id_matches("task", args.get("task_id")):
            return AgentToolAuthorityDecision(False, "memory Task target is outside the current Agent scope")
        if tool_name == "memory_upsert":
            target = args.get("scope_id")
            kind = scope
            if target in (None, ""):
                target = getattr(context, f"{kind}_id", None)
            if not _scope_id_matches_current_or_explicit(context, kind=kind, value=target):
                return AgentToolAuthorityDecision(False, "memory write target is outside the current canonical scope")
        elif scope == "project":
            target = args.get("project_id") or getattr(context, "project_id", None)
            if _canonical_id(target) != current_project:
                return AgentToolAuthorityDecision(False, "memory search Project target is outside the current Agent scope")
        elif scope == "task":
            target = args.get("task_id") or getattr(context, "task_id", None)
            if not _scope_id_matches_current_or_explicit(context, kind="task", value=target):
                return AgentToolAuthorityDecision(False, "memory search Task target is outside the current Agent scope")
        else:
            if not _canonical_id(getattr(context, "session_id", None)):
                return AgentToolAuthorityDecision(False, "memory search requires the current session")
        return None

    # ID-only reads/mutations resolve the persisted row first.  This prevents
    # a model-supplied scope_id/project from widening an otherwise valid ID.
    memory_id = args.get("memory_id")
    if memory_id not in (None, ""):
        try:
            memory_uuid = UUID(str(memory_id))
        except (TypeError, ValueError, AttributeError):
            return AgentToolAuthorityDecision(False, "memory identifier is invalid")
        try:
            from ..memory.database import get_database_manager
            from ..memory.models import ContextMemory

            manager = get_database_manager()
            raw_session = manager.get_session()
            db = await raw_session if inspect.isawaitable(raw_session) else raw_session
        except Exception:
            return AgentToolAuthorityDecision(False, "memory scope could not be resolved")
        try:
            row = await db.get(ContextMemory, memory_uuid)
            if row is None:
                return AgentToolAuthorityDecision(False, "memory row is unavailable")
            decision = await _memory_row_scope_decision(context, db, row)
            if decision is not None:
                return decision
            if tool_name == "memory_promote_to_project_information" and str(getattr(row, "scope_type", "")).casefold() != "project":
                return AgentToolAuthorityDecision(False, "only Project memory may be promoted")
            if tool_name == "memory_move_scope":
                target_scope = str(args.get("target_scope") or "").strip().casefold()
                target_id = args.get("target_scope_id") or getattr(context, f"{target_scope}_id", None)
                if target_scope not in _AGENT_MEMORY_SCOPES or not _scope_id_matches_current_or_explicit(context, kind=target_scope, value=target_id):
                    return AgentToolAuthorityDecision(False, "memory move target is outside the current canonical scope")
            return None
        finally:
            await db.close()
    return AgentToolAuthorityDecision(False, "memory identifier is required")


def _project_scope_decision(
    context: Any,
    args: Mapping[str, Any],
    *,
    memory_tool: bool = False,
) -> AgentToolAuthorityDecision | None:
    canonical_project = _canonical_id(getattr(context, "project_id", None))
    for key in ("project_id", "project", "project_ref", "target_project_id"):
        raw = args.get(key)
        if raw in (None, ""):
            continue
        if not canonical_project or _canonical_id(raw) != canonical_project:
            return AgentToolAuthorityDecision(
                False,
                "tool target is outside the server-derived Agent Project scope",
            )
    for key, kind in (
        ("space_id", "space"),
        ("persona_id", "persona"),
        ("task_id", "task"),
    ):
        if memory_tool and kind == "task":
            # Memory scope validation already compares the requested Task to
            # the canonical current/explicit Task and persisted parent row.
            continue
        raw = args.get(key)
        if raw in (None, ""):
            continue
        if not is_explicit_reference_in_turn(kind, str(raw), context=context):
            return AgentToolAuthorityDecision(
                False,
                f"tool target {kind} was not server-authorized for this turn",
            )
    # Resource IDs are not authority by themselves.  A model-produced Docs
    # node/file/media/artifact ID must either be a server-authorized explicit
    # reference on this turn or be rejected before the concrete human ACL
    # service runs.  This prevents a Project-A Agent from using a human user's
    # valid ACL to reach a Project-B resource.
    resource_keys = {
        "node_id": "docs",
        "knowledge_node_id": "docs",
        "docs_node_id": "docs",
        "document_id": "docs_document",
        "file_id": "file",
        "attachment_id": "attachment",
        "asset_id": "media_asset",
        "media_id": "media_asset",
        "artifact_id": "artifact",
        "resource_id": "resource",
        "work_item_id": "work_item",
        "agent_run_id": "agent_run",
        "docs_library_id": "docs_library",
        "library_id": "docs_library",
        "workspace_id": "workspace",
        "persona_ref": "persona",
        "character_id": "persona",
        "content_item_id": "media_content",
        "generation_plan_id": "media_plan",
        "action_id": "action",
        "delegation_id": "delegation",
    }
    for key, kind in resource_keys.items():
        raw = args.get(key)
        if raw in (None, ""):
            continue
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else (raw,)
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if is_explicit_reference_in_turn(kind, text, context=context):
                continue
            # Explicit @ references are normalized to a small set of canonical
            # resource kinds. Keep a narrow alias set for older mention rows.
            aliases = {
                "docs": ("docs", "node", "knowledge"),
                "docs_document": ("docs_document", "docs", "node", "knowledge"),
                "file": ("file", "workspace_file"),
                "attachment": ("attachment", "file"),
                "media_asset": ("media_asset", "media"),
                "artifact": ("artifact", "agent_artifact"),
                "work_item": ("work_item", "agent_work"),
                "agent_run": ("agent_run",),
                "resource": ("resource",),
            }.get(kind, (kind,))
            if any(is_explicit_reference_in_turn(alias, text, context=context) for alias in aliases):
                continue
            return AgentToolAuthorityDecision(
                False,
                "resource target was not server-authorized for this Agent turn",
            )
    return None


async def _resource_scope_decision(
    context: Any,
    args: Mapping[str, Any],
) -> AgentToolAuthorityDecision | None:
    """Resolve common resource targets to their canonical Project owner.

    The guard does not infer ownership from names or prompt text.  For a known
    UUID resource it asks the canonical ORM row for ``project_id`` (including
    the Docs node/attachment parent) and compares it to the server-derived
    turn Project. Unknown/missing rows fail closed for Agent turns.
    """

    targets: list[tuple[str, str, Any]] = []
    for key, kind in (
        ("task_id", "task"),
        ("node_id", "node"),
        ("knowledge_node_id", "node"),
        ("docs_node_id", "node"),
        ("attachment_id", "attachment"),
        ("file_id", "file"),
        ("asset_id", "asset"),
        ("media_id", "asset"),
        ("artifact_id", "artifact"),
    ):
        raw = args.get(key)
        if raw in (None, ""):
            continue
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else (raw,)
        for value in values:
            if str(value or "").strip():
                targets.append((key, kind, value))
    if not targets:
        return None
    try:
        from ..memory.database import get_database_manager
        from ..memory.models import (
            ArtifactVersion,
            KnowledgeAttachment,
            KnowledgeNode,
            MediaAssetRevision,
            Task,
        )

        manager = get_database_manager()
        raw_session = manager.get_session()
        db = await raw_session if inspect.isawaitable(raw_session) else raw_session
    except Exception:
        return AgentToolAuthorityDecision(False, "resource owner could not be resolved")
    try:
        project_id = _canonical_id(getattr(context, "project_id", None))
        for key, kind, raw in targets:
            try:
                resource_id = UUID(str(raw))
            except (TypeError, ValueError, AttributeError):
                return AgentToolAuthorityDecision(False, "resource target identifier is invalid")
            model = {
                "task": Task,
                "node": KnowledgeNode,
                "attachment": KnowledgeAttachment,
                "file": KnowledgeAttachment,
                "asset": MediaAssetRevision,
                "artifact": ArtifactVersion,
            }[kind]
            row = await db.get(model, resource_id)
            if row is None:
                return AgentToolAuthorityDecision(False, "resource target is unavailable")
            if kind in {"attachment", "file"}:
                node_id = getattr(row, "node_id", None)
                node = await db.get(KnowledgeNode, node_id) if node_id is not None else None
                owner_project = getattr(node, "project_id", None) if node is not None else None
            else:
                owner_project = getattr(row, "project_id", None)
            if owner_project is None or not project_id or _canonical_id(owner_project) != project_id:
                return AgentToolAuthorityDecision(False, "resource target is outside the server-derived Agent Project scope")
        return None
    finally:
        await db.close()


def _subagent_decision(
    context: Any,
    tool_name: str,
    args: Mapping[str, Any],
    config: Any,
) -> AgentToolAuthorityDecision | None:
    if tool_name != "agent_team_delegate":
        return None
    return AgentToolAuthorityDecision(False, "Agent Team delegation is retired; use delegate")


async def guard_agent_tool_call_async(
    tool_name: str,
    tool_args: Mapping[str, Any] | None = None,
    *,
    tool_definition: Any = None,
    config: Any = None,
) -> AgentToolAuthorityDecision:
    """Revalidate current Agent authority before one concrete invocation."""

    context = get_turn_context()
    binding = getattr(context, "agent_execution_binding", None)
    if binding is None:
        return AgentToolAuthorityDecision(True)
    user_id = str(getattr(context, "user_id", None) or "").strip()
    agent_id = str(getattr(context, "agent_id", None) or getattr(binding, "agent_id", "") or "").strip()
    normalized_tool = str(tool_name or "").rsplit(".", 1)[-1].strip().lower()
    from ..llm.tool_policy import agent_capability_ids_for_tool

    tool_capabilities = agent_capability_ids_for_tool(
        normalized_tool,
        tool_definition,
    )
    revision_id = str(
        getattr(context, "agent_revision_id", None)
        or getattr(binding, "agent_revision_id", "")
        or ""
    ).strip()
    if not user_id or not agent_id or not revision_id:
        return _annotate_authority_denial(
            AgentToolAuthorityDecision(False, "Agent execution identity is incomplete"),
            tool_name=tool_name,
            tool_definition=tool_definition,
            layer="execution_identity",
            required_capabilities=tuple(sorted(tool_capabilities)),
            reason_code="execution_identity_missing",
        )
    args = dict(tool_args or {})
    if normalized_tool == "delegate_worker":
        tool_capabilities = _delegate_worker_required_capabilities(args)
    if normalized_tool == "load_tool_pack":
        from ..llm.tool_packs import agent_tool_pack_denial_code

        denial_code = agent_tool_pack_denial_code(
            args.get("pack"),
            tool_definition,
        )
        if denial_code == "pack_unknown":
            return _annotate_authority_denial(
                AgentToolAuthorityDecision(False, "Unknown Agent tool pack"),
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="tool_pack",
                reason_code="pack_unknown",
            )
        if denial_code == "pack_not_allowed":
            return _annotate_authority_denial(
                AgentToolAuthorityDecision(False, "Agent tool pack is not available for this turn"),
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="tool_pack",
                reason_code="pack_not_allowed",
            )
        # Loading a pack changes only the session's presented schema. Every
        # concrete tool from that pack still passes this guard independently.
        return AgentToolAuthorityDecision(True)
    projectless_turn = not _canonical_id(getattr(context, "project_id", None))
    worker_dialogue_lane = False
    scope_marker = _execution_scope_marker(context, config)
    dialogue_role = str(
        getattr(context, "agent_dialogue_role", "")
        or scope_marker.get("dialogue_role")
        or scope_marker.get("role")
        or ""
    ).strip().casefold()
    scope_kind = str(
        scope_marker.get("scope_kind")
        or scope_marker.get("conversation_kind")
        or scope_marker.get("dialogue_kind")
        or ""
    ).strip().casefold()
    if normalized_tool in _AGENT_DIALOGUE_TOOLS:
        if dialogue_role == "worker":
            worker_decision = await _projectless_worker_dialogue_decision(
                context,
                config=config,
                tool_name=normalized_tool,
                tool_args=args,
            )
            if worker_decision is not None:
                return _annotate_authority_denial(
                    worker_decision,
                    tool_name=tool_name,
                    tool_definition=tool_definition,
                    layer="worker_scope",
                    required_capabilities=tuple(sorted(tool_capabilities)),
                )
            worker_dialogue_lane = True
        else:
            if dialogue_role == "root" or scope_kind in _ROOT_SCOPE_KINDS:
                root_decision = await _projectless_root_dialogue_decision(
                    context,
                    config=config,
                )
            else:
                root_decision = await _generic_agent_dialogue_controller_decision(
                    context,
                    tool_name=normalized_tool,
                    config=config,
                )
            if root_decision is not None:
                return _annotate_authority_denial(
                    root_decision,
                    tool_name=tool_name,
                    tool_definition=tool_definition,
                    layer="root_scope",
                    required_capabilities=tuple(sorted(tool_capabilities)),
                )
        # Child Project/Task targets are deliberately left to the dialogue
        # service, which owns human Project ACLs and worker grants.  The Root
        # or worker proof above is only the controller admission boundary.
    elif normalized_tool in _AGENT_DIALOGUE_QUESTION_TOOLS and projectless_turn:
        if dialogue_role == "worker":
            worker_decision = await _projectless_worker_dialogue_decision(
                context,
                config=config,
                tool_name=normalized_tool,
                tool_args=args,
            )
            if worker_decision is not None:
                return _annotate_authority_denial(
                    worker_decision,
                    tool_name=tool_name,
                    tool_definition=tool_definition,
                    layer="worker_scope",
                    required_capabilities=tuple(sorted(tool_capabilities)),
                )
            worker_dialogue_lane = True
        elif dialogue_role == "root" or scope_kind in _ROOT_SCOPE_KINDS:
            root_decision = await _projectless_root_dialogue_decision(
                context,
                config=config,
            )
            if root_decision is not None:
                return _annotate_authority_denial(
                    root_decision,
                    tool_name=tool_name,
                    tool_definition=tool_definition,
                    layer="root_scope",
                    required_capabilities=tuple(sorted(tool_capabilities)),
                )
        else:
            return _annotate_authority_denial(
                AgentToolAuthorityDecision(
                    False,
                    "projectless question requires a canonical AgentDialogue scope",
                ),
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="run_scope",
                required_capabilities=tuple(sorted(tool_capabilities)),
                reason_code="run_scope_missing",
            )
        # A non-Dialogue projectless Agent follows the ordinary capability and
        # planning gates below; this branch only admits a canonical Dialogue
        # worker/Root question.
    if not worker_dialogue_lane and not (
        normalized_tool in _AGENT_DIALOGUE_TOOLS
        and projectless_turn
    ):
        memory_scope_decision = await _memory_scope_decision(context, normalized_tool, args)
        if memory_scope_decision is not None and not memory_scope_decision.allowed:
            return _annotate_authority_denial(
                memory_scope_decision,
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="memory_scope",
                required_capabilities=tuple(sorted(tool_capabilities)),
            )
        scope_decision = _project_scope_decision(
            context,
            (
                {}
                if normalized_tool
                in {"delegate", "delegate_agent_work", "delegate_plan", "delegate_worker"}
                else args
            ),
            memory_tool=normalized_tool in _AGENT_MEMORY_TOOLS,
        )
        if scope_decision is not None:
            return _annotate_authority_denial(
                scope_decision,
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="project_scope",
                required_capabilities=tuple(sorted(tool_capabilities)),
            )
        resource_decision = await _resource_scope_decision(
            context,
            (
                {}
                if normalized_tool
                in {"delegate", "delegate_agent_work", "delegate_plan", "delegate_worker"}
                else args
            ),
        )
        if resource_decision is not None:
            return _annotate_authority_denial(
                resource_decision,
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="resource_scope",
                required_capabilities=tuple(sorted(tool_capabilities)),
            )
    subagent_decision = _subagent_decision(context, normalized_tool, args, config)
    if subagent_decision is not None and not subagent_decision.allowed:
        return _annotate_authority_denial(
            subagent_decision,
            tool_name=tool_name,
            tool_definition=tool_definition,
            layer="subagent_scope",
            required_capabilities=tuple(sorted(subagent_decision.required_capabilities)),
        )

    if normalized_tool == "agent_team_delegate" and subagent_decision is not None:
        capabilities = frozenset(subagent_decision.required_capabilities)
    elif normalized_tool == "delegate_worker":
        capabilities = _delegate_worker_required_capabilities(args)
    else:
        capabilities = agent_capability_ids_for_tool(normalized_tool, tool_definition)
    if not capabilities:
        owner = str(getattr(tool_definition, "owner", "") or "").strip().casefold()
        reason_code = "tool_owner_unknown" if owner and owner not in {
            "core",
            "aoi_tools",
            "tool_packs",
        } else "capability_unresolved"
        return _annotate_authority_denial(
            AgentToolAuthorityDecision(False, "Unknown Agent capability/resource; execution is blocked"),
            tool_name=tool_name,
            tool_definition=tool_definition,
            layer="capability_resolver",
            reason_code=reason_code,
        )
    resolver = AgentAuthorityResolver(config=config)
    reasons: list[str] = []
    allowed_capabilities: list[str] = []
    for capability in sorted(capabilities):
        try:
            decision = await resolver.resolve(
                agent_id=agent_id,
                revision_id=revision_id,
                project_id=getattr(context, "project_id", None),
                required_capability=capability,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _annotate_authority_denial(
                AgentToolAuthorityDecision(
                    False,
                    "Agent authority could not be revalidated",
                    capability,
                    tuple(sorted(capabilities)),
                ),
                tool_name=tool_name,
                tool_definition=tool_definition,
                layer="capability_resolver",
                reason_code="capability_revalidation_failed",
            )
        if decision.allowed:
            allowed_capabilities.append(capability)
            continue
        reasons.extend(str(item) for item in decision.deny_reasons if str(item))
    # A tool may belong to more than one authority family (for example an
    # arbitrary command is both workspace_write and command_execute).  The
    # Agent/human intersection is valid only when every family is still
    # granted.  Returning on the first allowed family would turn a revoked
    # capability into a fail-open native/provider execution.
    if len(allowed_capabilities) == len(capabilities):
        return AgentToolAuthorityDecision(
            True,
            capability=allowed_capabilities[0] if allowed_capabilities else None,
        )
    reason = reasons[0] if reasons else "Agent authority intersection denied this tool"
    return _annotate_authority_denial(
        AgentToolAuthorityDecision(
            False,
            reason,
            sorted(capabilities)[0],
            tuple(sorted(capabilities)),
        ),
        tool_name=tool_name,
        tool_definition=tool_definition,
        layer="capability_resolver",
        reason_code=_authority_reason_code(
            reason,
            fallback="capability_not_in_ceiling",
        ),
    )


async def guard_agent_native_capabilities_async(
    capabilities: set[str] | tuple[str, ...] | list[str] | frozenset[str],
    *,
    config: Any = None,
) -> AgentToolAuthorityDecision:
    """Revalidate the authority families exposed by a native CLI invocation.

    Native shell/apply-patch/MCP surfaces do not pass through an AoiTalk
    ``ToolDefinition``.  Map the finite provider surface to the same central
    guard so native capability checks use the exact AgentRevision and current
    grants as managed tools.
    """

    native_tool_by_capability = {
        "workspace_read": "read_file",
        "workspace_write": "edit_file",
        "command_execute": "execute_command",
    }
    for capability in sorted({str(item).strip() for item in capabilities if str(item).strip()}):
        tool_name = native_tool_by_capability.get(capability)
        if tool_name is None:
            return AgentToolAuthorityDecision(
                False,
                "Unknown native Agent capability; execution is blocked",
                capability,
            )
        decision = await guard_agent_tool_call_async(
            tool_name,
            {},
            config=config,
        )
        if not decision.allowed:
            return decision
    return AgentToolAuthorityDecision(True)


def guard_agent_native_capabilities_sync(
    capabilities: set[str] | tuple[str, ...] | list[str] | frozenset[str],
    *,
    config: Any = None,
) -> AgentToolAuthorityDecision:
    """Synchronous bridge for the provider/native CLI pre-execution gate."""

    coroutine = guard_agent_native_capabilities_async(capabilities, config=config)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coroutine).result()


def guard_agent_tool_call_sync(
    tool_name: str,
    tool_args: Mapping[str, Any] | None = None,
    *,
    tool_definition: Any = None,
    config: Any = None,
) -> AgentToolAuthorityDecision:
    """Synchronous bridge used by native/CLI tool lanes.

    When called from an event loop, the existing context is copied into a
    short-lived worker thread so the async DB resolver can run without nesting
    event loops or treating a coroutine object as an allow decision.
    """

    coroutine = guard_agent_tool_call_async(
        tool_name,
        tool_args,
        tool_definition=tool_definition,
        config=config,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coroutine).result()


__all__ = [
    "AgentToolAuthorityDecision",
    "guard_agent_native_capabilities_async",
    "guard_agent_native_capabilities_sync",
    "guard_agent_tool_call_async",
    "guard_agent_tool_call_sync",
]
