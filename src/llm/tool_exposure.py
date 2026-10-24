"""Provider-neutral model tool exposure rules."""

from __future__ import annotations

import inspect
import hashlib
import json
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable
from uuid import UUID

from ..tools.registry import ToolRegistry
from ..tools.operations_direct import (
    MEDIA_OPERATIONS_READ_TOOL_NAMES,
    OPERATIONS_READ_TOOL_NAMES,
)
from ..services.turn_context import get_turn_context
from ..services.project_context import (
    get_runtime_project_context,
    runtime_project_context_is_bound,
)
from .generation_policy import GenerationProfile, get_client_generation_policy
from .planning_policy import (
    PlanningRunPhase,
    get_current_planning_run_state,
    is_planning_cancelled_terminal,
    is_planning_phase_active,
)
from .tool_packs import (
    ToolPackSession,
    auto_load_contextual_packs,
    auto_load_packs_for_command_capabilities,
    build_load_tool_pack_tool,
    contextual_agent_team_scope,
    tool_pack_session_for_client,
    tool_visible_for_session,
)
from .tool_policy import (
    DOCS_READ_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    agent_tool_scope_permits_exposure,
    SEARCH_TOOL_NAMES,
    command_capabilities_from_text,
    embedding_master_enabled,
    get_current_user_input,
    sanitize_command_capabilities,
)

REVIEW_TOOL_ALLOWLIST = frozenset(
    {
        *DOCS_READ_TOOL_NAMES,
        *FILESYSTEM_READ_TOOL_NAMES,
        *OPERATIONS_READ_TOOL_NAMES,
        *MEDIA_OPERATIONS_READ_TOOL_NAMES,
        *PROJECT_MANAGEMENT_READ_TOOL_NAMES,
        *SEARCH_TOOL_NAMES,
        "knowledge_search",
        "knowledge_query",
        "knowledge_read",
        "knowledge_status",
        "media_search",
        "media_read",
        "chat_search_sources",
        "search_past_chats",
        "list_chat_sessions",
        "read_chat_session",
        "webex_list_selected_spaces",
        "webex_search_messages",
        "webex_get_thread",
        "get_current_time",
        "get_weather_info",
        "calculate",
        "computer_observe",
    }
)

PLANNING_TOOL_ALLOWLIST = frozenset(
    {
        *REVIEW_TOOL_ALLOWLIST,
        "ask_user_question",
        "submit_plan_for_approval",
    }
)

# These controls are retained by the local-native narrowing path even when an
# Agent scope narrows the ordinary capability surface.  Keep the same bounded
# exemptions here so provider-neutral presentation filtering does not remove
# the controls before that path can apply its request-local handling.
_AGENT_SCOPE_PRESENTATION_EXEMPT_TOOL_NAMES = frozenset(
    {
        "send_agent_work_message",
        "get_agent_work_status",
        "delegate_agent_work",
        "answer_agent_work_question",
        "ask_user_question",
        "load_tool_pack",
    }
)


_STRICT_TOOL_ALLOWLIST: ContextVar[frozenset[str] | None] = ContextVar(
    "aoitalk_strict_tool_allowlist",
    default=None,
)


def set_strict_tool_allowlist(
    tool_names: Iterable[str] | None,
) -> Token:
    """Bind a non-expanding tool allowlist for one trusted execution."""

    allowed = (
        frozenset(
            str(name or "").strip()
            for name in tool_names or ()
            if str(name or "").strip()
        )
        if tool_names is not None
        else None
    )
    return _STRICT_TOOL_ALLOWLIST.set(allowed)


def get_strict_tool_allowlist() -> frozenset[str] | None:
    return _STRICT_TOOL_ALLOWLIST.get()


def reset_strict_tool_allowlist(token: Token) -> None:
    _STRICT_TOOL_ALLOWLIST.reset(token)


