"""Runtime tool registry helpers for LLM clients."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from ..services.project_context import (
    get_runtime_project_context,
    project_context_enabled_for_client,
    runtime_project_context_is_bound,
)
from ..services.agent_run_scope_service import (
    REQUIRE_RUN_SCOPE_KEY,
    RUN_SCOPE_CONTEXT_KEY,
    TRUSTED_PARENT_CONTEXT_KEY,
    TrustedParentRunContext,
    inject_trusted_parent_scope,
    resolve_trusted_parent_run_context,
)
from ..services.turn_context import get_turn_context
from ..features import Features
from ..skills.executor import invoke_skill
from ..tools.apps import build_app_tool_definitions
from ..tools.core import ToolDefinition, ToolParam, ensure_tool_definitions, tool as tool_decorator
from ..tools.registry import ToolRegistry
from ..services.agent_run_service import (
    AgentRunService,
    get_current_agent_run_id,
    reset_current_agent_run_id,
    set_current_agent_run_id,
)
from ..services.agent_team_service import (
    ToolFailureCircuitBreaker,
    parse_structured_tool_failure,
    cancelled_tool_terminal_event,
    AgentContinuationState,
    set_current_continuation_state,
    tool_failure_family,
)
from ..services.agent_team_v3 import (
    agent_team_v3_delegation_enabled,
    agent_team_v3_enabled,
    agent_team_v3_subagents,
    agent_team_v3_teams,
    agent_team_subagent_allows_write,
    agent_team_workspace_access,
    filter_agent_team_capabilities,
    resolve_agent_team_v3_route,
)
from .tool_packs import (
    CHARACTER_TOOL_OWNER,
    contextual_agent_team_scope,
)
from .tool_policy import (
    FILESYSTEM_MUTATION_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    check_tool_call_allowed,
    format_blocked_tool_result,
    get_current_agent_team_role,
    get_current_user_input,
    is_knowledge_search_enabled,
    is_memory_search_enabled,
    looks_like_docs_agent_delegation_request,
)
from .specialist_delegate import (
    AgentTeamSubagentDelegationRunner,
    MediaDelegationRunner,
)
from .worker_report import (
    WORKER_REPORT_SCHEMA_VERSION,
    normalize_worker_report,
    parent_publication_metadata,
)
from .generation_cancellation import (
    GenerationInterrupted,
    get_current_generation_cancellation,
    raise_if_generation_interrupted,
    set_current_generation_mutation_gate,
    GenerationMutationGate,
)


_LOADED_AGENT_TEAM_IDS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "aoitalk_loaded_agent_team_ids", default=frozenset()
)
# Loading a manual Team is a conversation/session concern, not a single
# generation-turn concern.  Keep a bounded process-local projection keyed by
# the active conversation/session identifier; the ContextVar above remains a
# compatibility fallback for callers that do not provide a client identity.
_LOADED_AGENT_TEAM_IDS_BY_SESSION: dict[str, frozenset[str]] = {}
_LOADED_AGENT_TEAM_IDS_LOCK = threading.RLock()
_DOCS_ROOT_DELEGATION_COMPLETED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aoitalk_docs_root_delegation_completed", default=False
)
_ROOT_TOOL_FAILURE_BREAKERS: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "aoitalk_root_tool_failure_breakers", default={}
)


def _session_identity(client: Any = None) -> str:
    candidates = (
        getattr(client, "current_session_id", None),
        getattr(client, "conversation_id", None),
        getattr(client, "session_id", None),
    )
    user_candidates = (
        getattr(client, "session_user_id", None),
        getattr(client, "user_id", None),
    )
    try:
        turn = get_turn_context()
        candidates += (getattr(turn, "session_id", None),)
        user_candidates += (getattr(turn, "user_id", None),)
    except Exception:
        pass
    for value in candidates:
        clean = str(value or "").strip()
        if clean:
            user = next((str(item or "").strip() for item in user_candidates if str(item or "").strip()), "")
            return f"{user}:{clean}" if user else clean
    return "__ambient__"


def loaded_agent_team_ids(session_id: str | None = None) -> frozenset[str]:
    key = str(session_id or "").strip()
    if key:
        with _LOADED_AGENT_TEAM_IDS_LOCK:
            return _LOADED_AGENT_TEAM_IDS_BY_SESSION.get(key, frozenset())
    return _LOADED_AGENT_TEAM_IDS.get()


def _register_load_agent_team_tool(
    registry: ToolRegistry,
    *,
    config: Any,
    client: Any = None,
) -> None:
    """Register the manual Team loader without fixed fixed Team/Subagent names."""
    if "load_agent_team" in registry or not agent_team_v3_enabled(config):
        return
    if not any(
        str((team.get("activation") or {}).get("mode") or "always").strip().lower()
        == "manual"
        for team in agent_team_v3_teams(config)
    ):
        return

    async def load_agent_team(team_id: str) -> str:
        """Load one manual Agent Team by name or stable ID for this session."""
        requested_name = str(team_id or "").strip()
        team = next(
            (
                item
                for item in agent_team_v3_teams(config)
                if str(item.get("team_id") or "") == requested_name
                or str(item.get("name") or "").casefold() == requested_name.casefold()
            ),
            None,
        )
        clean_id = str((team or {}).get("team_id") or requested_name)
        if not team:
            return f"Agent Team is not available: {clean_id or '(empty)'}"
        if not team.get("enabled", True):
            return f"Agent Team is disabled: {clean_id}"
        activation = team.get("activation") if isinstance(team, dict) else {}
        mode = str((activation or {}).get("mode") or "always").strip().lower()
        if mode != "manual":
            return f"Agent Team does not require manual loading: {clean_id}"
        session_key = _session_identity(client)
        with _LOADED_AGENT_TEAM_IDS_LOCK:
            current = set(_LOADED_AGENT_TEAM_IDS_BY_SESSION.get(session_key, frozenset()))
            current.add(clean_id)
            loaded = frozenset(current)
            _LOADED_AGENT_TEAM_IDS_BY_SESSION[session_key] = loaded
        _LOADED_AGENT_TEAM_IDS.set(loaded)
        return f"Agent Team loaded: {clean_id}"

    load_agent_team.__name__ = "load_agent_team"
    load_agent_team.__doc__ = (
        "Load a manual Agent Team by its display name or stable team_id for this conversation. "
        "Always/contextual Teams are active according to their activation settings."
    )
    registry.register(
        replace(
            tool_decorator(load_agent_team),
            owner="agent_team",
            risk="low",
            side_effect="none",
            supports_parallel=False,
        )
    )


def _model_visible_project_context(
    project_context: dict[str, Any] | None,
    *,
    client: Any = None,
) -> dict[str, Any] | None:
    """Keep internal selected-project state out of the model-facing registry."""

    if not project_context:
        return None
    turn = get_turn_context()
    if client is not None and project_context_enabled_for_client(client):
        return project_context
    selected_project_id = turn.project_id or getattr(client, "current_project_id", None)
    if (
        (turn.include_project_context is False or client is not None)
        and str(project_context.get("id") or "").strip()
        == str(selected_project_id or "").strip()
    ):
        return None
    return project_context


def _trusted_runtime_project_context(
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the request-local server context with trusted identity filled in.

    Agent-Team child execution needs the full internal context for App ACL and
    approval checks, while prompt formatting applies the separate
    ``include_project_context`` visibility policy.  This helper deliberately
    prefers a bound ContextVar (including an explicitly bound empty mapping)
    and only uses a construction-time fallback outside a request.  Identity
    mismatches are rejected rather than silently widening scope.
    """

    fallback_capability: Any = None
    try:
        from ..security.qa_browser_transport import QABrowserCapability

        if isinstance(fallback, dict):
            for key in ("qa_browser_capability", "_qa_browser_capability"):
                candidate = fallback.get(key)
                if candidate is None:
                    continue
                if not isinstance(candidate, QABrowserCapability):
                    # Model/project JSON may contain a look-alike value.  It
                    # is simply dropped below; only an explicit parent API
                    # argument (``qa_browser_capability=``) raises.
                    continue
                fallback_capability = candidate
                break
    except Exception:
        fallback_capability = None

    if runtime_project_context_is_bound():
        current = get_runtime_project_context()
        runtime_context: dict[str, Any] = (
            dict(current) if isinstance(current, dict) else {}
        )
    else:
        runtime_context = dict(fallback) if isinstance(fallback, dict) else {}
    if fallback_capability is not None:
        existing_capability = runtime_context.get("qa_browser_capability") or runtime_context.get(
            "_qa_browser_capability"
        )
        if existing_capability is not None and not isinstance(
            existing_capability, QABrowserCapability
        ):
            runtime_context.pop("qa_browser_capability", None)
            runtime_context.pop("_qa_browser_capability", None)
            existing_capability = None
        if existing_capability is not None and existing_capability is not fallback_capability:
            raise ValueError("runtime context contains a different QA browser capability")
        runtime_context["qa_browser_capability"] = fallback_capability
        runtime_context["_qa_browser_capability"] = fallback_capability

    try:
        turn = get_turn_context()
    except Exception:
        turn = None

    turn_user_id = str(getattr(turn, "user_id", None) or "").strip()
    context_user_id = str(
        runtime_context.get("user_id")
        or runtime_context.get("authenticated_user_id")
        or ""
    ).strip()
    if (
        turn_user_id
        and context_user_id
        and turn_user_id.casefold() != context_user_id.casefold()
    ):
        raise PermissionError("Agent Team runtime user identity mismatch")

    turn_project_id = str(getattr(turn, "project_id", None) or "").strip()
    context_project_value = runtime_context.get("project_id")
    if context_project_value is None and not (
        runtime_context.get("app_id") or runtime_context.get("active_app_id")
    ):
        context_project_value = runtime_context.get("id")
    if context_project_value is None and isinstance(
        runtime_context.get("project"), dict
    ):
        context_project_value = runtime_context["project"].get("id")
    context_project_id = str(context_project_value or "").strip()
    if turn is not None and (
        turn_project_id
        and context_project_id
        and turn_project_id.casefold() != context_project_id.casefold()
    ):
        raise PermissionError("Agent Team runtime Project identity mismatch")

    if turn_user_id and not context_user_id:
        runtime_context["user_id"] = turn_user_id
    if turn_project_id and not context_project_id:
        runtime_context["project_id"] = turn_project_id
    # QA browser handles are parent-issued opaque capability facades.  A
    # model/project payload may contain a look-alike object with ``navigate``
    # methods, but it must never cross into the SpecialistDelegationRunner.
    # Keep only the security module's concrete facade and drop all untrusted
    # aliases before a child registry is built.
    try:
        from ..security.qa_browser_transport import QABrowserCapability

        for key in ("qa_browser_capability", "_qa_browser_capability"):
            value = runtime_context.get(key)
            if value is not None and not isinstance(value, QABrowserCapability):
                runtime_context.pop(key, None)
    except Exception:
        runtime_context.pop("qa_browser_capability", None)
        runtime_context.pop("_qa_browser_capability", None)
    return runtime_context


