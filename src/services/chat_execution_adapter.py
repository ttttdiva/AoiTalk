"""Request-scoped execution adapter for Agent-backed Chat turns.

The adapter is deliberately small: conversation identity is resolved from the
durable session/run boundary, while provider construction and work-lease
bookkeeping remain transport concerns.  Character and Persona state never
participate in this identity.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import weakref
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .agent_role_context import build_agent_role_context
from .agent_model_routing import (
    AgentModelRoute,
    AgentModelRouteError,
    AgentRoutingPins,
    resolve_agent_role_route,
    safe_route_projection,
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
    AgentExecutionBinding,
    AgentPrivateRoot,
    AgentRevision,
    AgentRun,
    AgentWorkItem,
    ConversationMessage,
    ConversationSession,
)
from .turn_context import (
    TurnContext,
    get_turn_context,
    reset_turn_context,
    set_turn_context,
)
from .agent_delegate_policy import revision_has_new_delegate_policy
from .agent_revision_v2 import revision_delegate_policy, revision_tool_ids

logger = logging.getLogger(__name__)

_AOI_LEGACY_FALLBACK_WARNING_EMITTED: ContextVar[bool] = ContextVar(
    "aoi_chat_legacy_fallback_warning_emitted",
    default=False,
)

AOIDOTS_MAX_OUTPUT_TOKENS = 8192

if TYPE_CHECKING:
    from .conversation_agent_service import AgentExecutionBindingSnapshot


class AgentDialogueBudgetUnsupported(RuntimeError):
    """The selected provider cannot enforce a configured Dialogue budget."""

    code = "agent_dialogue_budget_unsupported"

    def __init__(self, message: str = "provider cannot enforce the configured AgentDialogue budget") -> None:
        super().__init__(message)


class InteractiveNativeRunRoutePersistenceError(RuntimeError):
    """A resolved native CLI route could not be pinned to its interactive Run."""

    code = "agent_run_scope_route_persistence_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = str(code or self.code)[:96]


_PERSONAL_DEADLINE_LOCAL_PROVIDERS = frozenset(
    {"openai_compatible_local", "ollama", "sglang"}
)
_PERSONAL_DEADLINE_PROOF_ATTRIBUTE = "_personal_deadline_caller_route_proof"
_PERSONAL_DEADLINE_PROOF_ISSUER = object()
_PERSONAL_DEADLINE_FALLBACK_ROUTE_SOURCES = frozenset(
    {"standard_model", "legacy_profile", "legacy_policy_override", "main_inherit"}
)
_PERSONAL_DEADLINE_PROFILE_ID = "personal-firearms-admin"
_PERSONAL_DEADLINE_CAPABILITY = "personal_deadline_draft"
_PERSONAL_DEADLINE_TOOL_GATE = "personal.deadline_chat_draft.enabled"


@dataclass(frozen=True, slots=True)
class PersonalDeadlineCallerRouteProof:
    """Immutable server proof for one personal local Chat caller."""

    owner_user_id: str
    session_id: str
    message_id: str
    agent_id: str
    agent_revision_id: str
    agent_role: str
    binding_id: str
    binding_snapshot_hash: str
    binding_object_id: int
    route_source: str
    route_effort: str
    route_backend: str
    route_inherits_main: bool
    route_notes: tuple[str, ...]
    provider: str
    model: str
    base_url: str
    client_object_id: int
    route_candidates_json: str = "{}"
    _binding_ref: weakref.ReferenceType[Any] | None = field(
        default=None, repr=False, compare=False
    )
    _client_ref: weakref.ReferenceType[Any] | None = field(
        default=None, repr=False, compare=False
    )
    _issuer_token: object | None = field(default=None, repr=False, compare=False)

def _clear_personal_deadline_caller_route_proof(
    execution: Any | None,
    *,
    source_client: Any | None = None,
) -> None:
    """Clear stale request-local proof markers before a new build."""

    binding = getattr(execution, "binding", None)
    for target in (binding, source_client):
        if target is None:
            continue
        for name in (
            _PERSONAL_DEADLINE_PROOF_ATTRIBUTE,
        ):
            try:
                object.__delattr__(target, name)
            except (AttributeError, TypeError):
                try:
                    delattr(target, name)
                except (AttributeError, TypeError):
                    pass


def _clear_private_marker(target: Any, name: str) -> None:
    if target is None:
        return
    try:
        object.__delattr__(target, name)
    except (AttributeError, TypeError):
        try:
            delattr(target, name)
        except (AttributeError, TypeError):
            pass


def _personal_deadline_endpoint(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return ""
        host = parsed.hostname.rstrip(".").casefold()
        host = host if ":" not in host else f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (parsed.scheme.casefold(), f"{host}{port}", parsed.path.rstrip("/"), "", "")
        )
    except (TypeError, ValueError):
        return ""


def _personal_deadline_client_endpoint(client: Any) -> str:
    value = getattr(client, "base_url", None)
    if not value:
        value = getattr(getattr(client, "_openai_client", None), "base_url", None)
    if not value:
        value = getattr(getattr(client, "client", None), "base_url", None)
    return _personal_deadline_endpoint(value)


def _personal_deadline_client_provider(client: Any) -> str:
    value = getattr(client, "provider_label", None)
    if not value:
        value = getattr(getattr(client, "_turn_runner", None), "provider_label", None)
    return str(value or "").strip().casefold()


def _personal_deadline_client_model(client: Any) -> str:
    return str(getattr(client, "model_name", "") or "").strip()


def _personal_deadline_cloud_context_is_default() -> bool:
    """Reject any ambient Cloud Advisor parent scope, including ContextVars."""

    try:
        from .cloud_advisor_service import (
            CloudAdvisorEscalationAssessment,
            CloudAdvisorTriggerOrigin,
            get_cloud_advisor_invocation_context,
        )

        invocation = get_cloud_advisor_invocation_context()
        return (
            type(invocation.origin) is CloudAdvisorTriggerOrigin
            and invocation.origin is CloudAdvisorTriggerOrigin.MAIN_AGENT
            and type(invocation.assessment) is CloudAdvisorEscalationAssessment
            and invocation.assessment == CloudAdvisorEscalationAssessment()
        )
    except Exception:
        return False


def _route_contract_json(route: Mapping[str, Any]) -> str:
    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        return value

    return json.dumps(plain({key: route[key] for key in (
        "candidate_routes", "allow_paid_fallback", "fallback_on"
    ) if key in route}), sort_keys=True, separators=(",", ":"))


def _personal_deadline_route_snapshot_matches(binding: Any, route: Any) -> bool:
    snapshot = getattr(binding, "route_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return False
    role = str(getattr(route, "role", "") or "").strip().casefold()
    roles = snapshot.get("roles", snapshot)
    frozen = roles.get(role) if isinstance(roles, Mapping) else None
    if not isinstance(frozen, Mapping) or frozen.get("error"):
        return False
    actual = route.provenance()
    if _route_contract_json(frozen) != _route_contract_json(actual):
        return False
    for name, expected in frozen.items():
        if name in {"candidate_routes", "allow_paid_fallback", "fallback_on"}:
            continue
        if name == "effort":
            if str(actual.get(name) or "") != str(expected or ""):
                return False
            continue
        if name == "notes":
            if list(actual.get(name) or []) != list(expected or []):
                return False
            continue
        if actual.get(name) != expected:
            return False
    return all(
        actual.get(name) == frozen.get(name)
        for name in (
            "agent_id",
            "agent_revision_id",
            "role",
            "provider",
            "model",
            "route_source",
            "inherits_main",
            "backend",
        )
    )


def _set_personal_deadline_marker(target: Any, name: str, value: Any) -> bool:
    try:
        object.__setattr__(target, name, value)
        return True
    except Exception:
        try:
            setattr(target, name, value)
            return True
        except Exception:
            return False


def _personal_deadline_route_ready(
    *,
    ambient_turn: Any,
    execution: Any,
    client: Any,
    config: Any,
    session_id: str | None,
    project_id: str | None,
    require_client_scope: bool = True,
) -> tuple[dict[str, str], str] | None:
    try:
        from .conversation_agent_service import AgentExecutionBindingSnapshot
    except Exception:
        return None
    if (
        type(ambient_turn) is not TurnContext
        or type(execution) is not ChatAgentExecution
        or type(execution.binding) is not AgentExecutionBindingSnapshot
        or type(execution.route) is not AgentModelRoute
        or ambient_turn.agent_execution_binding is not execution.binding
    ):
        return None
    owner = str(ambient_turn.user_id or "").strip()
    session = str(ambient_turn.session_id or "").strip()
    message = str(ambient_turn.message_id or "").strip()
    if not owner or not session or not message:
        return None
    if session_id and str(session_id).strip() != session:
        return None
    if project_id or ambient_turn.project_id is not None:
        return None
    if ambient_turn.context_project_hint_id:
        return None
    if (
        ambient_turn.cloud_advisor_origin is not None
        or ambient_turn.cloud_advisor_assessment is not None
    ):
        return None
    if not _personal_deadline_cloud_context_is_default():
        return None

    binding = execution.binding
    route = execution.route
    binding_id = str(binding.binding_id or "").strip()
    binding_hash = str(binding.snapshot_hash or "").strip()
    agent_id = str(binding.agent_id or "").strip()
    revision_id = str(binding.agent_revision_id or "").strip()
    if (
        not all((binding_id, binding_hash, agent_id, revision_id))
        or str(binding.session_id or "").strip() != session
        or str(route.agent_id or "").strip() != agent_id
        or str(route.agent_revision_id or "").strip() != revision_id
    ):
        return None
    role = str(route.role or "").strip()
    provider = str(route.provider or "").strip().casefold()
    model = str(route.model or "").strip()
    route_source = str(route.route_source or "").strip()
    route_effort = str(route.effort or "").strip()
    route_backend = str(route.backend or "").strip().casefold()
    route_inherits_main = bool(route.inherits_main)
    route_notes = tuple(str(item) for item in route.notes)
    if (
        role != "operator"
        or provider not in _PERSONAL_DEADLINE_LOCAL_PROVIDERS
        or not model
        or route_backend != "api"
        or bool(route.inherits_main)
        or route_source.casefold() in _PERSONAL_DEADLINE_FALLBACK_ROUTE_SOURCES
        or "fallback" in route_source.casefold()
        or not _personal_deadline_route_snapshot_matches(binding, route)
    ):
        return None
    for name, expected in (
        ("agent_id", agent_id),
        ("agent_revision_id", revision_id),
        ("agent_role", role),
    ):
        observed = str(getattr(ambient_turn, name, "") or "").strip()
        if not observed:
            return None
        if name == "agent_role":
            if observed.casefold() != expected.casefold():
                return None
        elif observed != expected:
            return None

    actual_provider = _personal_deadline_client_provider(client)
    actual_model = _personal_deadline_client_model(client)
    actual_endpoint = _personal_deadline_client_endpoint(client)
    if actual_provider != provider or actual_model != model or not actual_endpoint:
        return None
    try:
        from .personal_agent_controller import _resolve_local_provider_endpoint
        from .outbound_privacy_service import (
            OutboundPrivacyGateway,
            provider_classification,
        )

        expected_endpoint = _personal_deadline_endpoint(
            _resolve_local_provider_endpoint(config, route)
        )
        if expected_endpoint != actual_endpoint:
            return None
        if provider_classification(provider, base_url=actual_endpoint) != "local":
            return None
        gateway = getattr(client, "_privacy_gateway", None)
        if gateway is None:
            gateway = getattr(
                getattr(client, "_turn_runner", None),
                "privacy_gateway",
                None,
            )
        if not isinstance(gateway, OutboundPrivacyGateway):
            gateway = OutboundPrivacyGateway(config)
        if gateway.provider_class(provider, base_url=actual_endpoint) != "local":
            return None
    except Exception:
        return None
    workspace = execution.workspace
    if workspace is not None and workspace.project_id is not None:
        return None
    client_project = str(getattr(client, "current_project_id", "") or "").strip()
    client_session = str(getattr(client, "current_session_id", "") or "").strip()
    client_owner = str(getattr(client, "session_user_id", "") or "").strip()
    if require_client_scope and (
        client_owner != owner
        or client_session != session
        or client_project
    ):
        return None
    if not require_client_scope and (
        client_project or (client_session and client_session != session)
    ):
        return None
    return (
        {
            "owner_user_id": owner,
            "session_id": session,
            "message_id": message,
            "agent_id": agent_id,
            "agent_revision_id": revision_id,
            "agent_role": role,
            "route_source": route_source,
            "route_effort": route_effort,
            "route_backend": route_backend,
            "route_inherits_main": route_inherits_main,
            "route_notes": route_notes,
            "route_candidates_json": _route_contract_json(route.provenance()),
            "binding_id": binding_id,
            "binding_snapshot_hash": binding_hash,
            "provider": provider,
            "model": model,
            "base_url": actual_endpoint,
        },
        actual_endpoint,
    )


def _issue_personal_deadline_caller_route_proof(
    *,
    ambient_turn: Any,
    execution: Any,
    client: Any,
    config: Any,
    session_id: str | None,
    project_id: str | None,
) -> PersonalDeadlineCallerRouteProof | None:
    ready = _personal_deadline_route_ready(
        ambient_turn=ambient_turn,
        execution=execution,
        client=client,
        config=config,
        session_id=session_id,
        project_id=project_id,
    )
    if ready is None:
        return None
    values, _ = ready
    try:
        binding_ref = weakref.ref(execution.binding)
        client_ref = weakref.ref(client)
    except TypeError:
        return None
    proof = PersonalDeadlineCallerRouteProof(
        **values,
        binding_object_id=id(execution.binding),
        client_object_id=id(client),
        _binding_ref=binding_ref,
        _client_ref=client_ref,
        _issuer_token=_PERSONAL_DEADLINE_PROOF_ISSUER,
    )
    if not _set_personal_deadline_marker(
        execution.binding,
        _PERSONAL_DEADLINE_PROOF_ATTRIBUTE,
        proof,
    ):
        return None
    if not _set_personal_deadline_marker(
        client,
        _PERSONAL_DEADLINE_PROOF_ATTRIBUTE,
        proof,
    ):
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return None
    return proof


def _personal_deadline_live_route_matches(
    proof: PersonalDeadlineCallerRouteProof,
    turn: Any,
) -> bool:
    if proof._issuer_token is not _PERSONAL_DEADLINE_PROOF_ISSUER:
        return False
    binding = getattr(turn, "agent_execution_binding", None)
    if binding is None or id(binding) != proof.binding_object_id:
        return False
    if proof._binding_ref() is not binding:
        return False
    if (
        str(binding.binding_id or "").strip() != proof.binding_id
        or str(binding.snapshot_hash or "").strip() != proof.binding_snapshot_hash
        or str(binding.agent_id or "").strip() != proof.agent_id
        or str(binding.agent_revision_id or "").strip() != proof.agent_revision_id
    ):
        return False
    if (
        str(getattr(turn, "user_id", "") or "").strip() != proof.owner_user_id
        or str(getattr(turn, "session_id", "") or "").strip() != proof.session_id
        or str(getattr(turn, "message_id", "") or "").strip() != proof.message_id
        or str(getattr(turn, "agent_id", "") or "").strip() != proof.agent_id
        or str(getattr(turn, "agent_revision_id", "") or "").strip()
        != proof.agent_revision_id
        or str(getattr(turn, "agent_role", "") or "").strip()
        != proof.agent_role
        or getattr(turn, "project_id", None) is not None
        or str(getattr(turn, "context_project_hint_id", "") or "").strip()
        or getattr(turn, "cloud_advisor_origin", None) is not None
        or getattr(turn, "cloud_advisor_assessment", None) is not None
    ):
        return False
    if not _personal_deadline_cloud_context_is_default():
        return False
    client = proof._client_ref()
    if (
        client is None
        or id(client) != proof.client_object_id
        or getattr(client, _PERSONAL_DEADLINE_PROOF_ATTRIBUTE, None) is not proof
        or getattr(client, "_agent_execution_binding", None) is not binding
        or _personal_deadline_client_provider(client) != proof.provider
        or _personal_deadline_client_model(client) != proof.model
        or _personal_deadline_client_endpoint(client) != proof.base_url
        or str(getattr(client, "session_user_id", "") or "").strip()
        != proof.owner_user_id
        or str(getattr(client, "current_project_id", "") or "").strip()
        or str(getattr(client, "current_session_id", "") or "").strip()
        != proof.session_id
    ):
        return False
    route = getattr(client, "_agent_route", None)
    if not isinstance(route, Mapping):
        return False
    expected_route = {
        "agent_id": proof.agent_id,
        "agent_revision_id": proof.agent_revision_id,
        "role": proof.agent_role,
        "provider": proof.provider,
        "model": proof.model,
        "effort": proof.route_effort,
        "route_source": proof.route_source,
        "inherits_main": proof.route_inherits_main,
        "backend": proof.route_backend,
    }
    for name, expected in expected_route.items():
        observed = route.get(name)
        if name == "effort":
            if str(observed or "") != str(expected or ""):
                return False
        elif observed != expected:
            return False
    observed_notes = tuple(str(item) for item in route.get("notes") or ())
    return (observed_notes == proof.route_notes
            and _route_contract_json(route) == proof.route_candidates_json)


def get_personal_deadline_caller_route_proof(
    turn: Any | None = None,
) -> PersonalDeadlineCallerRouteProof | None:
    """Return the live proof for the current exact personal local caller."""

    current = turn if turn is not None else get_turn_context()
    binding = getattr(current, "agent_execution_binding", None)
    proof = getattr(binding, _PERSONAL_DEADLINE_PROOF_ATTRIBUTE, None)
    if not isinstance(proof, PersonalDeadlineCallerRouteProof):
        return None
    try:
        return proof if _personal_deadline_live_route_matches(proof, current) else None
    except Exception:
        return None


def _personal_deadline_gate_enabled(config: Any) -> bool:
    """Only a literal server-config ``True`` enables the chat draft seam."""

    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            if getter(_PERSONAL_DEADLINE_TOOL_GATE, None) is True:
                return True
        except (KeyError, TypeError, AttributeError):
            pass
    if isinstance(config, Mapping):
        value: Any = config
        for part in _PERSONAL_DEADLINE_TOOL_GATE.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return False
            value = value[part]
        return value is True
    return False


def _personal_deadline_turn_has_private_owner_origin(turn: Any) -> bool:
    """Reject project, shared Dialogue, and Cloud Advisor turn origins."""

    if (
        type(turn) is not TurnContext
        or not str(turn.user_id or "").strip()
        or not str(turn.session_id or "").strip()
        or not str(turn.message_id or "").strip()
        or turn.project_id is not None
        or bool(turn.context_project_hint_id)
        or bool(turn.include_project_context)
        or bool(turn.verified_project_attachment)
        or turn.cloud_advisor_origin is not None
        or turn.cloud_advisor_assessment is not None
    ):
        return False
    # AgentDialogue turns carry a shared Human/Agent handoff boundary. This
    # adapter accepts only the owner's private current conversation turn.
    if any(
        getattr(turn, name, None) not in (None, "")
        for name in (
            "agent_dialogue_id",
            "agent_dialogue_role",
            "agent_dialogue_input_id",
            "agent_dialogue_control_revision",
            "agent_dialogue_human_anchor",
            "agent_dialogue_continuation_target",
            "agent_dialogue_typed_resume_answer",
            "agent_dialogue_typed_resume_answer_id",
        )
    ):
        return False
    try:
        from ..llm.tool_policy import get_current_shared_resource_scope

        if get_current_shared_resource_scope() is not None:
            return False
    except Exception:
        return False
    return _personal_deadline_cloud_context_is_default()


def _personal_deadline_controller_execution_matches(
    execution: Any,
    selected: Any,
    client: Any,
) -> bool:
    """Match the Chat build against a fresh profile/capability resolution."""

    try:
        from .agent_authority import AuthorityDecision
        from .agent_dialogue_workspace import AgentDialogueWorkspace
        from .conversation_agent_service import AgentExecutionBindingSnapshot
        from .personal_agent_controller import PersonalAgentExecution
    except ImportError:
        return False
    if (
        type(execution) is not ChatAgentExecution
        or type(execution.binding) is not AgentExecutionBindingSnapshot
        or type(execution.route) is not AgentModelRoute
        or execution.root_scope_marker is not None
        or type(selected) is not PersonalAgentExecution
        or type(selected.binding_snapshot) is not AgentExecutionBindingSnapshot
        or type(selected.authority) is not AuthorityDecision
        or type(selected.route) is not AgentModelRoute
    ):
        return False
    binding = execution.binding
    resolved = selected.binding_snapshot
    authority = selected.authority
    turn = get_turn_context()
    workspace = execution.workspace
    if getattr(client, "_agent_dialogue_workspace", None) is not workspace:
        return False
    if workspace is not None and (
        type(workspace) is not AgentDialogueWorkspace
        or workspace.session_id != str(turn.session_id or "")
        or workspace.agent_id != str(binding.agent_id)
        or workspace.owner_id != str(turn.user_id or "")
        or workspace.project_id is not None
        or workspace.resource_key is not None
        or not workspace.readonly
        or workspace.native_allowed
        or workspace.run_id
        != str(getattr(client, "_agent_run_id", "") or "")
    ):
        return False
    capability_ceiling = tuple(
        str(item or "").strip().casefold() for item in binding.capability_ceiling
    )
    if (
        len(capability_ceiling) != 1
        or capability_ceiling != (_PERSONAL_DEADLINE_CAPABILITY,)
        or selected.profile_id != _PERSONAL_DEADLINE_PROFILE_ID
        or selected.required_capability != _PERSONAL_DEADLINE_CAPABILITY
        or type(turn) is not TurnContext
        or selected.owner_id != str(turn.user_id or "")
        or selected.session_id != str(binding.session_id)
        or selected.agent_id != str(binding.agent_id)
        or selected.agent_revision_id != str(binding.agent_revision_id)
        or selected.binding_id != str(binding.binding_id or "")
        or selected.revision_version != int(binding.revision_version)
        or resolved.session_id != str(binding.session_id)
        or resolved.agent_id != str(binding.agent_id)
        or resolved.agent_revision_id != str(binding.agent_revision_id)
        or resolved.binding_id != str(binding.binding_id or "")
        or resolved.snapshot_hash != binding.snapshot_hash
        or tuple(
            str(item or "").strip().casefold()
            for item in resolved.capability_ceiling
        ) != (_PERSONAL_DEADLINE_CAPABILITY,)
        or selected.route.provenance() != execution.route.provenance()
        or selected.route.role != "operator"
        or selected.route.backend != "api"
        or selected.route.inherits_main
        or authority.allowed is not True
        or authority.requested_capability != _PERSONAL_DEADLINE_CAPABILITY
        or authority.capabilities != frozenset({_PERSONAL_DEADLINE_CAPABILITY})
        or authority.agent_id != str(binding.agent_id)
        or authority.agent_revision_id != str(binding.agent_revision_id)
        or authority.project_id is not None
        or authority.space_id is not None
        or authority.organization_id is not None
        or authority.persona_id is not None
    ):
        return False
    return True


async def activate_personal_deadline_current_turn_tool_after_persist(
    client: Any,
    execution: Any,
    *,
    config: Any,
    session_id: str | None,
    project_id: str | None,
) -> bool:
    """Expose the one personal deadline candidate tool after a canonical row exists.

    TerminalMode calls this only after it has bound the persisted user-message
    UUID into TurnContext (or has received the already-persisted UUID). All
    identities come from that trusted current turn and the pinned execution;
    this hook accepts no owner, message, proposal, or date arguments.
    """

    if not _personal_deadline_gate_enabled(config):
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False

    turn = get_turn_context()
    execution_binding = getattr(execution, "binding", None)
    execution_route = getattr(execution, "route", None)
    if (
        not _personal_deadline_turn_has_private_owner_origin(turn)
        or get_turn_context() is not turn
        or project_id is not None
        or str(session_id or "").strip() != str(turn.session_id or "").strip()
        or str(getattr(turn, "agent_role", "") or "").strip().casefold()
        != "operator"
        or str(getattr(turn, "agent_id", "") or "").strip()
        != str(getattr(execution_binding, "agent_id", "") or "").strip()
        or str(getattr(turn, "agent_revision_id", "") or "").strip()
        != str(getattr(execution_binding, "agent_revision_id", "") or "").strip()
        or getattr(turn, "agent_execution_binding", None)
        is not execution_binding
        or getattr(turn, "agent_execution_config", None) is not config
        or str(getattr(client, "agent_id", "") or "").strip()
        != str(getattr(execution_binding, "agent_id", "") or "").strip()
        or str(getattr(client, "agent_revision_id", "") or "").strip()
        != str(getattr(execution_binding, "agent_revision_id", "") or "").strip()
        or getattr(client, "_agent_execution_binding", None)
        is not execution_binding
        or getattr(client, "_agent_dialogue_workspace", None)
        is not getattr(execution, "workspace", None)
        or type(execution) is not ChatAgentExecution
        or type(execution_route) is not AgentModelRoute
        or str(execution_route.role or "").strip().casefold() != "operator"
        or str(execution_route.backend or "").strip().casefold() != "api"
        or bool(execution_route.inherits_main)
        or set(
            str(item or "").strip().casefold()
            for item in getattr(execution_binding, "capability_ceiling", ())
        )
        != {_PERSONAL_DEADLINE_CAPABILITY}
        or getattr(execution, "root_scope_marker", None) is not None
        or not isinstance(getattr(client, "_agent_route", None), Mapping)
        or getattr(client, "_agent_route", None)
        != execution_route.provenance()
    ):
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False
    try:
        UUID(str(turn.message_id))
        owner_id = str(UUID(str(turn.user_id)))
        sid = str(UUID(str(turn.session_id)))
    except (TypeError, ValueError, AttributeError):
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False

    # These markers are rebound only on the isolated, server-built target
    # after its binding and route have matched the exact trusted turn.
    try:
        client.session_user_id = owner_id
        client.current_session_id = sid
        client.current_project_id = None
        client.current_include_project_context = False
    except Exception:
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False

    try:
        from .conversation_agent_service import AgentExecutionBindingSnapshot
    except ImportError:
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False

    if (
        type(execution) is not ChatAgentExecution
        or type(execution.binding) is not AgentExecutionBindingSnapshot
        or type(execution.route) is not AgentModelRoute
        or not _personal_deadline_route_ready(
            ambient_turn=turn,
            execution=execution,
            client=client,
            config=config,
            session_id=sid,
            project_id=None,
        )
    ):
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False

    try:
        from .personal_agent_controller import (
            PersonalAgentExecutionResolver,
            _resolve_local_provider_endpoint,
        )
        from .personal_agent_tools import (
            PersonalAgentTools,
            bind_personal_agent_local_route,
        )
        from .personal_deadline_chat_draft import PersonalDeadlineChatDraftAdapter
        from .personal_draft_service import PersonalDraftService
        from .personal_ledger_service import (
            LedgerPermissionError,
            PersonalLedgerService,
        )
        from .outbound_privacy_service import OutboundPrivacyGateway

        db_manager = get_database_manager()
        resolver = PersonalAgentExecutionResolver(db_manager, config=config)
        initial = await resolver.resolve(
            owner_id=owner_id,
            session_id=sid,
            profile_id=_PERSONAL_DEADLINE_PROFILE_ID,
            required_capability=_PERSONAL_DEADLINE_CAPABILITY,
        )
        if (
            get_turn_context() is not turn
            or not _personal_deadline_gate_enabled(config)
            or not _personal_deadline_turn_has_private_owner_origin(turn)
            or not _personal_deadline_controller_execution_matches(
                execution,
                initial,
                client,
            )
            or _personal_deadline_endpoint(initial.base_url)
            != _personal_deadline_client_endpoint(client)
            or _personal_deadline_endpoint(
                _resolve_local_provider_endpoint(config, execution.route)
            )
            != _personal_deadline_endpoint(initial.base_url)
        ):
            _clear_personal_deadline_caller_route_proof(
                execution,
                source_client=client,
            )
            return False

        _issue_personal_deadline_caller_route_proof(
            ambient_turn=turn,
            execution=execution,
            client=client,
            config=config,
            session_id=sid,
            project_id=None,
        )
        proof = get_personal_deadline_caller_route_proof(turn)
        if proof is None:
            return False

        gateway = OutboundPrivacyGateway(
            config,
            user_id=owner_id,
            session_id=sid,
            session_context={"privacy_mode": "local_only"},
            project_metadata=None,
        )

        async def current_authority_guard():
            if (
                not _personal_deadline_gate_enabled(config)
                or get_turn_context() is not turn
                or not _personal_deadline_turn_has_private_owner_origin(turn)
                or get_personal_deadline_caller_route_proof(turn) is not proof
            ):
                raise LedgerPermissionError("personal deadline turn authority changed")
            current = await resolver.resolve(
                owner_id=owner_id,
                session_id=sid,
                profile_id=_PERSONAL_DEADLINE_PROFILE_ID,
                required_capability=_PERSONAL_DEADLINE_CAPABILITY,
            )
            if (
                get_turn_context() is not turn
                or get_personal_deadline_caller_route_proof(turn) is not proof
                or not _personal_deadline_controller_execution_matches(
                    execution,
                    current,
                    client,
                )
                or current.fingerprint() != initial.fingerprint()
            ):
                raise LedgerPermissionError("personal deadline authority was revoked")
            return True

        def tool_available() -> str | None:
            if not _personal_deadline_gate_enabled(config):
                return "personal deadline chat draft is disabled"
            if (
                get_turn_context() is not turn
                or not _personal_deadline_turn_has_private_owner_origin(turn)
                or get_personal_deadline_caller_route_proof(turn) is not proof
                or getattr(client, "_agent_execution_binding", None)
                is not execution.binding
                or getattr(client, "_agent_route", None)
                != execution.route.provenance()
            ):
                return "personal deadline current-turn proof is stale"
            return None

        session_factory = db_manager.get_session
        personal_tools = PersonalAgentTools(
            PersonalLedgerService(session_factory),
            PersonalDraftService(session_factory),
            enabled=True,
        )
        route_context = {
            "profile_id": _PERSONAL_DEADLINE_PROFILE_ID,
            "gateway": gateway,
            "provider": proof.provider,
            "base_url": proof.base_url,
            "allow_external_fallback": False,
            "current_authority_guard": current_authority_guard,
        }
        with bind_personal_agent_local_route(**route_context):
            actor = await personal_tools._authorize_current(
                "private.draft.propose",
                "deadline",
            )
            pinned_guard = personal_tools._pinned_guard(
                "private.draft.propose",
                "deadline",
                actor,
            )
            # The canonical current user row is checked before the schema can
            # become visible. No history/ledger rows are read here.
            await PersonalDeadlineChatDraftAdapter(
                personal_tools
            )._read_current_message(turn, pinned_guard)
            await pinned_guard()

        async def create_personal_deadline_candidate():
            if tool_available() is not None:
                raise LedgerPermissionError("personal deadline current-turn tool is unavailable")
            with bind_personal_agent_local_route(**route_context):
                return await PersonalDeadlineChatDraftAdapter(
                    personal_tools
                ).create_current_turn_draft()

        from ..llm.runtime_tool_registry import (
            register_personal_deadline_current_turn_tool,
        )

        return register_personal_deadline_current_turn_tool(
            client,
            handler=create_personal_deadline_candidate,
            activation_enabled=True,
            availability_check=tool_available,
        )
    except Exception:
        logger.debug(
            "Personal deadline current-turn tool was not exposed",
            exc_info=True,
        )
        _clear_personal_deadline_caller_route_proof(
            execution,
            source_client=client,
        )
        return False


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


from .default_team_graph_service import RoutingContext


@dataclass(frozen=True)
class DefaultRoutingSnapshot:
    proof: InitialAoiDotsRootProof
    context: RoutingContext | None
    status: str


async def _capture_default_routing_snapshot(binding: Any, proof: InitialAoiDotsRootProof, session: Any, db_manager: Any) -> None:
    from .default_team_graph_service import decode_run_snapshot

    context = None
    try:
        run = await session.get(AgentRun, UUID(proof.run_id))
        if run is not None:
            context = decode_run_snapshot(run, proof)
    except Exception:
        pass
    object.__setattr__(binding, "_default_routing_snapshot", DefaultRoutingSnapshot(
        proof, context, "available" if context is not None else "unavailable"
    ))


def is_bound_initial_aoidots_root_turn(turn: Any, binding: Any) -> bool:
    """Check the server-issued Root proof against the live turn and run."""
    if (
        str(getattr(turn, "project_id", "") or "").strip()
        or str(getattr(turn, "agent_dialogue_id", "") or "").strip()
        or str(getattr(turn, "agent_dialogue_role", "") or "").strip()
        or binding is None
        or binding is not getattr(turn, "agent_execution_binding", None)
    ):
        return False
    try:
        from .agent_run_service import get_current_agent_run_id

        proof = getattr(binding, "_initial_aoidots_root_proof", None)
        current_run_id = get_current_agent_run_id()
    except Exception:
        return False
    if not isinstance(proof, InitialAoiDotsRootProof) or not current_run_id:
        return False
    marker = getattr(binding, "_root_scope_marker", None)
    if not isinstance(marker, Mapping) or dict(marker) != proof.marker():
        return False
    return all((
        str(getattr(turn, "user_id", "") or "") == proof.owner_user_id,
        str(getattr(turn, "session_id", "") or "") == proof.session_id,
        str(getattr(turn, "agent_id", "") or "") == proof.agent_id,
        str(getattr(turn, "agent_revision_id", "") or "") == proof.agent_revision_id,
        str(current_run_id) == proof.run_id,
        str(getattr(binding, "agent_id", "") or "") == proof.agent_id,
        str(getattr(binding, "session_id", "") or "") == proof.session_id,
        str(getattr(binding, "agent_revision_id", "") or "") == proof.agent_revision_id,
    ))


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
            if not _project_initial_aoidots_output_cap(client):
                return None
        marker = proof.marker()
        # AgentExecutionBindingSnapshot and ChatAgentExecution are frozen
        # dataclasses, so these private request-local attributes are attached
        # deliberately with object.__setattr__.  The same binding object is
        # already held by TurnContext and the isolated provider client.
        object.__setattr__(binding, "_initial_aoidots_root_proof", proof)
        await _capture_default_routing_snapshot(binding, proof, session, db_manager)
        object.__setattr__(binding, "_root_scope_marker", dict(marker))
        object.__setattr__(execution, "root_scope_marker", dict(marker))
        object.__setattr__(execution, "aoidots_output_cap_eligible", True)
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
    try:
        object.__setattr__(execution, "aoidots_output_cap_eligible", False)
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


def _project_initial_aoidots_output_cap(client: Any) -> bool:
    """Apply the AoiDots cap to an already-built request-local client.

    The direct WebSocket fallback builds its isolated provider client before
    the canonical Human row is persisted.  Its Root proof is therefore only
    complete in ``refresh_initial_aoidots_root_proof``.  Keep the cap on that
    isolated client's config, and fail closed if the config cannot confirm
    the projection.
    """

    target_config = getattr(client, "config", None)
    getter = getattr(target_config, "get", None)
    setter = getattr(target_config, "set", None)
    if not callable(getter) or not callable(setter):
        return False
    try:
        raw_current = getter("runtime.target_max_output_tokens", None)
        current = int(raw_current) if raw_current not in (None, "") else None
        cap = (
            min(current, AOIDOTS_MAX_OUTPUT_TOKENS)
            if current and current > 0
            else AOIDOTS_MAX_OUTPUT_TOKENS
        )
        setter("runtime.target_max_output_tokens", cap)
        projected = getter("runtime.target_max_output_tokens", None)
        return int(projected) == cap
    except Exception:
        return False


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
    # P2 fields are optional during the rolling deployment. ``None`` keeps the
    # existing capability based exposure for legacy revisions; an empty tuple
    # is an intentional "show no tools" policy on a migrated revision.
    tool_ids: tuple[str, ...] | None = None
    model_overrides: Mapping[str, Any] | None = None
    delegate_policy: Mapping[str, Any] | None = None
    # Set only by the durable private-Root / AgentDialogue proof below. This
    # request-local fact scopes the AoiDots output cap without changing global
    # Chat settings or trusting caller metadata.
    aoidots_output_cap_eligible: bool = False
    # Exact caller/worker revision intersection issued by the locked Dialogue.
    # ``None`` is reserved for historical capability-only dialogues.
    dialogue_tool_ids: tuple[str, ...] | None = None
    required_tool_name: str | None = None
    required_tool_graph_context: Mapping[str, Any] | None = None

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
            **(
                {"tool_ids": list(self.tool_ids)}
                if self.tool_ids is not None
                else {}
            ),
        }


async def persist_interactive_native_run_route(
    agent_run_service: Any,
    run_id: str,
    execution: ChatAgentExecution,
) -> Mapping[str, Any]:
    """Persist and verify a resolved native CLI route before issuing its scope.

    ``execution`` is produced by the server-side route resolver. The caller
    invokes this only on the native CLI path, so user-supplied route data is
    never accepted here. A missing or mismatched read-back fails closed.
    """

    if not isinstance(execution, ChatAgentExecution):
        raise InteractiveNativeRunRoutePersistenceError(
            "native CLI route was not resolved by the Agent execution adapter",
            code="agent_run_scope_route_unverified",
        )
    route = execution.route
    provider = str(getattr(route, "provider", "") or "").strip().casefold()
    model = str(getattr(route, "model", "") or "").strip()
    if provider not in {
        "claude-cli",
        "codex-cli",
        "grok-cli",
        "antigravity-cli",
    } or not model:
        raise InteractiveNativeRunRoutePersistenceError(
            "resolved native CLI route is incomplete",
            code="agent_run_scope_route_unverified",
        )

    try:
        saved = await agent_run_service.update_runtime_route(
            run_id,
            provider=provider,
            model=model,
            route_source=str(getattr(route, "route_source", "") or "agent_revision"),
            reasoning_effort=str(getattr(route, "effort", "") or "") or None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise InteractiveNativeRunRoutePersistenceError(
            "resolved native CLI route could not be persisted"
        ) from exc

    if (
        not isinstance(saved, Mapping)
        or str(saved.get("provider") or "").strip().casefold() != provider
        or str(saved.get("model") or "").strip() != model
    ):
        raise InteractiveNativeRunRoutePersistenceError(
            "resolved native CLI route could not be verified"
        )
    return saved


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


async def _is_aoidots_dialogue_worker_run(
    *,
    session: Any,
    dialogue: Any,
    binding: Any,
    run: Any,
    session_id: str,
) -> bool:
    """Prove a worker Run belongs to a live Dialogue from a private AoiDots Root.

    The AgentDialogue's unique worker session is the durable link from the
    receiver Run to its parent Root. All mismatches and unavailable rows fail
    closed so unrelated Dialogue workers retain their existing limits.
    """

    try:
        from sqlalchemy import select

        dialogue_id = _clean(_read(dialogue, "id"))
        owner_id = _clean(_read(dialogue, "owner_user_id"))
        root_session_id = _clean(_read(dialogue, "root_session_id"))
        root_agent_id = _clean(_read(dialogue, "root_agent_id"))
        worker_agent_id = _clean(_read(dialogue, "worker_agent_id"))
        worker_revision_id = _clean(_read(dialogue, "worker_agent_revision_id"))
        run_id = _clean(_read(run, "id"))
        if not all(
            (
                dialogue_id,
                owner_id,
                root_session_id,
                root_agent_id,
                worker_agent_id,
                worker_revision_id,
                run_id,
            )
        ):
            return False
        if (
            _clean(_read(run, "run_type")).casefold() != "chat_turn"
            or _clean(_read(run, "status")).casefold()
            not in {"queued", "running", "awaiting_approval"}
            or _clean(_read(run, "session_id")) != session_id
            or _clean(_read(run, "user_id")) != owner_id
            or _clean(_read(run, "agent_id")) != worker_agent_id
            or _clean(_read(run, "agent_revision_id")) != worker_revision_id
            or _clean(_read(binding, "session_id")) != session_id
            or _clean(_read(binding, "agent_id")) != worker_agent_id
            or _clean(_read(binding, "agent_revision_id")) != worker_revision_id
            or _clean(_read(dialogue, "worker_session_id")) != session_id
            or _clean(_read(dialogue, "worker_agent_id")) != worker_agent_id
            or _clean(_read(dialogue, "worker_agent_revision_id"))
            != worker_revision_id
            or _clean(_read(dialogue, "state")).casefold()
            not in {"queued", "running", "waiting"}
        ):
            return False

        worker_session = await session.get(ConversationSession, UUID(session_id))
        root_session = await session.get(
            ConversationSession,
            UUID(root_session_id),
        )
        if worker_session is None or root_session is None:
            return False
        if (
            _clean(_read(worker_session, "id")) != session_id
            or _clean(_read(worker_session, "user_id")) != owner_id
            or _clean(_read(worker_session, "agent_id")) != worker_agent_id
            or _read(worker_session, "deleted_at") is not None
            or _clean(_read(root_session, "id")) != root_session_id
            or _clean(_read(root_session, "user_id")) != owner_id
            or _clean(_read(root_session, "agent_id")) != root_agent_id
            or bool(_read(root_session, "is_group_chat", False))
            or _read(root_session, "deleted_at") is not None
        ):
            return False

        root_result = await session.execute(
            select(AgentPrivateRoot)
            .where(
                AgentPrivateRoot.session_id == UUID(root_session_id),
                AgentPrivateRoot.agent_id == UUID(root_agent_id),
                AgentPrivateRoot.owner_user_id == UUID(owner_id),
                AgentPrivateRoot.purpose == "aoidots",
                AgentPrivateRoot.state == "active",
            )
            .limit(1)
        )
        private_root = root_result.scalars().first()
        if private_root is not None:
            return bool(
                _clean(_read(private_root, "session_id")) == root_session_id
                and _clean(_read(private_root, "agent_id")) == root_agent_id
                and _clean(_read(private_root, "owner_user_id")) == owner_id
                and _clean(_read(private_root, "purpose")) == "aoidots"
                and _clean(_read(private_root, "state")) == "active"
            )

        # P3 generic Agent-to-Agent delegation keeps this same Dialogue row
        # and worker session lifecycle, but its caller can be any owner-bound
        # Agent session.  The exact source Revision and immutable session
        # binding are required; legacy rows without the P2 tool columns remain
        # private-root-only.
        root_agent = await session.get(Agent, UUID(root_agent_id))
        root_revision = await session.get(AgentRevision, UUID(_clean(_read(dialogue, "root_agent_revision_id"))))
        if (
            root_agent is None
            or str(getattr(root_agent, "state", "")).casefold() != "active"
            or getattr(root_agent, "created_by", None) not in (None, UUID(owner_id))
            or root_revision is None
            or _clean(getattr(root_revision, "agent_id", None)) != root_agent_id
            or not revision_has_new_delegate_policy(root_revision)
        ):
            return False
        if _read(root_session, "project_id") is not None:
            from ..memory.project_repository import ProjectRepository

            if not await ProjectRepository.has_permission(
                session,
                UUID(_clean(_read(root_session, "project_id"))),
                UUID(owner_id),
                "read",
            ):
                return False
        root_binding = (
            await session.execute(
                select(AgentExecutionBinding)
                .where(
                    AgentExecutionBinding.session_id == UUID(root_session_id),
                    AgentExecutionBinding.agent_id == UUID(root_agent_id),
                    AgentExecutionBinding.agent_revision_id
                    == UUID(_clean(_read(dialogue, "root_agent_revision_id"))),
                )
                .order_by(AgentExecutionBinding.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        return bool(
            root_binding is not None
            and _clean(_read(root_binding, "session_id")) == root_session_id
            and _clean(_read(root_binding, "agent_id")) == root_agent_id
            and _clean(_read(root_binding, "agent_revision_id"))
            == _clean(_read(dialogue, "root_agent_revision_id"))
        )
    except Exception:
        return False


def _system_prompt(binding: "AgentExecutionBindingSnapshot") -> str:
    """Compatibility wrapper around the shared immutable role builder."""

    return build_agent_role_context(binding).text


def _chat_route_from_mapping(
    raw_route: Any,
    *,
    pins: Any,
    source: str,
) -> AgentModelRoute | None:
    """Adapt a P2 model-role route to the existing Chat route DTO."""

    if isinstance(raw_route, AgentModelRoute):
        return raw_route.with_role("chat")
    if not isinstance(raw_route, Mapping):
        return None
    # ``kind=pool`` is represented by the existing routing-profile client.
    kind = str(raw_route.get("kind") or "llm").strip().casefold()
    route_notes: tuple[str, ...] = ()
    if kind == "pool":
        provider = "routing-profile"
        pool_id = str(raw_route.get("pool_id") or "").strip()
        model = "free-team"
        if pool_id:
            route_notes = (f"pool_id:{pool_id[:96]}",)
    else:
        provider = str(raw_route.get("provider") or "").strip().casefold()
        model = str(raw_route.get("model") or "").strip()
        pool_id = str(raw_route.get("pool_id") or "").strip()
        if provider == "routing-profile" and pool_id:
            route_notes = (f"pool_id:{pool_id[:96]}",)
    if not provider or not model:
        return None
    effort = str(
        raw_route.get("effort") or raw_route.get("reasoning_effort") or ""
    ).strip()
    acting_subagent = str(raw_route.get("acting_subagent_id") or "").strip()
    backend = str(raw_route.get("backend") or "").strip().casefold()
    if not backend:
        backend = "remote" if provider == "muse-remote" else (
            "cli" if provider.endswith("-cli") else "api"
        )
    return AgentModelRoute(
        agent_team_id=pins.agent_team_id,
        execution_profile_id=pins.execution_profile_id,
        acting_subagent_id=acting_subagent,
        provider=provider,
        model=model,
        effort=effort or None,
        route_source=str(source or "agent_revision")[:96],
        inherits_main=bool(raw_route.get("inherit_model", False)),
        backend=backend,
        role="chat",
        agent_id=pins.agent_id,
        agent_revision_id=pins.agent_revision_id,
        notes=route_notes,
    )


def _route_pool_id(route: Any) -> str:
    """Read a trusted Free Team pool id preserved in route notes."""

    notes = getattr(route, "notes", ())
    for note in notes if isinstance(notes, (list, tuple)) else ():
        text = str(note or "").strip()
        if text.startswith("pool_id:"):
            return text[len("pool_id:") :].strip()[:128]
    return ""


def _session_route_mapping(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    provider = str(value.get("provider") or "").strip().casefold()
    model = str(value.get("model") or "").strip()
    if not provider or not model:
        return None
    effort = str(value.get("effort") or value.get("reasoning_effort") or "").strip()
    result = {"kind": "llm", "provider": provider, "model": model}
    pool_id = str(value.get("pool_id") or "").strip()
    if pool_id:
        result["pool_id"] = pool_id[:128]
    if effort:
        result["effort"] = effort
    return result


def _warn_aoi_legacy_chat_fallback() -> None:
    """Emit one rolling-deployment warning for the current Chat turn."""

    if _AOI_LEGACY_FALLBACK_WARNING_EMITTED.get():
        return
    _AOI_LEGACY_FALLBACK_WARNING_EMITTED.set(True)
    logger.warning(
        "Aoi Chat revision settings unavailable; using legacy Chat execution path"
    )


async def _load_session_chat_route(
    session: Any,
    session_id: str,
) -> Mapping[str, Any] | None:
    """Read the persisted session model picker without changing its state."""

    try:
        row = await session.get(ConversationSession, UUID(session_id))
    except (TypeError, ValueError, AttributeError):
        row = None
    if row is None:
        return None
    try:
        from .conversation_session_selection import (
            read_session_llm_settings,
            session_main_route_override_for_binding,
        )

        settings = read_session_llm_settings(getattr(row, "context", None))
        return session_main_route_override_for_binding(settings)
    except Exception:
        return None


async def resolve_default_chat_agent_runtime(
    *,
    owner_user_id: str | None,
    config: Any | None = None,
    session_route: Mapping[str, Any] | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
) -> Mapping[str, Any] | None:
    """Read the owner's migrated Aoi revision for an ordinary Chat turn.

    This is intentionally a read-only, request-local seam.  Ordinary Chat
    keeps its existing streaming/voice/history path and does not create an
    AgentRun or WorkItem merely to obtain Aoi's route and prompt.  Before P2,
    the nullable fields are absent and this returns ``None`` so callers retain
    the legacy Chat configuration unchanged.
    """

    owner = _clean(owner_user_id)
    if not owner:
        return None
    try:
        owner_uuid = UUID(owner)
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        from sqlalchemy import desc, select

        from .agent_identity_service import aoi_default_agent_idempotency_key
    except (ImportError, AttributeError):
        return None
    try:
        key = aoi_default_agent_idempotency_key(owner_uuid)
    except Exception:
        key = f"private-agent:chat_default:{owner}:v1"
    own = db_session is None
    try:
        session = db_session or await _session_db(db_manager)
    except Exception:
        _warn_aoi_legacy_chat_fallback()
        logger.debug("Aoi default Chat database read was unavailable", exc_info=True)
        return None
    try:
        # Lazy, owner-scoped provisioning is idempotent and uses the same
        # session when the caller already owns one. In a pre-P2 database the
        # row is still created, but the nullable readers below return None and
        # the turn keeps the legacy runtime.
        try:
            from .agent_private_provisioning import AgentPrivateProvisioningService

            await AgentPrivateProvisioningService(
                db_manager=db_manager,
                config=config,
            ).ensure_default_chat_agent(
                owner_uuid,
                config=config,
                # A resolver-owned read session may already have autobegun
                # while provisioning validates the Human owner. Let the
                # provisioner open and commit its own transaction in that
                # case; only reuse an explicitly caller-owned session.
                session_factory=(None if own else session),
            )
        except Exception:
            _warn_aoi_legacy_chat_fallback()
            logger.debug("Aoi default Chat provisioning failed", exc_info=True)
            # Do not continue with an existing/stale Aoi row after a failed
            # provisioning validation. The legacy Chat path is the only safe
            # fallback when the current concrete tool projection is unknown.
            return None
        agent = (
            await session.execute(
                select(Agent)
                .where(
                    Agent.idempotency_key == key,
                    Agent.created_by == owner_uuid,
                    Agent.state == "active",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if agent is None:
            _warn_aoi_legacy_chat_fallback()
            return None
        revision = (
            await session.execute(
                select(AgentRevision)
                .where(AgentRevision.agent_id == agent.id)
                .order_by(desc(AgentRevision.version), desc(AgentRevision.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if revision is None:
            _warn_aoi_legacy_chat_fallback()
            return None
        from .agent_revision_v2 import (
            revision_delegate_policy,
            revision_model_overrides,
            revision_tool_ids,
        )

        tool_ids = revision_tool_ids(revision)
        model_overrides = revision_model_overrides(revision)
        delegate_policy = revision_delegate_policy(revision)
        if tool_ids is None or model_overrides is None or delegate_policy is None:
            _warn_aoi_legacy_chat_fallback()
            return None
        pins = AgentRoutingPins.from_revision(revision)
        route = _resolve_p2_chat_route(
            config,
            revision=revision,
            pins=pins,
            session_route=session_route,
        )
        if route is None:
            _warn_aoi_legacy_chat_fallback()
            return None
        return {
            "agent_id": str(agent.id),
            "agent_revision_id": str(revision.id),
            "revision": revision,
            "route": route,
            "tool_ids": tuple(tool_ids),
            "model_overrides": model_overrides,
            "delegate_policy": delegate_policy,
            "operational_instructions": str(
                getattr(revision, "operational_instructions", "") or ""
            ).strip(),
        }
    except Exception:
        _warn_aoi_legacy_chat_fallback()
        logger.debug("Aoi default Chat runtime read failed", exc_info=True)
        return None
    finally:
        if own:
            await session.close()


def _resolve_p2_chat_route(
    config: Any,
    *,
    revision: Any,
    pins: Any,
    session_route: Mapping[str, Any] | None,
) -> AgentModelRoute | None:
    """Resolve session first, then P2 override, model_roles, and legacy routes."""

    if session_route is not None:
        route = _chat_route_from_mapping(
            _session_route_mapping(session_route),
            pins=pins,
            source="session_main_route",
        )
        if route is not None:
            return route

    try:
        from .agent_revision_v2 import resolve_agent_role_route_v2
    except (ImportError, AttributeError):
        return None
    try:
        resolved = resolve_agent_role_route_v2(config, revision, "chat")
    except Exception:
        logger.debug(
            "Aoi Chat model route could not be resolved; keeping legacy Chat route",
            exc_info=True,
        )
        return None
    source = "agent_revision"
    raw_route = resolved
    if isinstance(resolved, tuple) and len(resolved) == 2:
        raw_route, source = resolved
    return _chat_route_from_mapping(
        raw_route,
        pins=pins,
        source=str(source or "agent_revision"),
    )


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


async def _persisted_delegation_review(session: Any, run: Any, run_id: str | None) -> bool:
    """Prove the Review phase from durable rows, never from a caller's role."""
    from ..memory.models.agent_delegation import AgentDelegation
    from .agent_delegation_service import source_revision

    if not isinstance(run, AgentRun) or not run_id or _clean(run.id) != _clean(run_id):
        return False
    try:
        stored = await session.get(AgentRun, UUID(str(run_id)), populate_existing=True)
        if stored is None or stored.work_item_id is None:
            return False
        work = await session.get(AgentWorkItem, stored.work_item_id, populate_existing=True)
        if work is None or work.source_type != "employee_delegation":
            return False
        order = await session.get(AgentDelegation, UUID(str(work.source_id)), populate_existing=True)
    except (ValueError, TypeError, AttributeError):
        return False
    if order is None:
        return False
    metadata = work.metadata_json if isinstance(work.metadata_json, Mapping) else {}
    run_metadata = stored.run_metadata if isinstance(stored.run_metadata, Mapping) else {}
    work_binding = metadata.get("agent_binding")
    run_binding = run_metadata.get("agent_binding")
    if not isinstance(work_binding, Mapping) or not isinstance(run_binding, Mapping):
        return False
    if any(not work_binding.get(key) or str(work_binding.get(key)) != str(run_binding.get(key))
           for key in ("binding_id", "snapshot_hash", "session_id", "agent_id", "agent_revision_id")):
        return False
    manifest = stored.resolved_execution_manifest
    if not isinstance(manifest, Mapping) or not stored.execution_manifest_hash:
        return False
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    return bool(
        digest == str(stored.execution_manifest_hash).lower()
        and stored.id == run.id and stored.session_id == run.session_id
        and stored.agent_id == run.agent_id and stored.agent_revision_id == run.agent_revision_id
        and stored.work_item_id == run.work_item_id and stored.project_id == run.project_id
        and stored.user_id == run.user_id
        and stored.resolved_execution_manifest == run.resolved_execution_manifest
        and stored.execution_manifest_hash == run.execution_manifest_hash
        and stored.run_metadata == run.run_metadata
        and work.active_agent_run_id == stored.id
        and work.execution_adapter == "employee_delegation"
        and work.intent_key == "review"
        and work.source_revision == source_revision(order, "review")
        and metadata.get("delegation_id") == str(order.id)
        and metadata.get("phase") == "review"
        and str(metadata.get("round")) == str(order.round)
        and work.assigned_agent_id == stored.agent_id == order.reviewer_agent_id
        and work.agent_revision_id == stored.agent_revision_id == order.reviewer_revision_id
        and work.project_id == stored.project_id == order.project_id
        and str(stored.user_id) == str(order.created_by)
        and str(stored.session_id) == str(work_binding.get("session_id"))
        and str(stored.agent_id) == str(work_binding.get("agent_id"))
        and str(stored.agent_revision_id) == str(work_binding.get("agent_revision_id"))
        and str(metadata.get("owner_user_id")) == str(order.created_by)
        and str(metadata.get("project_id")) == str(order.project_id)
    )