def _strict_scope_error(
    tool_name_value: str,
    *,
    reason: str,
    project_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "success": False,
        "error": (
            "Tool execution is unavailable in this strict "
            "background scope."
        ),
        "error_code": reason,
        "tool": str(tool_name_value or ""),
    }
    if project_id:
        payload["project_id"] = project_id
    return json.dumps(payload, ensure_ascii=False)


def _contains_other_project_id(
    value: Any,
    expected_project_id: str,
) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().casefold() == "project_id":
                actual = str(item or "").strip()
                if actual and actual != expected_project_id:
                    return True
            if _contains_other_project_id(item, expected_project_id):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(
            _contains_other_project_id(item, expected_project_id)
            for item in value
        )
    return False


def _strict_project_result(
    value: Any,
    *,
    tool_name_value: str,
    project_id: str,
) -> Any:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Text-producing tools have already had their Project argument
            # server-forced below. JSON results receive the additional
            # project_id containment check.
            return value

    if not _contains_other_project_id(parsed, project_id):
        return value

    error = _strict_scope_error(
        tool_name_value,
        reason="strict_project_result_scope",
        project_id=project_id,
    )
    return error if isinstance(value, str) else json.loads(error)


def _blocked_background_tool_definition(
    tool: Any,
    *,
    reason: str,
    project_id: str | None = None,
) -> Any | None:
    if isinstance(tool, str):
        # Name-only callers are diagnostic/menu surfaces. The executable
        # ToolDefinition is wrapped when the provider registry is materialized.
        return tool

    function = getattr(tool, "function", None)
    if not callable(function):
        return None

    def _blocked(**_kwargs: Any) -> str:
        return _strict_scope_error(
            getattr(tool, "name", ""),
            reason=reason,
            project_id=project_id,
        )

    try:
        return replace(tool, function=_blocked)
    except TypeError:
        return None