def _inject_qa_browser_capability(
    project_context: Mapping[str, Any] | None,
    capability: Any,
) -> dict[str, Any]:
    """Attach a parent-issued QA facade to trusted runtime context only.

    The worker bridge consumes this context through
    ``SpecialistDelegationRunner``.  Requiring the concrete
    :class:`QABrowserCapability` prevents raw Playwright drivers, pages,
    contexts, profile paths, or arbitrary test doubles from becoming tools.
    """

    from ..security.qa_browser_transport import QABrowserCapability

    if not isinstance(capability, QABrowserCapability):
        raise TypeError("qa_browser_capability must be a QABrowserCapability")
    context = dict(project_context or {})
    for key in ("qa_browser_capability", "_qa_browser_capability"):
        existing = context.get(key)
        if existing is not None and existing is not capability:
            raise ValueError("project context contains a different QA browser capability")
    context["qa_browser_capability"] = capability
    context["_qa_browser_capability"] = capability
    return context


# Public aliases used by parent/controller integrations.  Keep the concrete
# implementation private so raw capability objects are never accepted by
# accident from model-facing tool arguments.
attach_qa_browser_capability = _inject_qa_browser_capability
bind_qa_browser_capability = _inject_qa_browser_capability
inject_qa_browser_capability = _inject_qa_browser_capability