def _review_route_snapshot_matches(actual: Any, evidence: Any) -> bool:
    """Compare the complete reviewer route and all non-role policy fields."""
    from .agent_model_routing import OPERATIONS_WORK_ROLES

    if not isinstance(actual, Mapping) or not isinstance(evidence, Mapping):
        return False
    def split(snapshot: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, Any]]:
        roles = snapshot.get("roles")
        if isinstance(roles, Mapping):
            return roles, {k: v for k, v in snapshot.items() if k != "roles"}
        return snapshot, {k: v for k, v in snapshot.items() if k not in OPERATIONS_WORK_ROLES}

    actual_roles, actual_policy = split(actual)
    evidence_roles, evidence_policy = split(evidence)
    # A binding wraps the flat historical manifest routes with revision
    # metadata.  Those binding-only fields remain covered by its hash and
    # exact revision checks; any policy present in both must still match.
    for key in ("schema_version", "agent_id", "agent_revision_id", "agent_revision_v2"):
        if key not in evidence_policy:
            actual_policy.pop(key, None)
    return ("reviewer" in actual_roles and "reviewer" in evidence_roles
            and actual_roles["reviewer"] == evidence_roles["reviewer"]
            and actual_policy == evidence_policy)


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
    session_route_override: Mapping[str, Any] | None = None,
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
    effective_revision_tool_ids: tuple[str, ...] | None = None
    effective_revision_delegate_policy: Mapping[str, Any] | None = None
    dialogue_tool_ids: tuple[str, ...] | None = None
    required_tool_name: str | None = None
    required_tool_graph_context: Mapping[str, Any] | None = None
    aoidots_output_cap_eligible = False
    p2_revision: Any | None = None
    p2_fields_available = False
    p2_is_default_chat_agent = False
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
                    safe_rate_card: dict[str, Any] = {}
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
                    if "image_price" in raw_rate_card:
                        from src.llm.image_budget import validate_image_price_projection

                        try:
                            safe_rate_card["image_price"] = validate_image_price_projection(
                                raw_rate_card["image_price"]
                            )
                        except ValueError:
                            pass
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
            exact_revision = await session.get(AgentRevision, UUID(run_revision_id))
            if (
                exact_revision is None
                or _clean(getattr(exact_revision, "agent_id", None)) != run_agent_id
            ):
                raise ValueError("typed AgentRun AgentRevision is unavailable")
            revision_is_new_shape = revision_has_new_delegate_policy(exact_revision)
            raw_tool_ids = revision_tool_ids(exact_revision) if revision_is_new_shape else None
            effective_revision_tool_ids = (
                tuple(
                    item.strip()
                    for item in raw_tool_ids
                    if isinstance(item, str) and item.strip()
                )
                if raw_tool_ids is not None
                else None
            )
            raw_delegate_policy = (
                revision_delegate_policy(exact_revision)
                if revision_is_new_shape
                else None
            )
            effective_revision_delegate_policy = (
                dict(raw_delegate_policy)
                if raw_delegate_policy is not None
                else None
            )
            try:
                agent_key = UUID(run_agent_id)
            except ValueError:
                agent_key = run_agent_id
            current_agent = await session.get(Agent, agent_key)
            if current_agent is None or str(getattr(current_agent, "state", "")) != "active":
                raise ValueError("conversation Agent is no longer active")
            try:
                from .agent_identity_service import AgentIdentityService

                p2_is_default_chat_agent = bool(
                    AgentIdentityService.is_default_chat_agent(
                        current_agent,
                        owner_user_id=_read(run, "user_id"),
                    )
                )
            except Exception:
                owner_id = _clean(_read(run, "user_id"))
                p2_is_default_chat_agent = bool(
                    owner_id
                    and str(getattr(current_agent, "created_by", "") or "")
                    == owner_id
                    and str(getattr(current_agent, "idempotency_key", "") or "")
                    == f"private-agent:chat_default:{owner_id}:v1"
                )
            try:
                p2_revision = await session.get(AgentRevision, UUID(run_revision_id))
            except (TypeError, ValueError, AttributeError):
                p2_revision = None
            if p2_revision is not None:
                try:
                    from .agent_revision_v2 import (
                        revision_delegate_policy as _p2_revision_delegate_policy,
                        revision_model_overrides,
                        revision_tool_ids as _p2_revision_tool_ids,
                    )

                    p2_values = (
                        revision_model_overrides(p2_revision),
                        _p2_revision_tool_ids(p2_revision),
                        _p2_revision_delegate_policy(p2_revision),
                    )
                    p2_fields_available = all(value is not None for value in p2_values)
                except Exception:
                    p2_fields_available = False
            persisted_manifest = _read(run, "resolved_execution_manifest") or _read(
                run, "execution_manifest"
            )
            if isinstance(persisted_manifest, Mapping):
                manifest_route = persisted_manifest.get("route_snapshot")
                if isinstance(manifest_route, Mapping) and _manifest_has_route_fields(
                    manifest_route
                ):
                    binding_route = binding.routing_pins().route_snapshot
                    review_proven = (
                        _clean(dialogue_role).casefold() == "reviewer"
                        and await _persisted_delegation_review(session, run, run_id)
                    )
                    matches = (_review_route_snapshot_matches(binding_route, manifest_route)
                               if review_proven else _route_snapshot_matches(binding_route, manifest_route))
                    if not matches:
                        raise ValueError("typed AgentRun route snapshot mismatch")
        if binding is None:
            return None
        pins = binding.routing_pins()
        route = None
        route_errors: list[str] = []
        p2_applied = False
        p2_tool_ids: tuple[str, ...] | None = None
        p2_model_overrides: Mapping[str, Any] | None = None
        p2_delegate_policy: Mapping[str, Any] | None = None
        normalized_dialogue_role = _clean(dialogue_role).casefold()
        if not normalized_dialogue_role and isinstance(dialogue_marker, Mapping):
            normalized_dialogue_role = _clean(
                dialogue_marker.get("role") or dialogue_marker.get("dialogue_role")
            ).casefold()
        if (
            p2_fields_available
            and p2_revision is not None
            and p2_is_default_chat_agent
            and normalized_dialogue_role not in {"worker", "root", "reviewer"}
        ):
            try:
                from .agent_revision_v2 import (
                    revision_model_overrides,
                    revision_delegate_policy as _p2_revision_delegate_policy,
                    revision_tool_ids as _p2_revision_tool_ids,
                )

                p2_tool_ids = tuple(_p2_revision_tool_ids(p2_revision) or ())
                p2_model_overrides = revision_model_overrides(p2_revision)
                p2_delegate_policy = _p2_revision_delegate_policy(p2_revision)
                selected_session_route = _session_route_mapping(session_route_override)
                if selected_session_route is None:
                    selected_session_route = await _load_session_chat_route(
                        session,
                        sid,
                    )
                route = _resolve_p2_chat_route(
                    config,
                    revision=p2_revision,
                    pins=pins,
                    session_route=selected_session_route,
                )
                p2_applied = route is not None
                if not p2_applied and p2_is_default_chat_agent:
                    _warn_aoi_legacy_chat_fallback()
            except Exception:
                if p2_is_default_chat_agent:
                    _warn_aoi_legacy_chat_fallback()
                logger.debug("Aoi Chat revision settings could not be applied", exc_info=True)
                route = None
                p2_applied = False
        elif (
            run_agent_id
            and run_revision_id
            and normalized_dialogue_role not in {"worker", "root", "reviewer"}
        ):
            # This is the normal rolling deployment case: the exact revision
            # predates the nullable P2 columns. Keep the legacy route and tool
            # policy byte-for-byte compatible for the whole run.
            if p2_is_default_chat_agent:
                _warn_aoi_legacy_chat_fallback()
            p2_tool_ids = None
        if normalized_dialogue_role == "reviewer":
            snapshot = pins.route_snapshot
            roles = snapshot.get("roles", snapshot) if isinstance(snapshot, Mapping) else {}
            reviewer = roles.get("reviewer") if isinstance(roles, Mapping) else None
            if reviewer is None:
                raise AgentModelRouteError("agent_route_snapshot_role_missing")
            reviewer_route = safe_route_projection(reviewer)
            if (
                reviewer_route is None
                or reviewer_route.get("role", "reviewer") != "reviewer"
                or reviewer_route.get("inherits_main", False)
                or reviewer_route.get("route_source") == "inherit_chat"
            ):
                raise AgentModelRouteError("agent_route_snapshot_invalid")
            route = resolve_agent_role_route(config, pins, "reviewer")
            if (
                route.inherits_main
                or route.role != "reviewer"
                or route.route_source == "inherit_chat"
            ):
                raise AgentModelRouteError("agent_route_snapshot_invalid")
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
        if route is None:
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
                    aoidots_output_cap_eligible = (
                        _clean(_read(run, "run_type")).casefold() == "chat_turn"
                    )
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
                        await _capture_default_routing_snapshot(binding, proof, session, db_manager)
                        root_scope_marker = proof.marker()
                        aoidots_output_cap_eligible = (
                            _clean(_read(run, "run_type")).casefold()
                            == "chat_turn"
                        )
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
                if worker_dialogue is not None and await _is_aoidots_dialogue_worker_run(
                    session=session,
                    dialogue=worker_dialogue,
                    binding=binding,
                    run=run,
                    session_id=sid,
                ):
                    source_revision = await session.get(
                        AgentRevision, UUID(_clean(worker_dialogue.root_agent_revision_id))
                    )
                    authorization = (
                        worker_dialogue.authorization_json
                        if isinstance(worker_dialogue.authorization_json, Mapping)
                        else {}
                    )
                    tool_policy = authorization.get("tool_policy")
                    if isinstance(tool_policy, Mapping):
                        source_revision_is_new_shape = revision_has_new_delegate_policy(
                            source_revision
                        )
                        expected_hash = hashlib.sha256(
                            json.dumps(
                                authorization,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        raw_effective_tool_ids = tool_policy.get(
                            "agent_dialogue_tool_ids"
                        )
                        if not source_revision_is_new_shape:
                            raw_effective_tool_ids = None
                        if (
                            expected_hash
                            != _clean(worker_dialogue.authorization_hash)
                            or _clean(tool_policy.get("source_agent_revision_id"))
                            != _clean(worker_dialogue.root_agent_revision_id)
                            or _clean(tool_policy.get("worker_agent_revision_id"))
                            != _clean(worker_dialogue.worker_agent_revision_id)
                            or (
                                source_revision_is_new_shape
                                and not isinstance(raw_effective_tool_ids, list)
                            )
                            or (
                                raw_effective_tool_ids is not None
                                and (
                                    not isinstance(raw_effective_tool_ids, list)
                                    or any(
                                        not isinstance(item, str)
                                        or not item.strip()
                                        for item in raw_effective_tool_ids
                                    )
                                )
                            )
                        ):
                            worker_dialogue = None
                        elif isinstance(raw_effective_tool_ids, list):
                            dialogue_tool_ids = tuple(
                                item.strip() for item in raw_effective_tool_ids
                            )
                    if worker_dialogue is not None:
                        from .agent_delegate_policy import dialogue_worker_tool_ids

                        worker_revision = await session.get(
                            AgentRevision,
                            UUID(_clean(worker_dialogue.worker_agent_revision_id)),
                        )
                        dialogue_tool_ids = dialogue_worker_tool_ids(
                            worker_dialogue, source_revision, worker_revision
                        )
                        # Only the first, server-dispatched Graph request carries
                        # this internal requirement. Recheck its durable delivery
                        # and work links; caller text and UI metadata are irrelevant.
                        from ..memory.models import AgentDialogueDelivery

                        graph = authorization.get("team_graph")
                        marker = dialogue_marker if isinstance(dialogue_marker, Mapping) else {}
                        delivery_id = _clean(marker.get("delivery_id"))
                        work_id = _clean(_read(run, "work_item_id"))
                        delivery = await session.get(AgentDialogueDelivery, UUID(delivery_id)) if graph and delivery_id else None
                        work = await session.get(AgentWorkItem, UUID(work_id)) if graph and work_id else None
                        graph = graph if isinstance(graph, Mapping) else {}
                        pins = graph.get("nodepins")
                        leader = pins.get(graph.get("leader_key")) if isinstance(pins, Mapping) else None
                        ref = graph.get("ref")
                        if (
                            normalized_dialogue_role == "worker"
                            and isinstance(leader, Mapping)
                            and isinstance(ref, Mapping)
                            and graph.get("execution_kind") in {"generic", "software_code"}
                            and isinstance(graph.get("compiled"), Mapping)
                            and all(_clean(ref.get(key)) for key in ("graph_id", "revision_id", "group_key"))
                            and _clean(graph.get("graph_hash"))
                            and marker.get("team_graph_ref") == ref
                            and marker.get("team_graph_compiled") == graph.get("compiled")
                            and marker.get("team_graph_execution_kind") == graph.get("execution_kind")
                            and _clean(leader.get("agent_id")) == run_agent_id
                            and _clean(leader.get("revision_id")) == run_revision_id
                            and "delegate_plan" in (revision_tool_ids(worker_revision) or ())
                            and dialogue_tool_ids is not None
                            and "delegate_plan" in dialogue_tool_ids
                            and delivery is not None and work is not None
                            and _clean(marker.get("dialogue_id")) == _clean(worker_dialogue.id)
                            and marker.get("source_role") == "root"
                            and marker.get("receiver_role") == "worker"
                            and _clean(delivery.dialogue_id) == _clean(worker_dialogue.id)
                            and delivery.kind == "request" and delivery.sequence == 0
                            and _clean(delivery.source_session_id) == _clean(worker_dialogue.root_session_id)
                            and _clean(delivery.source_agent_id) == _clean(worker_dialogue.root_agent_id)
                            and _clean(marker.get("source_run_id")) == _clean(delivery.source_run_id)
                            and _clean(marker.get("source_agent_revision_id")) == _clean(delivery.source_agent_revision_id)
                            and _clean(delivery.source_run_id)
                            and _clean(delivery.source_agent_revision_id)
                            and _clean(delivery.receiver_session_id) == sid
                            and _clean(delivery.receiver_agent_id) == run_agent_id
                            and _clean(delivery.receiver_agent_revision_id) == run_revision_id
                            and _clean(delivery.receiver_run_id) == _clean(_read(run, "id"))
                            and _clean(delivery.receiver_work_item_id) == work_id
                            and _clean(work.active_agent_run_id) == _clean(_read(run, "id"))
                            and _clean(work.conversation_session_id) == sid
                            and _clean(work.assigned_agent_id) == run_agent_id
                            and _clean(work.agent_revision_id) == run_revision_id
                            and _clean(work.source_type) == "chat_turn"
                            and _clean(work.execution_adapter) == "interactive_chat"
                            and _clean(work.source_id) == _clean(_read(run, "id"))
                            and _clean(work.source_revision) == run_revision_id
                        ):
                            required_tool_name = "delegate_plan"
                            required_tool_graph_context = {
                                "ref": dict(ref), "graph_hash": graph["graph_hash"],
                                "source": {
                                    "source_run_id": _clean(_read(run, "id")),
                                    "source_work_item_id": work_id,
                                    "source_agent_id": run_agent_id,
                                    "source_revision_id": run_revision_id,
                                    "source_session_id": sid,
                                },
                            }
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
                            **(
                                {"agent_dialogue_tool_ids": list(dialogue_tool_ids)}
                                if dialogue_tool_ids is not None
                                else {}
                            ),
                        }
                        aoidots_output_cap_eligible = True
        except Exception:
            # Lightweight/fake DB sessions may not expose the root table. The
            # authority guard will fail closed when a dialogue tool is used.
            root_scope_marker = None
            required_tool_name = None
            required_tool_graph_context = None
            aoidots_output_cap_eligible = False
        if root_scope_marker is not None:
            # TerminalMode carries this immutable binding into TurnContext
            # directly, while its normal Config remains an object rather than
            # a Mapping.  Keep the server-issued marker on the request-local
            # snapshot; the guard still revalidates the durable ORM rows.
            try:
                object.__setattr__(binding, "_root_scope_marker", dict(root_scope_marker))
            except Exception:
                root_scope_marker = None
                required_tool_name = None
                required_tool_graph_context = None
                aoidots_output_cap_eligible = False
        return ChatAgentExecution(
            binding=binding,
            route=route,
            system_prompt=_system_prompt(binding),
            workspace=selected_workspace,
            root_scope_marker=root_scope_marker,
            dialogue_budget=dialogue_budget,
            tool_ids=(
                p2_tool_ids
                if p2_applied
                else effective_revision_tool_ids
            ),
            delegate_policy=(
                p2_delegate_policy
                if p2_applied
                else effective_revision_delegate_policy
            ),
            dialogue_tool_ids=dialogue_tool_ids,
            required_tool_name=required_tool_name,
            required_tool_graph_context=required_tool_graph_context,
            aoidots_output_cap_eligible=aoidots_output_cap_eligible,
            model_overrides=p2_model_overrides if p2_applied else None,
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

    ambient_turn = get_turn_context()
    _clear_personal_deadline_caller_route_proof(
        execution,
        source_client=source_client,
    )

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
    if execution.aoidots_output_cap_eligible:
        target_output_cap = min(
            target_output_cap or AOIDOTS_MAX_OUTPUT_TOKENS,
            AOIDOTS_MAX_OUTPUT_TOKENS,
        )

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
        **(
            {"allow_construction_fallback": False}
            if execution.route.candidate_routes is not None
            else {}
        ),
        provider_options={
            "ephemeral_session_client": True,
            **(
                {"pool_id": _route_pool_id(execution.route)}
                if str(execution.route.provider or "").strip().casefold()
                == "routing-profile"
                and _route_pool_id(execution.route)
                else {}
            ),
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
    # Capture reuse before this factory overwrites the exact binding and route
    # markers below. Reused targets remain usable for ordinary Chat, but cannot
    # receive a fresh personal caller proof.
    client_reused_for_personal_proof = client is source_client or any(
        hasattr(client, marker)
        for marker in ("_agent_execution_binding", "_agent_route")
    )
    # A test/embedding factory may reuse a request-local object.  Never carry
    # that object's previous proof into this construction attempt.
    _clear_private_marker(client, _PERSONAL_DEADLINE_PROOF_ATTRIBUTE)
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
    # Seed the Run-id keyed budget before the first provider call. The
    # current character client may be reused by concurrent Runs, so the
    # provider adapters share this ledger by trusted Run id rather than by
    # client instance.
    if run_id is not None:
        from ..llm.agent_run_budget import get_agent_run_budget

        get_agent_run_budget(
            client,
            run_id=str(run_id),
            config=getattr(client, "config", config),
            dialogue_budget=client._agent_dialogue_budget,
            require_typed_agent=False,
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
    # P2 ``tool_ids`` is an exposure restriction layered on top of the
    # existing capability ceiling and authority guard.  ``None`` is kept for
    # pre-P2 revisions so the old provider catalog remains unchanged.
    client._agent_tool_ids = (
        list(
            execution.dialogue_tool_ids
            if execution.dialogue_tool_ids is not None
            else execution.tool_ids
        )
        if execution.dialogue_tool_ids is not None or execution.tool_ids is not None
        else None
    )
    client._agent_required_tool_name = execution.required_tool_name
    client._agent_required_tool_graph_context = (
        dict(execution.required_tool_graph_context)
        if execution.required_tool_graph_context is not None else None
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
    if not client_reused_for_personal_proof:
        try:
            candidate = _personal_deadline_route_ready(
                ambient_turn=ambient_turn,
                execution=execution,
                client=client,
                config=config,
                session_id=session_id,
                project_id=project_id,
                require_client_scope=False,
            )
            if candidate is not None:
                # The factory's target is fresh and the full trusted current
                # context has already matched the exact personal route. Bind
                # these request markers before the proof issuer rechecks the
                # live client, while leaving all other Chat routes untouched.
                client.session_user_id = str(ambient_turn.user_id)
                client.current_session_id = str(ambient_turn.session_id)
                client.current_project_id = None
                _issue_personal_deadline_caller_route_proof(
                    ambient_turn=ambient_turn,
                    execution=execution,
                    client=client,
                    config=config,
                    session_id=session_id,
                    project_id=project_id,
                )
        except Exception:
            # The proof is an optional capability marker.  A malformed optional
            # route/config/client projection must never change normal Chat setup.
            _clear_personal_deadline_caller_route_proof(
                execution,
                source_client=client,
            )
    if execution.route.candidate_routes is not None:
        client._frozen_candidate_execution = execution
        client._frozen_candidate_source = source_client
        client._frozen_candidate_factory_kwargs = {
            "workspace": selected_workspace,
            "session_id": session_id,
            "project_id": project_id,
            "run_id": run_id,
            "tool_generation_max_tokens": tool_generation_max_tokens,
        }
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
    from ..llm.image_budget import bind_image_cost_limit
    try:
        bind_image_cost_limit(budget)
    except ValueError as exc:
        from .free_team_service import ComplimentaryMediaUnboundedError

        raise ComplimentaryMediaUnboundedError() from exc
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
    for key, value in marker.items():
        normalized_key = str(key)
        if normalized_key == "agent_dialogue_tool_ids" and isinstance(value, list):
            result[normalized_key] = [str(item) for item in value]
        else:
            result[normalized_key] = str(value)
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
    tool_scope_kwargs: dict[str, Any] = {"revalidate": _revalidate}
    if execution.dialogue_tool_ids is not None:
        tool_scope_kwargs["tool_ids"] = execution.dialogue_tool_ids
    elif execution.tool_ids is not None:
        tool_scope_kwargs["tool_ids"] = execution.tool_ids
    tool_scope_token = set_current_agent_tool_scope(
        execution.binding.capability_ceiling,
        allowed_subagent_ids=execution.binding.allowed_subagent_ids,
        **tool_scope_kwargs,
        delegate_policy=execution.delegate_policy,
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
    error_code: str | None = None,
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
                "error_code": error_code or "interactive_chat_failed",
                "error_message": error or "interactive chat failed",
            }
        )
    return await coordinator.settle_interactive(claim, outcome)


__all__ = [
    "ChatAgentExecution",
    "InteractiveNativeRunRoutePersistenceError",
    "bind_chat_agent_execution",
    "build_agent_chat_turn_client",
    "persist_interactive_native_run_route",
    "bind_initial_aoidots_human_run",
    "resolve_canonical_session_agent_id",
    "resolve_chat_agent_execution",
    "resolve_default_chat_agent_runtime",
    "refresh_initial_aoidots_root_proof",
    "settle_interactive_chat_work",
    "start_interactive_chat_work",
    "renew_interactive_chat_work",
]