def _strict_project_tool_definition(
    tool: Any,
    project_id: str,
) -> Any | None:
    if isinstance(tool, str):
        return tool

    function = getattr(tool, "function", None)
    if not callable(function):
        return None

    parameter_names = {
        str(getattr(parameter, "name", "") or "").strip()
        for parameter in (getattr(tool, "parameters", None) or ())
    }
    project_parameters = parameter_names & {"project", "project_id"}
    if not project_parameters:
        # A read tool without a Project discriminator cannot prove that it
        # stays inside the Steward's Project, so keep its schema but make the
        # execution fail closed.
        return _blocked_background_tool_definition(
            tool,
            reason="strict_project_tool_has_no_project_boundary",
            project_id=project_id,
        )

    def _scoped_kwargs(
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        scoped = dict(kwargs)
        if "project" in project_parameters:
            scoped["project"] = project_id
        if "project_id" in project_parameters:
            scoped["project_id"] = project_id
        return scoped

    if bool(getattr(tool, "is_async", False)) or inspect.iscoroutinefunction(
        function
    ):

        async def _scoped_async(**kwargs: Any) -> Any:
            result = function(**_scoped_kwargs(kwargs))
            if inspect.isawaitable(result):
                result = await result
            return _strict_project_result(
                result,
                tool_name_value=getattr(tool, "name", ""),
                project_id=project_id,
            )

        wrapped = _scoped_async
    else:

        def _scoped_sync(**kwargs: Any) -> Any:
            result = function(**_scoped_kwargs(kwargs))
            if inspect.isawaitable(result):

                async def _finish() -> Any:
                    resolved = await result
                    return _strict_project_result(
                        resolved,
                        tool_name_value=getattr(tool, "name", ""),
                        project_id=project_id,
                    )

                return _finish()
            return _strict_project_result(
                result,
                tool_name_value=getattr(tool, "name", ""),
                project_id=project_id,
            )

        wrapped = _scoped_sync

    try:
        return replace(tool, function=wrapped)
    except TypeError:
        return None


@dataclass(frozen=True)
class ToolExposureContext:
    """Immutable request-local Agent Team activation snapshot."""

    scope: Mapping[str, Any]


def resolve_tool_exposure_context(
    client: Any,
    *,
    project_context: dict[str, Any] | None = None,
) -> ToolExposureContext:
    """Resolve canonical Team activation at most once per exposure."""

    scope = contextual_agent_team_scope(
        getattr(client, "config", None),
        client=client,
        project_context=project_context,
    )
    # The resolver returns fresh lists for human-readable metadata.  Freeze
    # those containers before handing the snapshot to every deferred-pack and
    # delegate specialization call so no provider helper can mutate the
    # request-local activation result while another task reuses its registry.
    frozen_scope = {
        key: tuple(value) if isinstance(value, (list, set, frozenset)) else value
        for key, value in scope.items()
    }
    return ToolExposureContext(scope=MappingProxyType(frozen_scope))


def tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def _tool_owner(tool: Any, owner_lookup: Callable[[str], str]) -> str:
    if isinstance(tool, str):
        return owner_lookup(tool)
    owner = str(getattr(tool, "owner", "") or "")
    return owner or owner_lookup(tool_name(tool))


def _effective_client_config(client: Any = None, config: Any = None) -> Any:
    """Return the latest request config rather than a build-time snapshot."""

    if client is not None:
        current = getattr(client, "config", None)
        if current is not None:
            return current
    return config


def _current_project_scope_for_tool(client: Any = None) -> dict[str, Any] | None:
    """Resolve the current trusted project projection for capability checks.

    An explicitly bound ``None``/empty runtime context is meaningful and must
    not fall back to a provider constructor's project.  Legacy callers that
    have no request context may still use ``client.current_project_id`` as a
    narrow identity-only projection; they never receive richer stale fields.
    """

    try:
        if runtime_project_context_is_bound():
            current = get_runtime_project_context()
            return dict(current) if isinstance(current, dict) else {}
    except Exception:
        return {}
    try:
        turn = get_turn_context()
    except Exception:
        turn = None
    turn_project = str(getattr(turn, "project_id", None) or "").strip()
    if turn_project:
        return {"id": turn_project}
    client_project = str(getattr(client, "current_project_id", None) or "").strip()
    if client_project:
        return {"id": client_project}
    # Outside a request there is no trusted Project identity.  Returning None
    # lets callers preserve read-only legacy surfaces while contextual tools
    # fail closed instead of inheriting a stale constructor map.
    return None


def _workspace_tool_capability_status(
    tool: Any,
    *,
    client: Any = None,
) -> str | None:
    """Revalidate ``ws_*`` tools against the current Project and manifest.

    Workspace manifests are discovered once when a provider registry is built,
    but providers may be reused after a Project switch or a manifest edit.
    Every exposure/execution pass therefore checks the trusted Project id,
    the current workspace-tools enablement flag, and the exact manifest hash
    captured at registration.  Missing provenance is a hard denial.
    """

    name = tool_name(tool)
    owner = str(getattr(tool, "owner", "") or "").strip().casefold()
    if not name.startswith("ws_") and owner != "workspace":
        return None
    availability = getattr(tool, "availability", None)
    if not isinstance(availability, Mapping):
        return "workspace tool provenance is unavailable"
    expected_project_id = str(
        availability.get("project_id")
        or availability.get("project")
        or ""
    ).strip()
    if not expected_project_id:
        return "workspace tool has no trusted Project identity"
    current = _current_project_scope_for_tool(client)
    if not isinstance(current, Mapping):
        return "workspace tool requires a trusted Project context"
    current_project_id = str(
        current.get("project_id")
        or current.get("id")
        or (
            current.get("project", {}).get("id")
            if isinstance(current.get("project"), Mapping)
            else ""
        )
        or ""
    ).strip()
    if not current_project_id or current_project_id.casefold() != expected_project_id.casefold():
        return "workspace tool Project scope does not match the current Project"
    metadata = current.get("metadata") if isinstance(current.get("metadata"), Mapping) else {}
    enabled_value = current.get("workspace_tools_enabled", metadata.get("workspace_tools_enabled"))
    if enabled_value is not True and str(enabled_value or "").strip().casefold() not in {
        "true",
        "1",
        "yes",
        "on",
    }:
        return "workspace tools are disabled for the current Project"

    expected_manifest_hash = str(
        availability.get("manifest_sha256")
        or availability.get("manifest_hash")
        or ""
    ).strip().casefold()
    if not expected_manifest_hash:
        return "workspace tool manifest integrity is unavailable"
    manifest_name = str(
        availability.get("manifest_name") or name.removeprefix("ws_")
    ).strip()
    if not manifest_name:
        return "workspace tool manifest name is unavailable"
    try:
        from ..services.project_workspace_cleanup import get_project_workspace_path
        workspace = get_project_workspace_path(UUID(current_project_id))
        manifest_path = workspace / "tools" / manifest_name / "manifest.yaml"
        resolved_workspace = workspace.resolve()
        resolved_manifest = manifest_path.resolve()
        resolved_manifest.relative_to(resolved_workspace)
        if not resolved_manifest.is_file():
            return "workspace tool manifest is missing"
        current_hash = hashlib.sha256(resolved_manifest.read_bytes()).hexdigest().casefold()
        if current_hash != expected_manifest_hash:
            return "workspace tool manifest integrity has changed"
        from ..services.workspace_tool_runner import load_workspace_tool_manifests

        manifests = load_workspace_tool_manifests(workspace)
        manifest = next((item for item in manifests if item.name == manifest_name), None)
        if manifest is None:
            return "workspace tool manifest is invalid or unavailable"
        expected_entrypoint = str(availability.get("entrypoint") or "").strip()
        if expected_entrypoint:
            try:
                if Path(manifest.entrypoint).resolve() != Path(expected_entrypoint).resolve():
                    return "workspace tool entrypoint does not match its manifest"
            except (OSError, ValueError):
                return "workspace tool entrypoint is invalid"
        expected_entrypoint_hash = str(
            availability.get("entrypoint_sha256") or ""
        ).strip().casefold()
        if expected_entrypoint_hash:
            current_entrypoint_hash = hashlib.sha256(
                manifest.entrypoint.read_bytes()
            ).hexdigest().casefold()
            if current_entrypoint_hash != expected_entrypoint_hash:
                return "workspace tool entrypoint integrity has changed"
    except Exception:
        return "workspace tool manifest could not be revalidated"
    return None


def runtime_tool_capability_status(
    tool: Any,
    *,
    client: Any = None,
    config: Any = None,
) -> str | None:
    """Return a sanitized denial reason for a stale/disabled tool.

    The result is intentionally a plain reason string suitable for a compact
    audit trace; it is never used as an authority token.  ``None`` means the
    current capability gates permit the definition to proceed.
    """

    effective_config = _effective_client_config(client, config)
    try:
        from .tool_policy import _runtime_capability_for_tool

        reason = _runtime_capability_for_tool(
            tool_name(tool),
            config=effective_config,
            tool_definition=tool,
        )
    except Exception:
        # A malformed optional config must not widen an untrusted capability;
        # if a config was supplied, fail closed.  Legacy config-less callers
        # retain their historical behavior.
        reason = (
            "runtime capability could not be validated"
            if effective_config is not None
            else None
        )
    if reason:
        return str(reason)
    return _workspace_tool_capability_status(tool, client=client)


def effective_capability_trace(
    client: Any,
    tools: Iterable[Any],
    *,
    config: Any = None,
) -> dict[str, Any]:
    """Build a compact, sanitized capability trace for inspectability.

    Only tool names, owners, and allow/deny reasons are returned.  No argument,
    Project payload, token, manifest path, or raw user text is included, and
    callers must not treat this diagnostic projection as an authority source.
    """

    entries: list[dict[str, str]] = []
    for item in tools:
        name = tool_name(item).strip()
        if not name:
            continue
        owner = str(getattr(item, "owner", "") or "core").strip() or "core"
        reason = runtime_tool_capability_status(item, client=client, config=config)
        entries.append(
            {
                "tool": name[:128],
                "owner": owner[:64],
                "status": "denied" if reason else "allowed",
                **({"reason": str(reason)[:160]} if reason else {}),
            }
        )
    return {"tools": entries}


def _owner_lookup_for_client(client: Any) -> Callable[[str], str]:
    registry = getattr(client, "_tool_registry", None)
    getter = getattr(registry, "get", None) if registry is not None else None
    if not callable(getter):
        return lambda name: ""

    def _lookup(name: str) -> str:
        try:
            tool = getter(name)
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(tool, "owner", "") or "")

    return _lookup