def _remove_untrusted_run_scope_fields(
    project_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Remove scope-looking values that did not come from the parent factory.

    ``project_context`` is server-bound for normal turns, but extensions and
    model-facing adapters may still carry arbitrary JSON fields.  A raw
    ``run_scope`` dict/path, ``require_run_scope`` flag, or forged metadata
    must never become an execution authority.  A validated
    :class:`TrustedParentRunContext` is injected separately below.
    """

    context = dict(project_context or {})
    for key in (
        RUN_SCOPE_CONTEXT_KEY,
        REQUIRE_RUN_SCOPE_KEY,
        "agent_run_scope",
        "trusted_parent_scope",
        TRUSTED_PARENT_CONTEXT_KEY,
        "_trusted_agent_run_capability",
    ):
        context.pop(key, None)
    metadata = context.get("metadata")
    if isinstance(metadata, Mapping):
        safe_metadata = dict(metadata)
        for key in (
            RUN_SCOPE_CONTEXT_KEY,
            REQUIRE_RUN_SCOPE_KEY,
            "agent_run_scope",
            "trusted_parent_scope",
            "parent_run_id",
            "canonical_repository_root",
            "repository_identity",
        ):
            safe_metadata.pop(key, None)
        context["metadata"] = safe_metadata
    return context


def _agent_enabled(config: Any, domain_key: str, default: bool = True) -> bool:
    if Features.is_enterprise() and domain_key == "media":
        # The Enterprise publisher removes these specialist modules.  Do not
        # trust a stale persisted config row to reintroduce their lazy imports.
        return False
    if not config:
        return default
    if domain_key == "media":
        # Shared integrations intentionally live outside Agent Team.  Keep
        # their direct integration entry points available regardless of Team
        # topology.
        return bool(config.get("agents", {}).get(domain_key, {}).get("enabled", default))
    return bool(config.get("agents", {}).get(domain_key, {}).get("enabled", default))


def _apps_enabled(config: Any, default: bool = True) -> bool:
    if not config:
        return default
    if isinstance(config, dict):
        return bool((config.get("apps") or {}).get("enabled", default))
    getter = getattr(config, "get", None)
    if callable(getter):
        return bool(getter("apps.enabled", default))
    return default


def _story_context_active_for_client(client: Any) -> bool:
    """Read the existing StoryChatContext without publishing it to normal chat."""

    getter = getattr(client, "_get_story_chat_context_sync", None) if client is not None else None
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except Exception:
        return False


def _project_management_direct_tools_enabled(config: Any, default: bool = True) -> bool:
    if not config:
        return default
    agents = config.get("agents", {}) if hasattr(config, "get") else {}
    project_config = agents.get("project_management", {}) if isinstance(agents, dict) else {}
    if isinstance(project_config, dict) and "direct_tools_enabled" in project_config:
        return bool(project_config.get("direct_tools_enabled"))
    return default


def _configured_model(config: Any, default: str = "gpt-5.6-luna") -> str:
    if not config or not hasattr(config, "get"):
        return default
    return str(config.get("llm_model", default) or default)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        value = config.get(key, default)
        if value is not default or "." not in key:
            return value
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _spotify_integration_enabled(config: Any) -> bool:
    """Return the canonical Spotify Integration availability flag.

    Spotify is a shared integration, not an Agent Team subagent.  The nested
    ``integrations.spotify.enabled`` value is therefore the only runtime
    availability switch.  The old ``spotify.enabled``/``agents.spotify``
    settings are intentionally *not* used as a fallback: migration projects
    those values into the canonical location once, and a stale legacy value
    must not make Spotify visible to the model again.
    """

    if config is None:
        return False
    value = _config_get(config, "integrations.spotify.enabled", None)
    if value is None:
        # Config objects expose dotted ``get`` in normal runtime.  Plain dicts
        # are handled by _config_get above; missing canonical state is treated
        # as disabled so fresh/partially migrated installs fail closed.
        return False
    return bool(value)


def _register_spotify_direct_tools(registry: ToolRegistry) -> bool:
    """Register Spotify's high-level direct tools as a shared integration.

    No LLM/agent is created here.  The functions are the existing Spotify
    implementation (auth, search, playback, queue, playlist, and activity)
    wrapped in AoiTalk ``ToolDefinition`` objects and owned by ``spotify`` so
    the optional Spotify tool pack can lazy-load them for the current session.
    """

    try:
        from ..tools.entertainment.spotify import (
            add_playlist_to_queue,
            add_queue_to_playlist,
            add_tracks_to_playlist,
            clear_spotify_queue,
            create_playlist,
            create_playlist_from_queue,
            get_spotify_status,
            get_spotify_user_playlists,
            pause_spotify,
            play_playlist,
            play_song_now,
            play_spotify_track,
            previous_track,
            queue_song,
            remove_from_queue,
            remove_tracks_from_playlist,
            search_spotify_music,
            set_spotify_auth_code,
            setup_spotify_auth,
            show_queue,
            skip_spotify_track,
        )
        from ..tools.memory.spotify_memory_tools import (
            get_recent_spotify_activity,
            get_spotify_activity_stats,
            get_spotify_listening_patterns,
            search_spotify_activity,
        )
    except Exception:
        # Spotify credentials/dependencies are optional.  Do not publish a
        # partially populated pack if imports fail.
        return False

    definitions = ensure_tool_definitions(
        [
            setup_spotify_auth,
            set_spotify_auth_code,
            search_spotify_activity,
            get_spotify_activity_stats,
            get_recent_spotify_activity,
            get_spotify_listening_patterns,
            search_spotify_music,
            play_spotify_track,
            play_song_now,
            queue_song,
            pause_spotify,
            skip_spotify_track,
            previous_track,
            get_spotify_status,
            show_queue,
            clear_spotify_queue,
            remove_from_queue,
            get_spotify_user_playlists,
            create_playlist,
            create_playlist_from_queue,
            add_tracks_to_playlist,
            add_queue_to_playlist,
            add_playlist_to_queue,
            remove_tracks_from_playlist,
            play_playlist,
        ]
    )
    registered = False
    for definition in definitions:
        registered = (
            _register_tool_definition(
                registry,
                definition,
                owner="spotify",
                risk="medium" if definition.name in {
                    "play_spotify_track",
                    "play_song_now",
                    "pause_spotify",
                    "skip_spotify_track",
                    "previous_track",
                    "queue_song",
                    "add_tracks_to_playlist",
                    "add_queue_to_playlist",
                    "add_playlist_to_queue",
                    "remove_tracks_from_playlist",
                } else "low",
                supports_parallel=False,
            )
            or registered
        )
    return registered


def _search_x_enabled(config: Any) -> bool:
    """Return the legacy toggle for the optional Grok X-search tool.

    ``x_search`` is the canonical direct X/Twitter search tool and is
    registered whenever the Search domain is enabled.  Keep this helper
    narrowly scoped to the backwards-compatible ``grok_x_search`` alias so
    the old ``search.x_enabled``/``search.grok_x_enabled`` settings continue
    to control Grok without hiding the canonical tool.
    """

    search_config = _config_get(config, "search", {}) or {}
    if not isinstance(search_config, dict):
        return False
    return bool(
        search_config.get(
            "x_enabled",
            search_config.get("grok_x_enabled", False),
        )
    )


def _webex_configured() -> bool:
    return all(
        str(os.getenv(key, "") or "").strip()
        for key in ("WEBEX_CLIENT_ID", "WEBEX_CLIENT_SECRET", "WEBEX_REDIRECT_URI")
    )


def _register_tool_definition(
    registry: ToolRegistry,
    tool_def: ToolDefinition,
    *,
    owner: str,
    side_effect: str = "none",
    risk: str = "low",
    requires_approval: bool = False,
    supports_parallel: bool = True,
) -> bool:
    if tool_def.name in registry:
        return False
    registry.register(
        replace(
            tool_def,
            owner=owner,
            side_effect=side_effect,
            risk=risk,
            requires_approval=requires_approval,
            supports_parallel=supports_parallel,
        )
    )
    return True


def _register_search_direct_tools(
    registry: ToolRegistry,
    *,
    config: Any,
) -> bool:
    """Expose search primitives directly to the root turn runtime."""

    from ..tools.basic.x_search import x_search_impl
    from ..tools.basic.web_search import web_search_with_config

    @tool_decorator
    def x_search(
        query: str,
        max_results: int = 8,
        timeout_seconds: int = 45,
    ) -> str:
        """Yahooリアルタイム検索を使ってXの投稿を調べます。"""

        return x_search_impl(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            config=config,
        )

    registered = _register_tool_definition(
        registry,
        x_search,
        owner="search",
        risk="medium",
        supports_parallel=False,
    )

    @tool_decorator
    def web_search(query: str) -> str:
        """Search the public web for fresh or time-sensitive information."""
        return web_search_with_config(query, config=config)

    registered = (
        _register_tool_definition(
            registry,
            web_search,
            owner="search",
            risk="medium",
            supports_parallel=False,
        )
        or registered
    )

    if _search_x_enabled(config):
        from ..tools.basic.grok_x_search import grok_x_search

        registered = (
            _register_tool_definition(
                registry,
                grok_x_search,
                owner="search",
                risk="medium",
                supports_parallel=False,
            )
            or registered
        )

    if is_knowledge_search_enabled(config):
        from ..tools.knowledge import knowledge_read, knowledge_search, knowledge_status

        for tool_def in ensure_tool_definitions(
            [knowledge_search, knowledge_read, knowledge_status]
        ):
            registered = (
                _register_tool_definition(
                    registry,
                    tool_def,
                    owner="search",
                    risk="low",
                )
                or registered
            )

    if _webex_configured():
        from ..tools.webex import (
            webex_get_thread,
            webex_list_selected_spaces,
            webex_search_messages,
        )

        for tool_def in ensure_tool_definitions(
            [
                webex_list_selected_spaces,
                webex_search_messages,
                webex_get_thread,
            ]
        ):
            registered = (
                _register_tool_definition(
                    registry,
                    tool_def,
                    owner="search",
                    risk="low",
                    supports_parallel=False,
                )
                or registered
            )

    return registered


def _register_planning_tools(registry: ToolRegistry) -> bool:
    from ..services.planning_runtime import (
        ASK_USER_QUESTION_TOOL,
        SUBMIT_PLAN_FOR_APPROVAL_TOOL,
    )

    registered = False
    for tool_def in (ASK_USER_QUESTION_TOOL, SUBMIT_PLAN_FOR_APPROVAL_TOOL):
        registered = (
            _register_tool_definition(registry, tool_def, owner="planning")
            or registered
        )
    return registered


def _register_utility_direct_tools(registry: ToolRegistry) -> bool:
    """Expose time/weather/calculation as root tools outside Agent Team.

    Utility lookups are deterministic/basic integrations and should not pay
    for a nested specialist LLM turn.  The former utility-specialist bridge
    is intentionally not registered: utility capabilities
    are shared tools and must remain outside the Agent Team topology.
    """

    try:
        from ..tools.basic import calculate, get_current_time, get_weather_info
    except Exception:
        return False
    registered = False
    for tool_def in ensure_tool_definitions(
        [get_current_time, get_weather_info, calculate]
    ):
        registered = (
            _register_tool_definition(
                registry,
                tool_def,
                owner="utility",
                risk="medium" if tool_def.name == "get_weather_info" else "low",
                supports_parallel=tool_def.name != "get_weather_info",
            )
            or registered
        )
    return registered


def _register_session_tools(
    registry: ToolRegistry,
    *,
    config: Any,
    search_enabled: bool = True,
) -> bool:
    """Expose cross-session chat history tools to the root turn runtime.

    search_past_chats は意味検索と語句検索の断片しか返せないため、
    セッション一覧と session_id 指定の本文読み出しも読み取り専用ツールとして
    合わせて公開する。
    """
    from ..tools.sessions import (
        build_explicit_read_chat_session_tool,
        list_chat_sessions,
        read_chat_session,
        search_past_chats,
    )

    memory_search_enabled = is_memory_search_enabled(config)
    if not memory_search_enabled:
        # An explicit @chat_session is a narrow read capability and must not
        # disappear merely because the broader Search agent toggle is OFF.
        # (The wrapper still requires the validated TurnContext binding.)
        session_tools = [build_explicit_read_chat_session_tool()]
    elif not search_enabled:
        return False
    else:
        session_tools = [list_chat_sessions, read_chat_session, search_past_chats]
    # Memory Search controls cross-session discovery, not an explicitly
    # mentioned session read.  In the OFF case expose only a guarded reader;
    # its wrapper requires the current TurnContext to contain the
    # server-validated ``chat_session`` reference, then delegates to the same
    # repository/ACL implementation as the normal reader.
    registered = False
    for tool_def in ensure_tool_definitions(session_tools):
        registered = (
            _register_tool_definition(
                registry,
                tool_def,
                owner="sessions",
                side_effect="none",
                risk="low",
                supports_parallel=False,
            )
            or registered
        )
    return registered


def _register_scoped_memory_tools(registry: ToolRegistry) -> bool:
    from ..services.scoped_memory_flags import scoped_memory_v2_enabled

    if not scoped_memory_v2_enabled():
        return False
    from ..tools.memory.scoped_memory_tools import SCOPED_MEMORY_TOOLS

    registered = False
    read_only = {"memory_search", "memory_get", "memory_explain"}
    for tool_def in ensure_tool_definitions(SCOPED_MEMORY_TOOLS):
        registered = (
            _register_tool_definition(
                registry,
                tool_def,
                owner="sessions",
                side_effect="none" if tool_def.name in read_only else "writes",
                risk="low" if tool_def.name in read_only else "medium",
                supports_parallel=False,
            )
            or registered
        )
    return registered


def _register_filesystem_direct_tools(registry: ToolRegistry) -> bool:
    """Expose file, workspace, repository, and command tools at root level."""

    from ..tools.file_explorer.file_explorer_tools import (
        create_workspace_directory,
        copy_workspace_item,
        delete_workspace_item,
        get_workspace_file_info,
        move_workspace_item,
        list_workspace_tree,
        upload_workspace_file,
    )
    from ..tools.os_operations import (
        append_to_file,
        create_file,
        delete_file,
        edit_file,
        execute_command,
        insert_to_file,
        list_commands,
        list_directory,
        read_command_output,
        read_file,
        search_files,
        stop_command,
        undo_edit,
        write_command_input,
    )
    from ..tools.repo_map.tools import get_repo_map
    legacy_user_file_tools = []
    if not Features.is_enterprise():
        # These legacy tools operate on the shared workspace root and have no
        # project row-lock/quota transaction.  Enterprise uses the scoped
        # project/user file APIs instead, so do not even expose the old
        # entrypoints to the model.
        from ..tools.workspaces.file_tools import (
            delete_user_file,
            download_user_file,
            get_user_file_info,
            list_user_files,
            upload_user_file,
        )

        legacy_user_file_tools = [
            upload_user_file,
            download_user_file,
            list_user_files,
            delete_user_file,
            get_user_file_info,
        ]

    registered = False
    for tool_def in ensure_tool_definitions(
        [
            create_workspace_directory,
            upload_workspace_file,
            delete_workspace_item,
            move_workspace_item,
            copy_workspace_item,
            get_workspace_file_info,
            list_workspace_tree,
            execute_command,
            read_command_output,
            write_command_input,
            stop_command,
            list_commands,
            read_file,
            create_file,
            delete_file,
            append_to_file,
            edit_file,
            insert_to_file,
            undo_edit,
            list_directory,
            search_files,
            get_repo_map,
            *legacy_user_file_tools,
        ]
    ):
        is_mutation = tool_def.name in FILESYSTEM_MUTATION_TOOL_NAMES
        is_command = tool_def.name == "execute_command"
        registered = (
            _register_tool_definition(
                registry,
                tool_def,
                owner="filesystem",
                side_effect="mutation" if is_mutation or is_command else "none",
                risk="high" if is_command else ("medium" if is_mutation else "low"),
                requires_approval=is_mutation or is_command,
                supports_parallel=tool_def.name in FILESYSTEM_READ_TOOL_NAMES
                and tool_def.name != "execute_command",
            )
            or registered
        )
    return registered


def _register_bm25_direct_tools(
    registry: ToolRegistry,
    *,
    project_context: dict[str, Any] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Expose the authorized, read-only BM25 candidate search.

    BM25 is a high-level workspace read capability rather than a replacement
    for ``search_files``/``read_file``/RepoMap.  The scope adapter owns path
    resolution and authorization; this registry only supplies the full
    server-bound runtime context and applies the common filesystem metadata.
    ``project_context`` may be hidden from the model, but must remain available
    to the adapter so an ``include_project_context=False`` turn cannot widen or
    lose its authorization boundary.
    """
    try:
        from ..services.bm25_scope import Bm25ScopeService
        from ..tools.bm25_search import build_bm25_search_tool_definition
    except Exception:
        # BM25 is optional during rolling upgrades and for minimal installs.
        import logging

        logging.getLogger(__name__).debug(
            "BM25 runtime tool is unavailable", exc_info=True
        )
        return False

    # Keep an explicit context as a construction-time fallback (for callers
    # that execute a registry outside a request), but resolve the active
    # ContextVars again for every invocation below.  CLI clients construct one
    # registry and reuse it across turns/projects; capturing the first turn's
    # service here would otherwise leak its project/App visibility into the
    # next turn.  ``get_runtime_project_context`` is server-bound and never
    # inferred from cwd.
    base_context: dict[str, Any] = (
        dict(project_context) if isinstance(project_context, dict) else {}
    )

    def _context_for_turn() -> dict[str, Any]:
        current_context = get_runtime_project_context()
        if isinstance(current_context, dict):
            # An explicitly bound empty mapping must stay empty: falling back
            # to a prior constructor context could widen a request boundary.
            runtime_context: dict[str, Any] = dict(current_context)
        else:
            runtime_context = dict(base_context)

        # A registry can be built by a provider constructor before a Project
        # context is resolved.  TurnContext values are trusted request-bound
        # identity inputs; only fill missing values so a richer server-resolved
        # runtime context (including App/release metadata) remains authoritative.
        try:
            turn = get_turn_context()
        except Exception:
            return runtime_context

        turn_user_id = str(getattr(turn, "user_id", None) or "").strip()
        context_user_id = str(
            runtime_context.get("user_id")
            or runtime_context.get("authenticated_user_id")
            or ""
        ).strip()
        if (
            turn_user_id
            and context_user_id
            and turn_user_id.casefold() != context_user_id.casefold()
        ):
            raise PermissionError("BM25 runtime user identity mismatch")

        turn_project_id = str(getattr(turn, "project_id", None) or "").strip()
        context_project_value = runtime_context.get("project_id")
        if context_project_value is None and not (
            runtime_context.get("app_id") or runtime_context.get("active_app_id")
        ):
            context_project_value = runtime_context.get("id")
        if context_project_value is None and isinstance(
            runtime_context.get("project"), dict
        ):
            context_project_value = runtime_context["project"].get("id")
        context_project_id = str(context_project_value or "").strip()
        if (
            turn_project_id
            and context_project_id
            and turn_project_id.casefold() != context_project_id.casefold()
        ):
            raise PermissionError("BM25 runtime Project identity mismatch")

        if turn_user_id and not context_user_id:
            runtime_context["user_id"] = turn_user_id
        if turn_project_id and not context_project_id:
            runtime_context["project_id"] = turn_project_id
        return runtime_context

    runtime_context = _context_for_turn()

    try:
        service = Bm25ScopeService(
            context=runtime_context,
            workspace_root=workspace_root,
        )
        tool_def = build_bm25_search_tool_definition(
            service=service,
            context=runtime_context,
            workspace_root=workspace_root,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "BM25 runtime tool could not be initialized", exc_info=True
        )
        return False

    def _bm25_search(
        query: str,
        scope: str = "auto",
        path: str = "",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Execute BM25 against the current request's fresh authorization context.

        The high-level tool is intentionally registered once for CLI clients,
        while its service is request-scoped.  Rebuilding the adapter at call
        time also forces the Project/App ACL and pinned release checks to run
        against the current identity instead of a stale constructor snapshot.
        """
        current_service = Bm25ScopeService(
            context=_context_for_turn(),
            workspace_root=workspace_root,
        )
        return current_service.search(
            query,
            scope=scope,
            path=path,
            max_results=max_results,
        )

    # Preserve the builder's frozen schema/metadata while replacing only its
    # captured service callback with the request-scoped implementation above.
    tool_def = replace(tool_def, function=_bm25_search, is_async=False)

    return _register_tool_definition(
        registry,
        tool_def,
        owner="filesystem",
        side_effect="none",
        risk="low",
        requires_approval=False,
        supports_parallel=True,
    )


def _register_project_management_direct_tools(
    registry: ToolRegistry,
    *,
    config: Any,
) -> bool:
    """Expose project-management tools directly to the root turn runtime.

    Direct tools let the parent model perform normal multi-step work itself:
    inspect project state, read configured project files, update the project DB,
    and continue after tool results without depending on another internal loop.
    """
    if not _project_management_direct_tools_enabled(config, True):
        return False

    try:
        from ..agents.project_management_agent import ProjectManagementAgent
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "ProjectManagementAgent tools could not be loaded",
            exc_info=True,
        )
        return False

    agent = ProjectManagementAgent(model=_configured_model(config)).agent
    direct_tool_available = False
    for tool_def in agent.tools:
        if tool_def.name in registry:
            direct_tool_available = True
            continue
        is_mutation = tool_def.name in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
        registry.register(
            replace(
                tool_def,
                owner="project_management",
                side_effect="mutation" if is_mutation else "none",
                risk="medium" if is_mutation else "low",
                requires_approval=is_mutation,
                supports_parallel=False,
            )
        )
        direct_tool_available = True
    return direct_tool_available


def _register_docs_direct_tools(registry: ToolRegistry) -> bool:
    """Expose AoiTalk Docs read/write tools directly to the root turn runtime."""

    try:
        from ..tools.docs_direct import (
            DOCS_MUTATION_TOOL_NAMES,
            build_docs_direct_tools,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Docs direct tools could not be loaded",
            exc_info=True,
        )
        return False

    registered = False
    for tool_def in ensure_tool_definitions(build_docs_direct_tools()):
        is_mutation = tool_def.name in DOCS_MUTATION_TOOL_NAMES
        registered = (
            _register_tool_definition(
                registry,
                tool_def,
                owner="docs",
                side_effect="mutation" if is_mutation else "none",
                risk="medium" if is_mutation else "low",
                requires_approval=is_mutation,
                supports_parallel=not is_mutation,
            )
            or registered
        )
    return registered


def _register_delegation_tool(
    registry: ToolRegistry,
    *,
    tool_name: str,
    description: str,
    runner: Any,
    config: Any | None = None,
    client: Any = None,
    owner: str = "core",
) -> None:
    if tool_name in registry:
        return

    async def _delegate(request: str) -> str:
        decision = check_tool_call_allowed(
            tool_name,
            user_input=get_current_user_input(),
            tool_args={"request": request},
            config=config,
        )
        if not decision.allowed:
            print(f"[ToolPolicy] blocked {tool_name}: {decision.reason}")
            return format_blocked_tool_result(tool_name, decision)

        runtime_project_context = get_runtime_project_context()
        project_context = _model_visible_project_context(
            runtime_project_context,
            client=client,
        )
        # ``None`` means “use the ambient runtime context” to specialist
        # runners.  Pass an explicit empty model context when only the
        # selected Project was hidden, so direct client-flag OFF cannot
        # reintroduce rich context through that fallback.
        if runtime_project_context and project_context is None:
            project_context = {}
        return await runner.run_async(
            request,
            project_context=project_context,
        )

    _delegate.__name__ = tool_name
    _delegate.__doc__ = description
    registry.register(replace(tool_decorator(_delegate), owner=owner))


def _agent_team_scope_for_request(
    config: Any,
    *,
    client: Any = None,
    project_context: dict[str, Any] | None = None,
    loaded_team_ids: frozenset[str] | set[str] | None = None,
    contextual_scope: Any = None,
) -> dict[str, Any]:
    """Resolve Team activation from one canonical structured-context helper."""

    if contextual_scope is not None:
        return contextual_scope
    return contextual_agent_team_scope(
        config,
        client=client,
        project_context=project_context,
        loaded_team_ids=loaded_team_ids or loaded_agent_team_ids(_session_identity(client)),
    )


def _agent_team_delegate_roster(
    config: Any,
    *,
    client: Any = None,
    project_context: dict[str, Any] | None = None,
    loaded_team_ids: frozenset[str] | set[str] | None = None,
    contextual_scope: Any = None,
) -> tuple[str, set[str], set[str]]:
    """Return contextual roster text and canonical active Team/Subagent IDs."""

    configured_teams = agent_team_v3_teams(config)
    if not configured_teams:
        return "", set(), set()
    scope = _agent_team_scope_for_request(
        config,
        client=client,
        project_context=project_context,
        loaded_team_ids=loaded_team_ids,
        contextual_scope=contextual_scope,
    )
    active_team_ids = set(scope.get("active_team_ids") or [])
    active_visible_ids = set(scope.get("active_subagent_ids") or [])
    visible_ids = set(active_visible_ids)
    # Manual Teams remain discoverable so Main can call ``load_agent_team``;
    # they are listed alongside active contextual Teams but are not added to
    # ``active_team_ids`` and therefore remain rejected until explicitly
    # loaded.  Inactive contextual Teams are never used as a fallback roster.
    manual_team_ids = {
        str(team.get("team_id") or "")
        for team in configured_teams
        if str((team.get("activation") or {}).get("mode") or "always")
        .strip()
        .lower()
        == "manual"
    }
    for team in configured_teams:
        if str(team.get("team_id") or "") in manual_team_ids:
            visible_ids.update(str(item) for item in team.get("subagent_ids") or [])
    if not visible_ids:
        return "", active_team_ids, set()

    active_subagent_ids = {
        str(item.get("subagent_id") or "")
        for item in agent_team_v3_subagents(config, include_disabled=False)
        if str(item.get("subagent_id") or "") in active_visible_ids
    }
    subagents_by_id = {
        str(item.get("subagent_id") or ""): item
        for item in agent_team_v3_subagents(config, include_disabled=False)
    }
    roster_lines: list[str] = []
    for team_config in configured_teams:
        if not team_config.get("enabled", True):
            continue
        team_id = str(team_config.get("team_id") or "")
        activation_mode = str(
            (team_config.get("activation") or {}).get("mode") or "always"
        ).strip().lower()
        if team_id not in active_team_ids and activation_mode != "manual":
            continue
        team_name = str(team_config.get("name") or team_id or "Team")
        subagents: list[str] = []
        for sid in team_config.get("subagent_ids") or []:
            item = subagents_by_id.get(str(sid))
            if not item or str(sid) not in visible_ids:
                continue
            label = str(item.get("name") or sid)
            summary = str(item.get("description") or "").strip()
            subagents.append(f"- {label}: {summary}" if summary else f"- {label}")
        if subagents:
            roster_lines.append(f"{team_name}\n" + "\n".join(subagents))
    return "\n\n".join(roster_lines), active_team_ids, active_subagent_ids


def specialize_agent_team_delegate_definition(
    definition: ToolDefinition,
    *,
    config: Any,
    client: Any = None,
    project_context: dict[str, Any] | None = None,
    contextual_scope: Any = None,
) -> ToolDefinition:
    """Clone ``agent_team_delegate`` with a request-local roster description.

    Provider registries are often reused between turns.  Mutating the shared
    ToolDefinition here would let concurrent clients overwrite one another's
    schema, so only the copy returned to the exposure layer is specialized.
    """

    if definition.name != "agent_team_delegate":
        return definition
    roster_text, _active_team_ids, _active_subagents = _agent_team_delegate_roster(
        config,
        client=client,
        project_context=project_context,
        contextual_scope=contextual_scope,
    )
    base_description = str(definition.description or "").split(
        "\n\nAvailable Agent Teams and Subagents:",
        1,
    )[0]
    description = base_description
    if roster_text:
        description = f"{base_description}\n\nAvailable Agent Teams and Subagents:\n{roster_text}"
    return replace(definition, description=description)


def _register_agent_team_delegate_tool(
    registry: ToolRegistry,
    *,
    config: Any,
    client: Any = None,
    project_context: dict[str, Any] | None = None,
    trusted_parent_context: TrustedParentRunContext | None = None,
    qa_browser_coordinator: Any = None,
) -> None:
    if "agent_team_delegate" in registry:
        return
    configured_teams = agent_team_v3_teams(config)
    if not configured_teams:
        return
    initial_project_context = project_context
    roster_text, _active_team_ids, active_subagents = _agent_team_delegate_roster(
        config,
        client=client,
        project_context=project_context,
    )
    has_manual_team = any(
        str((team.get("activation") or {}).get("mode") or "always")
        .strip()
        .lower()
        == "manual"
        for team in configured_teams
    )
    enabled_subagent_ids = {
        str(item.get("subagent_id") or "")
        for item in agent_team_v3_subagents(config, include_disabled=False)
        if str(item.get("subagent_id") or "").strip()
    }
    has_future_contextual_team = any(
        bool(team.get("enabled", True))
        and bool(
            enabled_subagent_ids
            & {
                str(item).strip()
                for item in (team.get("subagent_ids") or [])
                if str(item).strip()
            }
        )
        for team in configured_teams
    )
    if not agent_team_v3_delegation_enabled(config) or (
        not active_subagents and not has_manual_team and not has_future_contextual_team
    ):
        return
    # The public description is generated from the saved Team/Subagent graph;
    # do not bake fixed Team/Subagent enums or internal IDs into the prompt contract.
    subagents_by_id = {
        str(item.get("subagent_id") or ""): item
        for item in agent_team_v3_subagents(config, include_disabled=False)
    }

    async def _agent_team_delegate_impl(
        team: str = "",
        subagent: str = "",
        task: str = "",
        instances: int = 1,
        work_mode: str = "read",
        scopes: Optional[list[str]] = None,
    ) -> str:
        """Delegate bounded work to a Subagent in an available Agent Team.

        Args:
            team: Agent Team name or stable ID from the available Team roster.
            subagent: Subagent name or stable ID listed under that Team.
            task: The bounded assignment for the teammate(s).
            instances: How many instances to run. Clamped by the Subagent limits.
            work_mode: Requested workspace mode (`read` or `write`), bounded by the Subagent.
            scopes: One independent scope per instance. Required when instances resolves to more than one.
        """
        # A v3 Subagent is a leaf worker.  The role ContextVar is set by the
        # specialist runner around the complete child turn; checking it here
        # prevents a child from invoking this tool even if an extension or a
        # stale provider registry attempted to publish the definition.
        current_role = str(get_current_agent_team_role() or "").strip()
        if current_role:
            return (
                "Agent Team child workers cannot delegate nested workers; "
                "return the bounded WorkerReport to the parent coordinator."
            )
        requested_team_id = str(team or "").strip()
        requested_subagent_id = str(subagent or "").strip()
        tools_required = True
        decision = check_tool_call_allowed(
            "agent_team_delegate",
            user_input=get_current_user_input(),
            tool_args={
                "team": requested_team_id,
                "subagent": requested_subagent_id,
                "task": task,
                "instances": instances,
                "work_mode": work_mode,
                "tools_required": tools_required,
                "scopes": scopes,
            },
            config=config,
        )
        if not decision.allowed:
            print(f"[ToolPolicy] blocked agent_team_delegate: {decision.reason}")
            return format_blocked_tool_result("agent_team_delegate", decision)

        requested_work_mode = str(work_mode or "read").strip().lower()
        if requested_work_mode not in {"read", "write"}:
            return "Agent Team work_mode must be read or write"

        if not requested_team_id or not requested_subagent_id:
            return "Agent Team delegation requires both team and subagent"
        configured_team = next(
            (item for item in configured_teams
             if str(item.get("team_id") or "") == requested_team_id
             or str(item.get("name") or "").casefold() == requested_team_id.casefold()),
            None,
        )
        if not configured_team:
            return f"Agent Team is not available: {requested_team_id}"
        if not configured_team.get("enabled", True):
            return f"Agent Team is disabled: {requested_team_id}"
        scope = _agent_team_scope_for_request(
            config,
            client=client,
            project_context=(
                get_runtime_project_context()
                if runtime_project_context_is_bound()
                else initial_project_context
            ),
        )
        active_team_ids = set(scope.get("active_team_ids") or [])
        selected_team_id = str(configured_team.get("team_id") or requested_team_id)
        if selected_team_id not in active_team_ids:
            activation_mode = str((configured_team.get("activation") or {}).get("mode") or "always")
            if activation_mode == "manual":
                return f"Agent Team {requested_team_id} is not loaded for this conversation; call load_agent_team first"
            return f"Agent Team {requested_team_id} is not active in the current context"
        subagent = subagents_by_id.get(requested_subagent_id)
        if subagent is None:
            subagent = next(
                (
                    item for item in subagents_by_id.values()
                    if str(item.get("name") or "").casefold()
                    == requested_subagent_id.casefold()
                ),
                None,
            )
        if not subagent:
            return f"Subagent is not available: {requested_subagent_id}"
        canonical_subagent_id = str(subagent.get("subagent_id") or requested_subagent_id)
        if canonical_subagent_id not in {
            str(item).strip() for item in (configured_team.get("subagent_ids") or [])
        }:
            return f"Subagent {requested_subagent_id} is not a subagent of Agent Team {requested_team_id}"
        if not subagent.get("enabled", True):
            return f"Subagent is disabled: {requested_subagent_id}"
        subagent_id = canonical_subagent_id
        if requested_work_mode == "write" and not agent_team_subagent_allows_write(subagent):
            return f"Subagent {subagent_id} does not allow write mode"

        # Repository-capable child turns may only be started by a parent
        # controller that supplied the opaque scope capability.  In
        # particular, never derive a target root from ``task`` or arbitrary
        # project metadata.  Read-only/non-workspace specialists retain the
        # historical unscoped path.
        parent_agent_run_id = get_current_agent_run_id()
        bound_project_context = (
            get_runtime_project_context()
            if runtime_project_context_is_bound()
            else None
        )
        marker_context = (
            bound_project_context
            if isinstance(bound_project_context, dict)
            and TRUSTED_PARENT_CONTEXT_KEY in bound_project_context
            else initial_project_context
        )
        trusted_scope_context = resolve_trusted_parent_run_context(
            trusted_parent_context
            or (
                marker_context.get(TRUSTED_PARENT_CONTEXT_KEY)
                if isinstance(marker_context, dict)
                else None
            ),
            parent_run_id=parent_agent_run_id,
        )
        workspace_ceiling = agent_team_workspace_access(subagent)
        requires_repository_scope = bool(
            requested_work_mode == "write" and workspace_ceiling == "write"
        )
        if requires_repository_scope and trusted_scope_context is None:
            return (
                "Agent Team delegation blocked: parent controller must provide "
                "an immutable AgentRunScope for repository work"
            )

        # For an explicitly requested Docs specialist, preserve the user's
        # exact assignment when crossing the root→child boundary.  The root
        # model may enrich a task with a guessed canonical node (especially
        # when two titles are ambiguous); that defeats the specialist's
        # no-guess/no-write contract.  Passing the original wording lets the
        # docs_operator inspect the graph and return structured ambiguity
        # without a mutation.  Child calls are excluded by the Subagent scope.
        original_user_input = get_current_user_input()
        explicit_docs_root_request = bool(
            subagent_id == "docs_operator"
            and not get_current_agent_team_role()
            and original_user_input
            and looks_like_docs_agent_delegation_request(original_user_input)
        )
        if explicit_docs_root_request and _DOCS_ROOT_DELEGATION_COMPLETED.get():
            return (
                "Docs specialist delegation already completed for this turn; "
                "present its result to the user without starting another child run."
            )
        if explicit_docs_root_request:
            task = str(original_user_input)

        try:
            max_instances = max(1, int(subagent.get("max_instances") or 1))
        except (TypeError, ValueError):
            max_instances = 1
        default_instances = max(1, min(int(subagent.get("default_instances") or 1), max_instances))
        try:
            requested_instances = int(instances)
        except (TypeError, ValueError):
            requested_instances = default_instances
        count = max(1, min(requested_instances, max_instances)) if subagent.get("scalable", False) else 1
        if count <= 0:
            return f"Agent Team Subagent is disabled: {subagent_id}"

        normalized_scopes = [
            str(scope or "").strip()
            for scope in (scopes or [])
        ]
        if count > 1 and (
            len(normalized_scopes) != count
            or any(not scope for scope in normalized_scopes)
            or len(set(normalized_scopes)) != len(normalized_scopes)
        ):
            return (
                f"Agent Team instances={count} requires exactly {count} non-empty scopes. "
                "Provide one independent scope in scopes for each teammate; do not duplicate the same assignment."
            )

        raw_capabilities = tuple(
            dict.fromkeys(
                str(capability).strip()
                for capability in (subagent.get("capability_ids") or [])
                if str(capability).strip()
            )
        )
        route = resolve_agent_team_v3_route(config, subagent_id) or {}
        backend = str(
            route.get("backend")
            or ("cli" if str(route.get("provider") or "").endswith("-cli") else "api")
        )
        declared_capabilities = tuple(
            filter_agent_team_capabilities(
                subagent,
                requested=raw_capabilities or None,
                work_mode=requested_work_mode,
                backend=backend,
            )
        ) if tools_required else ()

        # Child execution needs the complete server-resolved runtime context
        # (user principal, App identity/ACL and selected Project) so high-level
        # App closures can authorize against the same request scope as the
        # root.  ``SpecialistDelegationRunner`` keeps this internal context
        # separate from its model-facing prompt and suppresses the selected
        # Project when ``include_project_context`` is OFF.  Resolve through
        # the per-invocation helper rather than the construction-time mapping
        # so a fixed registry cannot leak a prior turn's principal.
        runtime_project_context = _trusted_runtime_project_context(
            initial_project_context
        )
        if trusted_scope_context is None:
            # A raw scope-looking field is not an authority.  This also
            # prevents a model-supplied absolute path from reaching the
            # SpecialistDelegationRunner.
            project_context = _remove_untrusted_run_scope_fields(
                runtime_project_context
            )
        else:
            try:
                project_context = inject_trusted_parent_scope(
                    runtime_project_context,
                    trusted_scope_context,
                    parent_run_id=parent_agent_run_id,
                )
            except Exception:
                # The capability is parent-owned; any mismatch is a hard
                # denial rather than a fallback to an unscoped worker.
                return (
                    "Agent Team delegation blocked: trusted parent repository "
                    "scope does not match this AgentRun"
                )
        route = route or {}
        label = str(subagent.get("name") or subagent.get("label") or subagent_id)
        provider = str(route.get("provider") or subagent.get("provider") or "").strip()
        model = str(route.get("model") or subagent.get("model") or "").strip()
        mode = str(route.get("effort") or route.get("reasoning_effort") or route.get("mode") or "").strip()
        agent_run_service = AgentRunService() if parent_agent_run_id else None
        delegation_id = str(uuid4())
        instance_route_metadata: dict[int, dict[str, Any]] = {}
        instance_reports: dict[int, dict[str, Any]] = {}
        # インスタンスごとに子 AgentRun を作り、タイムラインから委譲先へ辿れるようにする。
        child_run_ids: dict[int, str] = {}
        breaker_key = f"{parent_agent_run_id or 'no-parent'}:{subagent_id}"
        breaker_store = dict(_ROOT_TOOL_FAILURE_BREAKERS.get() or {})
        failure_breaker = breaker_store.get(breaker_key)
        if not isinstance(failure_breaker, ToolFailureCircuitBreaker):
            failure_breaker = ToolFailureCircuitBreaker(
                max_same_failure=2,
                failed_tool_budget=max(4, count * 2),
            )
            breaker_store[breaker_key] = failure_breaker
            _ROOT_TOOL_FAILURE_BREAKERS.set(breaker_store)
        continuation_state = AgentContinuationState(
            original_goal=str(task),
            selected_project_id=str((project_context or {}).get("id") or "") or None,
            scope={"team_id": selected_team_id, "subagent_id": subagent_id},
        )
        # Main's Docs routing prompt carries canonical ``@docs:<uuid>`` refs.
        # Seed the continuation snapshot before the child starts so a parent
        # interrupt can preserve resolved identities even when the child is
        # cancelled inside its first read/tool call.
        task_refs = tuple(
            dict.fromkeys(
                match.group(1)
                for match in re.finditer(
                    r"@docs:([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})",
                    str(task or ""),
                )
            )
        )
        if task_refs:
            continuation_state.resolved_node_ids = task_refs
            # The final canonical ref in the routing task is the destination
            # parent for the Docs create/update operation.
            continuation_state.pending_destination_parent_id = task_refs[-1]
        # Bind in the delegate task's context.  Do not reset this binding in
        # the normal return path: the response handler catches
        # GenerationInterrupted one frame above and needs the snapshot to
        # render the deterministic retry prompt.
        set_current_continuation_state(continuation_state)
        # Each delegation gets an independent late-result fence.  A cancelled
        # provider thread may continue after its asyncio task is cancelled;
        # managed Docs mutations consult this gate immediately before commit.
        mutation_gate = GenerationMutationGate()
        set_current_generation_mutation_gate(mutation_gate)

        async def _ensure_child_run(index: int) -> str | None:
            if not agent_run_service or not parent_agent_run_id:
                return None
            existing = child_run_ids.get(index)
            if existing:
                return existing
            instance_label = f"{label}-{index}"
            try:
                child = await agent_run_service.create_run(
                    parent_run_id=parent_agent_run_id,
                    run_type="agent_team_delegate",
                    title=instance_label,
                    objective=task,
                    provider=provider or None,
                    model=model or None,
                    metadata={
                        "team_id": selected_team_id,
                        "subagent_id": subagent_id,
                        "llm_profile_id": None,
                        "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
                        "route_source": str(route.get("route_source") or "main_inherit") or None,
                        "agent_instance_key": f"{subagent_id}-{index}",
                        "agent_label": instance_label,
                        "delegation_id": delegation_id,
                        "instance_index": index,
                        "instance_count": count,
                        "scope": normalized_scopes[index - 1] if normalized_scopes else None,
                        "capabilities": list(declared_capabilities),
                        "backend": str(route.get("backend") or ("cli" if provider.endswith("-cli") else "api")),
                        "work_mode": requested_work_mode,
                        "workspace_access": str(subagent.get("max_workspace_access") or "none"),
                        "worker_report_schema": WORKER_REPORT_SCHEMA_VERSION,
                        "worker_report": normalize_worker_report(
                            None,
                            task=task,
                            parent_run_id=parent_agent_run_id,
                        ),
                        "publication": parent_publication_metadata(
                            parent_run_id=parent_agent_run_id,
                        ),
                        **(
                            trusted_scope_context.child_metadata()
                            if trusted_scope_context is not None
                            else {}
                        ),
                    },
                )
            except Exception as exc:
                print(f"[AgentTeam] child run create failed: {exc}")
                return None
            child_run_id = str((child or {}).get("id") or "")
            if not child_run_id:
                return None
            child_run_ids[index] = child_run_id
            try:
                await agent_run_service.create_edge(
                    parent_run_id=parent_agent_run_id,
                    child_run_id=child_run_id,
                    purpose="agent_team_delegate",
                    metadata={
                        "team_id": selected_team_id,
                        "subagent_id": subagent_id,
                        "llm_profile_id": None,
                        "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
                        "route_source": str(route.get("route_source") or "main_inherit") or None,
                        "instance_index": index,
                        "delegation_id": delegation_id,
                        "worker_report_schema": WORKER_REPORT_SCHEMA_VERSION,
                        "publication": parent_publication_metadata(
                            parent_run_id=parent_agent_run_id,
                        ),
                    },
                )
            except Exception as exc:
                print(f"[AgentTeam] child run edge create failed: {exc}")
            return child_run_id

        def _instance_payload(index: int) -> dict[str, Any]:
            instance_label = f"{label}-{index}"
            effective_backend = str(
                route.get("backend")
                or ("cli" if provider.endswith("-cli") else "api")
            )
            payload = {
                "actor_type": "agent_team",
                "child_run_id": child_run_ids.get(index),
                "actor_key": subagent_id,
                "team_id": selected_team_id,
                "subagent_id": subagent_id,
                "llm_profile_id": None,
                "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
                "route_source": str(route.get("route_source") or "main_inherit") or None,
                "agent_instance_key": f"{subagent_id}-{index}",
                "delegation_id": delegation_id,
                "operation_id": f"agent:{delegation_id}:{index}",
                "actor_label": instance_label,
                "agent_label": instance_label,
                "provider": provider or None,
                "model": model or None,
                "mode": mode or None,
                "reasoning_effort": mode or None,
                "backend": effective_backend,
                "work_mode": requested_work_mode,
                "workspace_access": str(subagent.get("max_workspace_access") or "none"),
                "instance_index": index,
                "instance_count": count,
                "task": task,
                "scope": normalized_scopes[index - 1] if normalized_scopes else None,
                "capabilities": list(declared_capabilities),
                "routing_profile_id": route.get("routing_profile_id"),
                "pool_id": route.get("pool_id"),
                "worker_report_schema": WORKER_REPORT_SCHEMA_VERSION,
                "publication": parent_publication_metadata(
                    parent_run_id=parent_agent_run_id,
                ),
            }
            report = instance_reports.get(index) or normalize_worker_report(
                None,
                task=task,
                parent_run_id=parent_agent_run_id,
            )
            payload["worker_report"] = report
            payload["publication"] = report.get(
                "publication",
                parent_publication_metadata(parent_run_id=parent_agent_run_id),
            )
            return payload

        async def _record_instance_event(
            event_type: str,
            index: int,
            *,
            status: str,
            message: str,
            extra: dict[str, Any] | None = None,
        ) -> None:
            if not agent_run_service or not parent_agent_run_id:
                return
            payload = _instance_payload(index)
            if extra:
                payload.update(extra)
            try:
                await agent_run_service.record_event(
                    parent_agent_run_id,
                    event_type,
                    status=status,
                    message=message,
                    payload=payload,
                )
            except Exception as exc:
                print(f"[AgentTeam] timeline event record failed: {exc}")

        async def _close_child_edge(index: int, status: str) -> None:
            """Persist terminal status for the durable parent/child edge."""

            child_run_id = child_run_ids.get(index)
            close_edge = getattr(agent_run_service, "close_edge", None)
            if not child_run_id or not agent_run_service or not callable(close_edge):
                return
            try:
                await close_edge(
                    parent_run_id=parent_agent_run_id,
                    child_run_id=child_run_id,
                    status=status,
                )
            except Exception as exc:
                print(f"[AgentTeam] child run edge close failed: {exc}")

        async def _finalize_parent_cancellation() -> None:
            """Close every child run when the parent delegation is cancelled."""
            # This is a cooperative immediate-interrupt boundary, not an
            # explicit user cancellation.  Keep the state resumable and make
            # the terminal tool event carry the canonical IDs/P destination.
            continuation_state.mutation_state = "interrupted"
            for index in range(1, count + 1):
                child_run_id = child_run_ids.get(index)
                if child_run_id and agent_run_service:
                    try:
                        await agent_run_service.cancel_run(
                            child_run_id,
                            message=f"{label}-{index} の実行が親委譲のキャンセルで停止しました",
                        )
                    except Exception as exc:
                        print(f"[AgentTeam] child run parent-cancel update failed: {exc}")
                await _close_child_edge(index, "cancelled")
                await _record_instance_event(
                    "agent_team.instance_cancelled",
                    index,
                    status="cancelled",
                    message=f"{label}-{index} の実行が親委譲のキャンセルで停止しました",
                    extra={
                        "error": "parent delegation cancelled",
                        **instance_route_metadata.get(index, {}),
                    },
                )
                # A cancelled delegation still closes the active tool
                # lifecycle.  Persist a compact continuation snapshot so the
                # next user message can resume with resolved identities.
                await _record_instance_event(
                    "tool_end",
                    index,
                    status="cancelled",
                    message=f"{label}-{index} tool cancelled by user interrupt",
                    extra=cancelled_tool_terminal_event(
                        subagent_id,
                        reason="user_interrupt",
                        state=continuation_state,
                    ),
                )

        try:
            for index in range(1, count + 1):
                child_run_id = await _ensure_child_run(index)
                if child_run_id and agent_run_service:
                    try:
                        await agent_run_service.mark_running(
                            child_run_id,
                            message=f"{label}-{index} を実行しています",
                            provider=provider or None,
                            model=model or None,
                        )
                    except Exception as exc:
                        print(f"[AgentTeam] child run start update failed: {exc}")
                await _record_instance_event(
                    "agent_team.instance_started",
                    index,
                    status="running",
                    message=f"{label}-{index} を実行しています",
                )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(_finalize_parent_cancellation())
            except Exception as exc:
                print(f"[AgentTeam] setup cancellation finalization failed: {exc}")
            raise

        async def _run_one(index: int) -> str:
            runner_kwargs = {
                "subagent_id": subagent_id,
                "team_id": selected_team_id,
                "llm_profile_id": None,
                "display_name": f"{label}-{index}",
                "tool_required": bool(tools_required),
                "capabilities": declared_capabilities,
            }
            # Keep the delegation seam compatible with injected runners while
            # allowing older extensions to omit optional work_mode/team_id.
            try:
                runner_params = inspect.signature(
                    AgentTeamSubagentDelegationRunner
                ).parameters
            except (TypeError, ValueError):
                runner_params = {}
            if "work_mode" in runner_params:
                runner_kwargs["work_mode"] = requested_work_mode
            runner = AgentTeamSubagentDelegationRunner(config, **runner_kwargs)
            scope = (
                normalized_scopes[index - 1]
                if normalized_scopes
                else "single assigned scope"
            )
            scoped_task = (
                f"You are {label}-{index} in the Agent Team.\n"
                f"Subagent: {subagent_id}\n"
                f"Team size for this delegation: {count}\n"
                f"Independent scope: {scope}\n"
                "Coordinate by keeping your scope independent and returning a concise result.\n\n"
                f"Assignment:\n{task}"
            )
            # 委譲本体の実行中だけ current agent run を子runへ切り替える。
            # asyncio.gather が各コルーチンを Task 化する際に context が複製されるため、
            # ここでの差し替えは他インスタンスや親側の記録へ影響しない。
            # instance_started / succeeded / failed は囲みの外で親runへ記録し続ける。
            child_run_id = child_run_ids.get(index)
            run_id_token = (
                set_current_agent_run_id(child_run_id) if child_run_id else None
            )
            try:
                result = await runner.run_async(
                    scoped_task, project_context=project_context
                )
                # Keep the provider-facing result string unchanged for
                # backwards compatibility, while retaining a bounded,
                # structured WorkerReport for the parent timeline/run record.
                instance_reports[index] = normalize_worker_report(
                    result,
                    task=scoped_task,
                    parent_run_id=parent_agent_run_id,
                )
                parsed_failure = parse_structured_tool_failure(result)
                # Provider models occasionally prefix a structured Docs
                # envelope with a short ``Error:`` paraphrase.  Normalize that
                # known high-level internal failure before the breaker sees
                # it; otherwise cosmetic wording would bypass the bounded
                # retry budget.  The original user-facing text is retained
                # in the root event/result preview, while the child result is
                # represented by this compact machine-readable envelope.
                if (
                    not parsed_failure
                    and "docs" in str(result or "").casefold()
                    and "内部処理" in str(result or "")
                ):
                    result = (
                        '{"success": false, "error": "Docsの内部処理に失敗しました。", '
                        '"error_code": "docs_access_internal", "retryable": false}'
                    )
                    parsed_failure = parse_structured_tool_failure(result)
                    instance_reports[index] = normalize_worker_report(
                        result,
                        task=scoped_task,
                        parent_run_id=parent_agent_run_id,
                    )
                if parsed_failure:
                    decision = failure_breaker.check(
                        tool_failure_family(subagent_id),
                        parsed_failure,
                    )
                    if not decision.allowed:
                        await _record_instance_event(
                            "agent_team.tool_circuit_opened",
                            index,
                            status="failed",
                            message="同じ内部Tool障害の再試行を抑制しました",
                            extra={
                                "error_code": parsed_failure["error_code"],
                                "failure_signature": decision.signature.key if decision.signature else None,
                                "retry_suppressed_count": failure_breaker.snapshot().get("suppressed", {}).get(decision.signature.key if decision.signature else "", 0),
                            },
                        )
                        return f"{label}-{index} delegation error: internal tool failure circuit open"
                return result
            finally:
                if run_id_token is not None:
                    reset_current_agent_run_id(run_id_token)
                instance_route_metadata[index] = dict(
                    getattr(runner, "route_metadata", {}) or {}
                )

        async def _gather_child_results() -> list[Any]:
            return await asyncio.gather(
                *[_run_one(index + 1) for index in range(count)],
                return_exceptions=True,
            )

        async def _wait_for_parent_interrupt() -> str:
            """Poll the run-scoped token without blocking the event loop."""

            handle = get_current_generation_cancellation()
            if handle is None:
                await asyncio.Event().wait()
            while handle is not None:
                if handle.cancel_requested.is_set():
                    return "cancel"
                # reserve_interrupt sets the wake-up Event before the durable
                # steer receipt is committed.  Wait until at least one
                # reservation is accepted so a persistence race cannot cancel
                # a child for an uncommitted instruction.
                if handle.interrupt_requested.is_set():
                    with handle._interrupt_lock:
                        accepted = any(
                            reservation.accepted is True
                            for reservation in handle._interrupt_reservations
                        )
                    if accepted:
                        return "interrupt"
                await asyncio.sleep(0.02)
            await asyncio.Event().wait()
            return "interrupt"

        gather_task = asyncio.create_task(_gather_child_results())
        interrupt_task = asyncio.create_task(_wait_for_parent_interrupt())
        try:
            done, _pending = await asyncio.wait(
                {gather_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if gather_task in done:
                interrupt_task.cancel()
                await asyncio.gather(interrupt_task, return_exceptions=True)
                results = gather_task.result()
            else:
                reason = str(interrupt_task.result() or "interrupt")
                # Fence any synchronous worker before cancelling its awaitable;
                # its late Docs tool calls will fail closed at commit time.
                mutation_gate.block(reason)
                gather_task.cancel()
                await asyncio.gather(gather_task, return_exceptions=True)
                try:
                    await asyncio.shield(_finalize_parent_cancellation())
                except Exception as exc:
                    print(f"[AgentTeam] interrupt finalization failed: {exc}")
                if reason == "cancel":
                    raise asyncio.CancelledError()
                # Propagate through the native tool router to the response
                # handler, which applies the steer as a continuation delta.
                try:
                    raise_if_generation_interrupted()
                except GenerationInterrupted as interrupt:
                    # The parent generation resets attempt-local ContextVars
                    # in its finally block.  Keep the bounded snapshot on the
                    # exception so the response handler can still construct
                    # the deterministic continuation prompt after that reset.
                    interrupt.continuation_state = continuation_state
                    raise
                raise GenerationInterrupted(continuation_state=continuation_state)
        except asyncio.CancelledError:
            # gather cancels its children before propagating.  Finalize the
            # durable lifecycle records before allowing the parent cancel to
            # escape, otherwise child runs remain stuck in ``running``.
            mutation_gate.block("parent_cancelled")
            if not gather_task.done():
                gather_task.cancel()
            await asyncio.gather(gather_task, return_exceptions=True)
            interrupt_task.cancel()
            await asyncio.gather(interrupt_task, return_exceptions=True)
            try:
                await asyncio.shield(_finalize_parent_cancellation())
            except Exception as exc:
                print(f"[AgentTeam] parent cancellation finalization failed: {exc}")
            raise

        def _result_status(result: Any, index: int) -> str:
            if isinstance(result, asyncio.CancelledError):
                return "cancelled"
            if isinstance(result, BaseException):
                return "failed"
            text = str(result or "").strip().casefold()
            prefix = f"{label}-{index}".casefold()
            label_prefix = str(label or "").strip().casefold()
            subagent_prefix = str(subagent_id or "").strip().casefold()
            cancellation_prefixes = (
                f"{prefix} delegation cancelled",
                f"{label_prefix} delegation cancelled",
                f"{subagent_prefix} delegation cancelled",
                "delegation cancelled",
            )
            if any(
                text == marker or text.startswith(marker + ":")
                for marker in cancellation_prefixes
            ):
                return "cancelled"
            failure_prefixes = (
                f"{prefix} delegation error:",
                f"{label_prefix} delegation error:",
                f"{subagent_prefix} delegation error:",
                f"{prefix} mcp is not configured",
                f"{label_prefix} mcp is not configured",
                f"{subagent_prefix} mcp is not configured",
                f"failed to initialize {prefix} mcp",
                f"failed to initialize {label_prefix} mcp",
                f"failed to initialize {subagent_prefix} mcp",
                f"{prefix} cli backend is not configured",
                f"{label_prefix} cli backend is not configured",
                f"{subagent_prefix} cli backend is not configured",
                f"{prefix} request is empty",
                f"{label_prefix} request is empty",
                f"{subagent_prefix} request is empty",
            )
            if any(text == marker or text.startswith(marker + " ") for marker in failure_prefixes):
                return "failed"
            return "succeeded"

        result_statuses = [
            _result_status(result, index)
            for index, result in enumerate(results, start=1)
        ]
        for index, result in enumerate(results, start=1):
            child_run_id = child_run_ids.get(index)
            status = result_statuses[index - 1]
            worker_report = instance_reports.get(index) or normalize_worker_report(
                result,
                task=task,
                parent_run_id=parent_agent_run_id,
            )
            report_metadata = {
                "worker_report": worker_report,
                "publication": worker_report.get(
                    "publication",
                    parent_publication_metadata(parent_run_id=parent_agent_run_id),
                ),
            }
            if status == "failed":
                if child_run_id and agent_run_service:
                    try:
                        await agent_run_service.fail_run(
                            child_run_id,
                            str(result),
                            result={
                                "output": str(result)[:20000],
                                **report_metadata,
                            },
                            metadata=report_metadata,
                        )
                    except Exception as exc:
                        print(f"[AgentTeam] child run fail update failed: {exc}")
                await _close_child_edge(index, "failed")
                await _record_instance_event(
                    "agent_team.instance_failed",
                    index,
                    status="failed",
                    message=f"{label}-{index} の実行に失敗しました",
                    extra={
                        "error": str(result),
                        **report_metadata,
                        **instance_route_metadata.get(index, {}),
                    },
                )
            elif status == "cancelled":
                if child_run_id and agent_run_service:
                    try:
                        await agent_run_service.cancel_run(
                            child_run_id,
                            message=str(result) or f"{label}-{index} の実行がキャンセルされました",
                        )
                    except Exception as exc:
                        print(f"[AgentTeam] child run cancel update failed: {exc}")
                await _close_child_edge(index, "cancelled")
                await _record_instance_event(
                    "agent_team.instance_cancelled",
                    index,
                    status="cancelled",
                    message=f"{label}-{index} の実行がキャンセルされました",
                    extra={
                        "error": str(result),
                        **report_metadata,
                        **instance_route_metadata.get(index, {}),
                    },
                )
            else:
                if child_run_id and agent_run_service:
                    try:
                        await agent_run_service.complete_run(
                            child_run_id,
                            result={
                                "output": str(result)[:20000],
                                **report_metadata,
                            },
                            message=f"{label}-{index} が完了しました",
                            metadata=report_metadata,
                        )
                    except Exception as exc:
                        print(f"[AgentTeam] child run complete update failed: {exc}")
                await _close_child_edge(index, "succeeded")
                await _record_instance_event(
                    "agent_team.instance_succeeded",
                    index,
                    status="succeeded",
                    message=f"{label}-{index} が完了しました",
                    extra={
                        "result_preview": str(result)[:1200],
                        **report_metadata,
                        **instance_route_metadata.get(index, {}),
                    },
                )

        failures = [
            result
            for result, status in zip(results, result_statuses)
            if status == "failed"
        ]
        if failures:
            first_failure = failures[0]
            if isinstance(first_failure, BaseException):
                if isinstance(first_failure, GenerationInterrupted):
                    first_failure.continuation_state = continuation_state
                raise first_failure
            raise RuntimeError(str(first_failure))

        if any(status == "cancelled" for status in result_statuses):
            return f"{label} delegation cancelled"

        successful_results = [str(result) for result in results]
        # Keep the root Docs delegation one-shot for normal/ambiguous tasks,
        # but allow the per-delegation circuit breaker to receive a second
        # structured non-retryable internal failure.  That second attempt is
        # what opens the circuit and records the suppression event in the
        # acceptance fault-injection path; ambiguity/user-validation results
        # remain one-shot and are returned directly to Main.
        structured_internal_failure = any(
            (failure := parse_structured_tool_failure(result)) is not None
            and str(failure.get("error_code") or "").strip().casefold()
            in {"docs_access_internal", "docs_internal_failure"}
            for result in successful_results
        )
        if count == 1:
            if explicit_docs_root_request and not structured_internal_failure:
                _DOCS_ROOT_DELEGATION_COMPLETED.set(True)
            return successful_results[0]
        if explicit_docs_root_request and not structured_internal_failure:
            _DOCS_ROOT_DELEGATION_COMPLETED.set(True)
        return "\n\n".join(
            f"## {label}-{index + 1}\n{result}"
            for index, result in enumerate(successful_results)
        )

    async def agent_team_delegate(
        team: str = "",
        subagent: str = "",
        task: str = "",
        instances: int = 1,
        work_mode: str = "read",
        scopes: Optional[list[str]] = None,
    ) -> str:
        """Delegate bounded work and close a parent-owned QA browser lease.

        The implementation is wrapped so a cancelled, failed, or successful
        Agent Team run always revokes the opaque QA capability and closes the
        underlying browser/profile before returning to the parent turn.
        """

        try:
            return await _agent_team_delegate_impl(
                team=team,
                subagent=subagent,
                task=task,
                instances=instances,
                work_mode=work_mode,
                scopes=scopes,
            )
        finally:
            coordinator = qa_browser_coordinator
            close = getattr(coordinator, "close", None)
            if callable(close):
                try:
                    result = close("Agent Team QA run finished")
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    # Capability cleanup must never mask the worker result;
                    # the coordinator itself records/contains close failures.
                    import logging

                    logging.getLogger(__name__).warning(
                        "QA browser coordinator cleanup failed", exc_info=True
                    )

    agent_team_delegate.__doc__ = (
        _agent_team_delegate_impl.__doc__ or ""
    ) + f"\nAvailable Agent Teams and Subagents:\n{roster_text}"
    definition = tool_decorator(agent_team_delegate)
    description = f"{definition.description}\n\nAvailable Agent Teams and Subagents:\n{roster_text}"
    definition = replace(
        definition,
        description=description,
        parameters=[
            replace(param, enum=["read", "write"])
            if param.name == "work_mode"
            else param
            for param in definition.parameters
        ],
    )
    registry.register(
        replace(
            definition,
            owner="agent_team",
            risk="medium",
            side_effect="none",
            supports_parallel=False,
        )
    )


def _register_project_workspace_tools(
    registry: ToolRegistry,
    project_context: dict[str, Any] | None,
    *,
    client: Any = None,
) -> None:
    # Project-provided subprocess tools have no Enterprise ACL/quota
    # transaction boundary and are intentionally removed from the published
    # Enterprise surface.
    if Features.is_enterprise():
        return
    context = _model_visible_project_context(
        project_context or get_runtime_project_context(),
        client=client,
    ) or {}
    project_id = str(context.get("id") or "")
    if not project_id:
        return
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    enabled = bool(context.get("workspace_tools_enabled", metadata.get("workspace_tools_enabled", False)))
    if not enabled:
        return
    from ..services.workspace_tool_runner import load_workspace_tool_manifests, manifest_to_tool
    from ..services.project_workspace_cleanup import get_project_workspace_path
    workspace = get_project_workspace_path(UUID(project_id))
    for manifest in load_workspace_tool_manifests(workspace):
        registry.register(manifest_to_tool(manifest))


def build_runtime_tool_registry(
    config: Any,
    project_context: dict[str, Any] | None = None,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    client: Any = None,
    trusted_parent_context: TrustedParentRunContext | None = None,
    qa_browser_capability: Any = None,
    qa_browser_coordinator: Any = None,
) -> ToolRegistry:
    """Build the root runtime registry with direct tools and high-level delegates.

    レジストリはコアと deferred pack のツールを両方保持する。モデルへ実際に
    公開する実効集合は `tool_exposure.filter_tools_for_client()` が
    ロード済み pack にもとづいて絞り込む。`load_tool_pack` メタツールは
    セッション状態を持つクライアント側で `ensure_load_tool_pack_tool()` により登録する。

    ``workspace_root`` は App ツールが使う workspace root。``None`` なら
    ``AOITALK_WORKSPACES_DIR`` 由来の既定 root をここで 1 度だけ解決して渡す。
    解決済みの絶対 path を渡すことで、Tool 経由の App 操作が API 側と別 root の
    ロックを取る（= 排他が壊れる）ことを防ぐ。
    """
    registry = ToolRegistry()
    # Keep the server-resolved object for authorization/contextual gates even
    # when ``include_project_context`` hides it from the model prompt.
    server_project_context = (
        project_context
        if isinstance(project_context, dict)
        else get_runtime_project_context()
    )
    if qa_browser_capability is not None:
        # Parent integrations may pass the capability separately so a fixed
        # provider registry can be rebuilt without storing raw browser state
        # on the LLM client.  Validate and copy only the opaque facade.
        server_project_context = _inject_qa_browser_capability(
            server_project_context,
            qa_browser_capability,
        )
    elif qa_browser_coordinator is not None:
        capability = getattr(qa_browser_coordinator, "capability", None)
        server_project_context = _inject_qa_browser_capability(
            server_project_context,
            capability,
        )
    model_project_context = _model_visible_project_context(
        project_context,
        client=client,
    )

    if config and not config.get("use_tools", True):
        return registry

    if config and config.get("skills", {}).get("enabled", True) and invoke_skill is not None:
        registry.register(invoke_skill)

    # Keep deferred App definitions in the provider registry (as Spotify and
    # media packs do), but let the contextual pack gate below decide whether
    # they reach a model payload.  This is required for providers that retain
    # one fixed registry across turns: App context can become active later
    # without rebuilding the provider object.
    if _apps_enabled(config, True):
        from ..services.app_storage import get_workspaces_root

        app_workspace_root = str(get_workspaces_root(workspace_root))
        for app_tool in build_app_tool_definitions(
            server_project_context,
            workspace_root=app_workspace_root,
            deployment_config=config if isinstance(config, dict) else getattr(config, "config", None),
        ):
            registry.register(app_tool)

    if not config:
        _register_project_workspace_tools(
            registry,
            model_project_context,
            client=client,
        )
        return registry

    # Agent Team（汎用作業系）の公開スイッチ。OFF時はSubagent委譲と
    # 高度推論を隠す。単独で動く専門エージェントは個別設定に従う。
    delegation_on = agent_team_v3_delegation_enabled(config)

    if delegation_on:
        _register_load_agent_team_tool(registry, config=config, client=client)
        _register_agent_team_delegate_tool(
            registry,
            config=config,
            client=client,
            project_context=server_project_context,
            trusted_parent_context=trusted_parent_context,
            qa_browser_coordinator=qa_browser_coordinator,
        )

    # Utility lookups are Shared Tools and do not depend on Agent Team
    # delegation being enabled.
    _register_utility_direct_tools(registry)
    _register_planning_tools(registry)

    search_enabled = _agent_enabled(config, "search", True)
    if search_enabled:
        _register_search_direct_tools(registry, config=config)
    _register_session_tools(
        registry,
        config=config,
        search_enabled=search_enabled,
    )
    _register_scoped_memory_tools(registry)

    # Spotify is a disabled-by-default Shared Integration.  When enabled,
    # expose its direct high-level tools lazily through the ``spotify`` pack;
    # never create an extra Spotify LLM hop.
    if _spotify_integration_enabled(config):
        _register_spotify_direct_tools(registry)

    if _agent_enabled(config, "filesystem", True):
        _register_filesystem_direct_tools(registry)
        _register_bm25_direct_tools(
            registry,
            # Keep the full server-bound context for authorization even when
            # the model-facing Project Context is intentionally hidden.
            project_context=project_context,
            workspace_root=workspace_root,
        )

    if _agent_enabled(config, "media", True):
        _register_delegation_tool(
            registry,
            tool_name="media_assistant",
            description="Delegate image and streaming media work to the media specialist agent.",
            runner=MediaDelegationRunner(config),
            config=config,
            client=client,
            owner="media",
        )

    if _agent_enabled(config, "project_management", True):
        _register_project_management_direct_tools(
            registry,
            config=config,
        )

    if _agent_enabled(config, "docs", True):
        _register_docs_direct_tools(registry)

    # Block duplicate low-level entrypoints so direct root tools stay the only
    # entrypoint for search, filesystem, and project-management work.
    try:
        from ..services.character_service import build_character_agent_tools

        blocked_duplicate_tool_names = {
            "filesystem_assistant",
            "project_management_assistant",
            "search_assistant",
        }
        # The Character service already applies the assistant-only public
        # boundary.  Repeat the boundary at registration so a future/custom
        # builder cannot accidentally publish Roleplay/TRPG/GM bridges to the
        # ordinary Main runtime.  ``allowed_tools`` is intentionally not read
        # here: it belongs to the Character's internal execution policy.
        for tool_def in build_character_agent_tools(config):
            availability = getattr(tool_def, "availability", None)
            # Fail closed when a custom/legacy builder does not attach the
            # service's character_type marker.  A missing marker cannot prove
            # that the ToolDefinition belongs to an assistant Character, so it
            # must not cross the ordinary Main runtime boundary.
            if not isinstance(availability, dict):
                continue
            character_type = str(availability.get("character_type") or "").strip().lower()
            if character_type != "assistant":
                continue
            if tool_def.name in blocked_duplicate_tool_names:
                continue
            # Persisted Character allowed_tools values are migrated to the
            # logical ``spotify`` integration group.  The old assistant
            # bridge must never reintroduce a hidden Spotify LLM path.
            if tool_def.name == "spotify_assistant":
                continue
            if tool_def.name not in registry:
                # `characters` pack から識別できるよう owner を明示する。
                registry.register(replace(tool_def, owner=CHARACTER_TOOL_OWNER))
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "キャラクターエージェントツールの登録に失敗しました", exc_info=True
        )

    _register_project_workspace_tools(
        registry,
        model_project_context,
        client=client,
    )
    return registry


def build_runtime_tool_registry_for_client(
    builder: Callable[..., ToolRegistry],
    config: Any,
    *,
    client: Any,
) -> ToolRegistry:
    """Invoke a provider's registry builder with its client when supported.

    Provider constructors are occasionally wrapped by integrations/tests with
    a legacy ``builder(config)`` callable.  Keep that compatibility while
    ensuring the real registry builder receives ``client=self`` so direct
    provider calls can honor ``current_include_project_context``.
    """
    try:
        parameters = inspect.signature(builder).parameters.values()
        accepts_client = any(
            parameter.name == "client"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        # C-extension/callable instances may not expose a signature.  Prefer
        # the modern keyword and let a genuine invocation error propagate.
        accepts_client = True
    if accepts_client:
        return builder(config, client=client)
    return builder(config)


# Parent/controller integrations import the lifecycle facade from the runtime
# module alongside ``build_runtime_tool_registry``.  The service itself only
# imports security primitives, so this alias does not create an LLM/runtime
# import cycle; its registry-building method performs a lazy import back here.
from ..services.qa_browser_coordinator import (  # noqa: E402  (late public API)
    QABrowserCoordinator,
    create_qa_browser_coordinator,
    create_qa_browser_runtime,
    open_qa_browser_coordinator,
    start_qa_browser_coordinator,
)