def effective_tool_pack_session(
    client: Any,
    *,
    exposure: ToolExposureContext | None = None,
) -> ToolPackSession:
    """クライアントのロード済み pack 集合を返し、自動ロード分を反映する。

    明示的なコマンド capability は意図が確定しているため、モデルの往復を
    待たずにここで pack をロードする。
    """
    session = tool_pack_session_for_client(client)
    # TurnContext is the server-owned isolation boundary.  Do not even
    # auto-load contextual packs for a Help/controller turn when a stale or
    # direct caller omitted the derived capability marker.
    if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
        return session
    capabilities: set[str] = set(
        sanitize_command_capabilities(
            getattr(client, "current_command_capabilities", ()) or ()
        )
    )
    user_input = get_current_user_input()
    if user_input:
        capabilities |= command_capabilities_from_text(user_input)
    # Help is an intentionally empty provider surface.  Do not auto-load any
    # contextual/manual pack while a Help turn is active; the final filter
    # below also returns an empty list even when a stale pack was loaded by a
    # previous turn on the same long-lived client.
    if "aoitalk_help" in capabilities:
        return session
    if capabilities:
        auto_load_packs_for_command_capabilities(session, capabilities)
    session.load("browser")
    # Apps and App Development Team packs are activated only from one
    # server-resolved structured snapshot for this exposure pass.
    exposure = exposure or resolve_tool_exposure_context(client)
    auto_load_contextual_packs(
        session,
        client=client,
        contextual_scope=exposure.scope,
    )
    return session


# Request-local lexical-only catalog projection for the semantic retrieval tool.
#
# The canonical ``search_past_chats`` definition lives in a registry that is
# shared per client (and, for some hosts, per process).  When the embedding
# master switch is OFF the *emitted* catalog must not advertise semantic
# retrieval at all, so the projection below builds a throwaway clone instead of
# mutating the canonical ``ToolDefinition``/``ToolParam``/``schema`` objects.
_SEMANTIC_OFF_PROJECTED_TOOL_NAME = "search_past_chats"
_SEMANTIC_OFF_PROJECTED_PARAM_NAME = "mode"
_SEMANTIC_OFF_PROJECTED_MODE = "text"
_SEMANTIC_OFF_TOOL_DESCRIPTION = (
    "Search past chat messages by lexical keyword match over stored text."
)
_SEMANTIC_OFF_MODE_DESCRIPTION = (
    "Retrieval mode. Only 'text' is accepted: lexical keyword matching."
)


def _project_semantic_off_parameters(parameters: Any) -> Any:
    """Return ``parameters`` with the retrieval ``mode`` narrowed to lexical."""
    if not isinstance(parameters, (list, tuple)):
        return parameters
    projected: list[Any] = []
    for parameter in parameters:
        if getattr(parameter, "name", None) != _SEMANTIC_OFF_PROJECTED_PARAM_NAME:
            projected.append(parameter)
            continue
        try:
            projected.append(
                replace(
                    parameter,
                    description=_SEMANTIC_OFF_MODE_DESCRIPTION,
                    required=False,
                    default=_SEMANTIC_OFF_PROJECTED_MODE,
                    enum=[_SEMANTIC_OFF_PROJECTED_MODE],
                    schema={
                        "type": "string",
                        "enum": [_SEMANTIC_OFF_PROJECTED_MODE],
                        "default": _SEMANTIC_OFF_PROJECTED_MODE,
                    },
                )
            )
        except (TypeError, ValueError):
            # A non-dataclass stand-in cannot be cloned; keep it verbatim so the
            # caller still gets a complete catalog rather than a broken entry.
            projected.append(parameter)
    return projected


def _project_semantic_off_tool_definition(definition: Any) -> Any:
    """Return a lexical-only clone of the semantic retrieval tool definition.

    Every other tool, and every non-``ToolDefinition`` entry (notably the plain
    tool-name strings used by name-only CLI menus), is returned unchanged so the
    emitted catalog keeps its shape.  Cloning preserves ``function`` wrappers,
    ACL/scope metadata and the tool name; only the tool description and the
    retrieval ``mode`` parameter differ.
    """
    if tool_name(definition) != _SEMANTIC_OFF_PROJECTED_TOOL_NAME:
        return definition
    parameters = getattr(definition, "parameters", None)
    if not isinstance(parameters, (list, tuple)):
        return definition
    try:
        return replace(
            definition,
            description=_SEMANTIC_OFF_TOOL_DESCRIPTION,
            parameters=_project_semantic_off_parameters(parameters),
        )
    except (TypeError, ValueError):
        return definition


def filter_tools_for_client(
    client: Any,
    tools: Iterable[Any],
    *,
    exposure: ToolExposureContext | None = None,
) -> list[Any]:
    """Return only tools that may be shown for the client's current session."""
    strict_allowlist = get_strict_tool_allowlist()
    # Reserved controller turns (notably AoiTalk Help) must not even materialize
    # the caller's registry iterable.  Some registries build or resolve dynamic
    # definitions from ``get_all``/generators, so returning before ``list``
    # preserves the no-tools/no-context boundary for direct low-level callers.
    # Trusted Project Steward/background turns also suppress automatic context,
    # but bind a strict allowlist and still need the fail-closed execution
    # wrappers below.  Only the unscoped Help/controller turn takes this early
    # return.
    if (
        bool(getattr(get_turn_context(), "suppress_automatic_context", False))
        and strict_allowlist is None
    ):
        return []
    values = list(tools)
    current_capabilities = set(
        sanitize_command_capabilities(
            getattr(client, "current_command_capabilities", ()) or ()
        )
    )
    trusted_input = get_current_user_input()
    if trusted_input:
        current_capabilities |= command_capabilities_from_text(trusted_input)
    # Reserved Help turns must publish zero provider schemas.  Keep this check
    # before registry resolution, contextual pack loading, planning
    # filters, or any other dynamic exposure work so stale registries cannot
    # leak a tool on the first provider request.
    if "aoitalk_help" in current_capabilities:
        return []
    registry = getattr(client, "_tool_registry", None)

    # Provider registries are intentionally persistent.  Re-evaluate owner
    # capability switches (Apps/Spotify/Search/Media/managed tools/Agent Team)
    # before any planning or pack-specific early return so a disabled feature
    # can never remain visible merely because its schema was built earlier.
    #
    # The same phase also resolves the embedding master switch once per call so
    # an OFF catalog can emit the lexical-only ``search_past_chats`` projection
    # on every downstream path (normal, planning, review and strict allowlist)
    # instead of only on the plain provisioning path.
    semantic_off_catalog_active = not embedding_master_enabled(
        _effective_client_config(client)
    )
    gated_values: list[Any] = []
    for candidate in values:
        definition = candidate
        if isinstance(candidate, str) and registry is not None:
            getter = getattr(registry, "get", None)
            if callable(getter):
                try:
                    definition = getter(candidate) or candidate
                except Exception:  # noqa: BLE001 - diagnostic registry seam
                    definition = candidate
        if runtime_tool_capability_status(definition, client=client) is None:
            if (
                tool_name(candidate)
                not in _AGENT_SCOPE_PRESENTATION_EXEMPT_TOOL_NAMES
                and agent_tool_scope_permits_exposure(
                    tool_name(candidate),
                    definition,
                )
                is False
            ):
                continue
            gated_values.append(
                _project_semantic_off_tool_definition(candidate)
                if semantic_off_catalog_active and not isinstance(candidate, str)
                else candidate
            )
    values = gated_values

    if strict_allowlist is not None:
        values = [
            tool
            for tool in values
            if tool_name(tool) in strict_allowlist
        ]

        # The strict allowlist constrains *which* tools can execute. Also
        # constrain the trusted data scope before provider-specific schema
        # materialization so a registry rebuild cannot widen it.
        turn = get_turn_context()
        constrained: list[Any] = []
        if turn.user_id is None:
            # Actorless agent_check deliberately has no authority to read any
            # user's Project/Docs data. Keep the canonical read-only schemas,
            # but every data-bearing execution fails before the tool function.
            for tool in values:
                wrapped = _blocked_background_tool_definition(
                    tool,
                    reason="actorless_background_scope",
                )
                if wrapped is not None:
                    constrained.append(wrapped)
            values = constrained
        elif turn.strict_project_scope:
            strict_project_id = str(turn.project_id or "").strip()
            if not strict_project_id:
                return []
            for tool in values:
                wrapped = _strict_project_tool_definition(
                    tool,
                    strict_project_id,
                )
                if wrapped is not None:
                    constrained.append(wrapped)
            values = constrained

    exposure = exposure or resolve_tool_exposure_context(client)
    if is_planning_cancelled_terminal():
        return []
    if get_client_generation_policy(client).profile == GenerationProfile.REVIEW:
        planning_state = get_current_planning_run_state()
        if planning_state is not None and planning_state.phase in {
            PlanningRunPhase.PLANNING,
            PlanningRunPhase.AWAITING_PLAN_APPROVAL,
        }:
            review_allowlist = REVIEW_TOOL_ALLOWLIST | {
                "ask_user_question",
                "submit_plan_for_approval",
            }
            return [tool for tool in values if tool_name(tool) in review_allowlist]
        # REVIEW では allowlist が最も強いフィルタで、載っているのはすべて
        # 読み取り専用ツール。`load_tool_pack` は allowlist に無く REVIEW では
        # 公開されないため、pack の deferral を重ねると allowlist 済みの
        # 読み取りツールが恒久的に使えなくなる。REVIEW は allowlist だけを適用する。
        return [tool for tool in values if tool_name(tool) in REVIEW_TOOL_ALLOWLIST]
    planning_state = get_current_planning_run_state()
    if planning_state is not None and planning_state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }:
        return [
            tool for tool in values if tool_name(tool) in PLANNING_TOOL_ALLOWLIST
        ]
    if planning_state is not None and planning_state.phase == PlanningRunPhase.AWAITING_USER:
        return values
    session = effective_tool_pack_session(client, exposure=exposure)
    owner_lookup = _owner_lookup_for_client(client)
    visible: list[Any] = []
    for tool in values:
        # ``load_tool_pack`` contains an enum/description generated when the
        # registry was first built.  Rebuild that one definition per exposure
        # pass so a fixed provider registry removes the contextual ``apps``
        # option immediately after App context is turned off.
        original_tool = tool
        if tool_name(tool) == "load_tool_pack" and registry is not None:
            try:
                dynamic_loader = build_load_tool_pack_tool(
                    registry,
                    session,
                    client=client,
                    contextual_scope=exposure.scope,
                )
            except Exception:  # noqa: BLE001 - compatibility fake registries
                dynamic_loader = None
            if dynamic_loader is None:
                continue
            # Name-only callers (CLI/tool menus) expect strings back; use the
            # rebuilt definition for gating but retain their original shape.
            if not isinstance(tool, str):
                tool = dynamic_loader
        if tool_visible_for_session(
            session,
            tool_name(tool),
            _tool_owner(tool, owner_lookup),
            client=client,
            contextual_scope=exposure.scope,
        ):
            # The Agent Team delegate is the one contextual schema whose
            # roster text must change with each request.  Clone the definition
            # rather than mutating a shared registry entry; concurrent clients
            # can then expose different Team/Subagent sets safely.
            if tool_name(tool) == "agent_team_delegate":
                try:
                    from .runtime_tool_registry import (
                        specialize_agent_team_delegate_definition,
                    )

                    # CLI menus and a few legacy adapters pass only tool
                    # names. Resolve the canonical definition solely for
                    # specialization/gating, then preserve the name-only
                    # return shape. Never keep an unverified build-time
                    # roster when the definition cannot be resolved.
                    specialization_target = tool
                    if isinstance(tool, str):
                        get_tool = getattr(registry, "get", None)
                        specialization_target = (
                            get_tool(tool) if callable(get_tool) else None
                        )
                        if specialization_target is None:
                            continue
                    tool = specialize_agent_team_delegate_definition(
                        specialization_target,
                        config=getattr(client, "config", None),
                        client=client,
                        contextual_scope=exposure.scope,
                    )
                except Exception:  # noqa: BLE001 - never leak stale roster
                    # A shared registry may contain a build-time App
                    # roster.  Keeping that base definition after a failed
                    # request-local specialization would expose inactive
                    # Teams/Subagents, so drop the delegate fail-closed.
                    continue
            visible.append(original_tool if isinstance(original_tool, str) else tool)
    return visible


def is_review_generation(client: Any) -> bool:
    return get_client_generation_policy(client).profile == GenerationProfile.REVIEW


def filtered_registry_for_client(
    client: Any,
    registry: ToolRegistry,
    *,
    exposure: ToolExposureContext | None = None,
) -> ToolRegistry:
    """Create a non-expanding registry view for model exposure and execution."""
    strict_allowlist = get_strict_tool_allowlist()
    # Do not call ``get_all``/``get_names`` on a dynamic registry during an
    # isolated controller turn.  Even an empty filtered result would otherwise
    # execute registry expansion work before the lower-level guard runs.
    # Project Steward/agent-check turns are also isolated, but their trusted
    # strict allowlist requires registry materialization so definitions can be
    # wrapped with the project/actor scope before execution.
    if (
        bool(getattr(get_turn_context(), "suppress_automatic_context", False))
        and strict_allowlist is None
    ):
        return ToolRegistry()
    get_all = getattr(registry, "get_all", None)
    if callable(get_all):
        values = list(get_all())
    elif isinstance(registry, dict):
        values = list(registry.values())
    else:
        get_names = getattr(registry, "get_names", None)
        get_tool = getattr(registry, "get", None)
        values = (
            [
                tool
                for name in get_names()
                if (tool := get_tool(name)) is not None
            ]
            if callable(get_names) and callable(get_tool)
            else []
        )
    filtered = filter_tools_for_client(client, values, exposure=exposure)
    # 取り出せない形のレジストリ（テスト用のフェイクなど）は values が空になるため、
    # 絞り込みが発生しない場合は元のレジストリをそのまま返す。
    # A same-sized result can still contain request-local clones (notably the
    # Agent Team delegate roster, or a rebuilt contextual pack loader).  Keep
    # the original registry only when every definition is the exact object
    # that was supplied; otherwise the provider would silently retain a stale
    # shared description.
    if len(filtered) == len(values) and all(
        exposed is original for exposed, original in zip(filtered, values)
    ):
        return registry
    result = ToolRegistry()
    for tool in filtered:
        result.register(tool)
    return result
