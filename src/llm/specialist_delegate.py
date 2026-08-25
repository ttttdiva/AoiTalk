"""Shared delegation runners for specialist agents."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import json
import logging
import os
import re
import time
from contextlib import nullcontext
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Sequence, Type

from openai import OpenAI

from ..config import Config
from ..features import Features
from .sglang_url import resolve_sglang_base_url
from ..services.project_context import (
    format_project_context_for_prompt,
    get_runtime_project_context,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.turn_context import get_turn_context
from ..services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    get_privacy_policy_context,
)
from ..services.agent_team_service import (
    ToolFailureCircuitBreaker,
    parse_structured_tool_failure,
)
from ..services.agent_team_v3 import (
    agent_team_v3_delegation_enabled,
    agent_team_v3_enabled,
    agent_team_workspace_access,
    agent_team_subagent_allows_write,
    agent_team_v3_teams,
    filter_agent_team_capabilities,
    agent_team_v3_subagents,
    resolve_agent_team_v3_route,
    subagent_requires_external_approval,
)
from ..tools.adapters import CLIAdapter, OpenAIAPIAdapter
from ..tools.apps import build_app_tool_definitions
from ..tools.core import ToolDefinition, ToolParam, ensure_tool_definitions
from ..tools.external import MCPPlugin, set_mcp_plugin
from ..tools.external_llm_permission import request_external_model_prompt
from ..tools.registry import ToolRegistry
from .agent_runtime import (
    OpenAIToolCallLoopResult,
    OpenAIToolCallRecord,
    run_openai_tool_call_loop,
)
from .context_budget import (
    clip_text_preserve_tail,
    resolve_context_budget,
)
from .conversation_context import normalize_usage, persist_usage_sync
from .json_tool_loop import (
    JsonToolCallRecord,
    JsonToolLoopResult,
    build_json_tool_loop_system_prompt,
    run_json_tool_loop,
)
from .generation_policy import PermissionPolicy, get_current_generation_policy
from .native_runtime import (
    AgentDefinition as NativeAgentDefinition,
    NativeModelSettings,
    Reasoning,
    run_native_agent_once,
)
from .provider_mode_adapters import ollama_reasoning_effort_for_mode
from .openai_compatible_local_profiles import (
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_reasoning_effort_request_extra_body,
)
from .openrouter_provider_routing import merge_provider_options_into_extra_body
from .unified_turn_runtime import run_cli_tool_call_loop
from .cli_backends.base import CLIBackendBase
from .generation_cancellation import GenerationInterrupted
from .worker_report import (
    WORKER_REPORT_SCHEMA_VERSION,
    normalize_worker_report,
    parent_publication_metadata,
)
from .tool_policy import (
    DOCS_MUTATION_TOOL_NAMES,
    DOCS_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    reset_current_agent_team_role,
    set_current_agent_team_role,
)

logger = logging.getLogger(__name__)

_EXTERNAL_APPROVAL_PROVIDERS = {
    "openai",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "gemini",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}


def _requires_external_approval(subagent: dict[str, Any] | None) -> bool:
    if not isinstance(subagent, dict):
        return False
    return str(subagent.get("provider") or "").strip().lower() in _EXTERNAL_APPROVAL_PROVIDERS


class _SpecialistUsageClient:
    """Context-only client shape consumed by :func:`persist_usage_sync`.

    Specialist runners call provider SDKs directly rather than through one of
    the long-lived LLM clients.  Keeping the identity fields on this small
    adapter lets the shared persistence helper apply the same UUID/user
    normalization as the regular client paths without mutating a shared
    runner (which may be used concurrently).
    """

    def __init__(
        self,
        *,
        user_id: str | None,
        session_id: str | None,
        project_id: str | None,
        agent_name: str | None,
    ) -> None:
        self._user_id = user_id
        self.current_session_id = session_id
        self.current_project_id = project_id
        self.character_name = agent_name

    def _get_session_user_id(self) -> str:
        return self._user_id or "default_user"


def _response_usage(response: Any) -> Any:
    """Read a provider response's usage field without assuming SDK classes."""

    if isinstance(response, dict):
        return response.get("usage")
    return getattr(response, "usage", None)


def _response_model(response: Any) -> str | None:
    if isinstance(response, dict):
        value = response.get("model") or response.get("model_version")
    else:
        value = getattr(response, "model", None) or getattr(
            response, "model_version", None
        )
    value = str(value or "").strip()
    return value or None

_runtime_specialist_provider: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("runtime_specialist_provider", default=None)
)


def set_runtime_specialist_provider(provider: Optional[str]) -> contextvars.Token:
    value = str(provider or "").strip().lower() or None
    return _runtime_specialist_provider.set(value)


def reset_runtime_specialist_provider(token: contextvars.Token) -> None:
    _runtime_specialist_provider.reset(token)

CLI_PROVIDER_NAMES = {"antigravity-cli", "claude-cli", "codex-cli", "grok-cli"}
# A Team worker is a leaf in the v3 delegation graph.  Keep this deny-list at
# the ToolDefinition boundary as a defence-in-depth measure: capability
# routing still decides which normal tools are available, while these names
# can never be used to open the Director browser, attach an arbitrary MCP
# server, spawn another worker, or publish Git state from a child.
_CHILD_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "agent_team_delegate",
        "use_mcp_tool",
        "git_commit",
        "git_push",
        "git_force_push",
        "git_reset_hard",
        "git_clean",
        "commit_changes",
        "push_changes",
        "publish_changes",
    }
)
_CHILD_FORBIDDEN_TOOL_MARKERS = (
    "director",
    "chatgpt_web",
    "playwright",
    "browser",
    "browser_mcp",
)
_CHILD_FORBIDDEN_OWNER_MARKERS = (
    "director",
    "browser",
    "chatgpt",
    "mcp",
)
_QA_BROWSER_TOOL_NAMES = frozenset(
    {
        "qa_navigate",
        "qa_action",
        "qa_snapshot",
        "qa_wait",
        "qa_upload",
        "qa_download",
    }
)
_WORKER_PUBLICATION_INSTRUCTIONS = (
    "This is a leaf Agent Team worker. Do not spawn or delegate another worker. "
    "Do not open ChatGPT/Director or any unassigned browser transport, and do not use MCP tools. "
    "Do not commit, push, reset, clean, or publish Git state; return a concise "
    "worker report to the parent coordinator instead. Include task, findings, "
    "evidence, changed_scope, verification, unresolved, decision, and relevant "
    "file/symbol references; legacy plain text remains accepted."
)


def _child_tool_allowed(
    tool_def: ToolDefinition,
    *,
    allow_qa_browser: bool = False,
) -> bool:
    """Return whether a ToolDefinition may cross the worker boundary."""

    name = str(getattr(tool_def, "name", "") or "").strip().lower()
    owner = str(getattr(tool_def, "owner", "") or "").strip().lower()
    description = str(getattr(tool_def, "description", "") or "").strip().lower()
    if not name:
        return False
    if name in _QA_BROWSER_TOOL_NAMES and not allow_qa_browser:
        return False
    if name in _CHILD_FORBIDDEN_TOOL_NAMES or name.startswith("mcp_"):
        return False
    is_qa_tool = allow_qa_browser and name in _QA_BROWSER_TOOL_NAMES and owner == "qa_lane"
    if not is_qa_tool and any(marker in name for marker in _CHILD_FORBIDDEN_TOOL_MARKERS):
        return False
    if not is_qa_tool and any(marker in owner for marker in _CHILD_FORBIDDEN_OWNER_MARKERS):
        return False
    if not is_qa_tool and any(marker in description for marker in _CHILD_FORBIDDEN_TOOL_MARKERS):
        return False
    # Publication tools added by integrations must remain parent-owned even if
    # their exact name is not in the compatibility list above.
    if (
        (name.startswith("git_") or name.startswith("git-"))
        and any(action in name for action in ("commit", "push", "reset", "clean", "publish"))
    ):
        return False
    if getattr(tool_def, "requires_approval", False) and any(
        marker in name for marker in ("publish", "force_push", "reset_hard")
    ):
        return False
    return True


def _qa_browser_tools_from_context(
    project_context: Mapping[str, Any] | None,
    *,
    role: str,
) -> list[ToolDefinition]:
    """Build a small QA-only facade from a parent-issued capability.

    The raw Playwright page/driver never enters this function.  The parent
    controller places an opaque ``QABrowserCapability`` in the trusted runtime
    context; only explicitly allowlisted QA roles can receive these tools.
    """

    normalized_role = str(role or "").strip().casefold().replace(" ", "-")
    try:
        from ..security.qa_browser_transport import QA_BROWSER_WORKER_ROLES
    except Exception:
        return []
    if normalized_role not in {
        str(item).strip().casefold().replace(" ", "-")
        for item in QA_BROWSER_WORKER_ROLES
    }:
        return []
    context = project_context if isinstance(project_context, Mapping) else {}
    capability = context.get("qa_browser_capability") or context.get(
        "_qa_browser_capability"
    )
    if capability is None or not all(
        callable(getattr(capability, name, None))
        for name in ("navigate", "action", "upload", "download")
    ):
        return []

    async def qa_navigate(url: str) -> Any:
        """Open one URL already allowlisted by the parent QA scope."""

        return await capability.navigate(url)

    async def qa_action(
        action: str,
        selector: str = "",
        value: str = "",
        timeout_ms: int = 10_000,
    ) -> Any:
        """Run one bounded action through the parent-issued QA facade."""

        return await capability.action(
            action,
            selector=selector or None,
            value=value or None,
            timeout_ms=max(1, min(int(timeout_ms), 120_000)),
        )

    async def qa_snapshot() -> Any:
        """Return a bounded page snapshot from the QA facade."""

        return await capability.action("snapshot")

    async def qa_wait(seconds: float = 0.25) -> Any:
        """Wait briefly without holding one unbounded tool call."""

        return await capability.action(
            "wait",
            value=max(0.0, min(float(seconds), 30.0)),
        )

    async def qa_upload(path: str, selector: str = "") -> Any:
        """Upload one file after the parent run scope approves its path."""

        return await capability.upload(path, locator=selector or None)

    async def qa_download(path: str, selector: str = "") -> Any:
        """Save one download into the parent-approved run directory."""

        return await capability.download(path, trigger=selector or None)

    return [
        ToolDefinition(
            name="qa_navigate",
            description="Open one allowlisted QA target URL.",
            function=qa_navigate,
            parameters=[ToolParam("url", "string", "Allowlisted target URL.")],
            is_async=True,
            owner="qa_lane",
            supports_parallel=False,
        ),
        ToolDefinition(
            name="qa_action",
            description="Run one bounded QA action such as click, fill, type, or press.",
            function=qa_action,
            parameters=[
                ToolParam("action", "string", "Action name.", enum=["click", "fill", "type", "press"]),
                ToolParam("selector", "string", "Optional target selector.", required=False, default=""),
                ToolParam("value", "string", "Optional value or key.", required=False, default=""),
                ToolParam("timeout_ms", "integer", "Bounded action timeout.", required=False, default=10_000),
            ],
            is_async=True,
            owner="qa_lane",
            supports_parallel=False,
        ),
        ToolDefinition(
            name="qa_snapshot",
            description="Read the current QA page snapshot.",
            function=qa_snapshot,
            parameters=[],
            is_async=True,
            owner="qa_lane",
            supports_parallel=False,
        ),
        ToolDefinition(
            name="qa_wait",
            description="Wait for a short bounded QA interval.",
            function=qa_wait,
            parameters=[ToolParam("seconds", "number", "Wait duration, capped at 30 seconds.", required=False, default=0.25)],
            is_async=True,
            owner="qa_lane",
            supports_parallel=False,
        ),
        ToolDefinition(
            name="qa_upload",
            description="Upload one run-scoped file through the QA lane.",
            function=qa_upload,
            parameters=[
                ToolParam("path", "string", "Run-scoped file path."),
                ToolParam("selector", "string", "Optional file input selector.", required=False, default=""),
            ],
            is_async=True,
            owner="qa_lane",
            side_effect="mutation",
            risk="medium",
            requires_approval=True,
            supports_parallel=False,
        ),
        ToolDefinition(
            name="qa_download",
            description="Save one download through the run-scoped QA lane.",
            function=qa_download,
            parameters=[
                ToolParam("path", "string", "Run-scoped destination path."),
                ToolParam("selector", "string", "Optional download trigger selector.", required=False, default=""),
            ],
            is_async=True,
            owner="qa_lane",
            side_effect="mutation",
            risk="medium",
            requires_approval=True,
            supports_parallel=False,
        ),
    ]
_PRIVACY_SCOPE_UNSET = object()
NATIVE_OPENAI_MODEL_PREFIXES = ("openai/", "litellm/", "gpt-", "o1", "o3", "o4")
OLLAMA_PROVIDER_NAME = "ollama"
OPENAI_COMPATIBLE_PROVIDER_NAMES = {
    "openai_compatible_local",
    "sglang",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
}
CONSTRAINED_OPENAI_COMPATIBLE_CONTEXT_TOKENS = 16384

TEAM_SUBAGENT_INSTRUCTIONS = {
    "docs_operator": """
You are the AoiTalk Docs operation specialist.

Use only the exposed high-level AoiTalk Docs tools; never access the database,
SQL, or a native shell. Resolve the requested Project and canonical node
identity before any write. If a title/path reference has multiple visible
candidates or the parent is not uniquely identified, do not guess and do not
mutate either candidate: return a concise clarification request or a
structured ambiguous-target result to Main. Keep source nodes unchanged when
the assignment asks for a derived summary, and verify the target parent and
project immediately before creating or updating a node.
""".strip(),
    "architecture_planner": """
You are the Agent Team architecture-planning Subagent.

Analyze the existing context and produce a concrete implementation blueprint.
Make decisions, assign ownership boundaries, identify affected files/modules,
and call out risks that implementers and reviewers must handle.
Do not write marketing copy. Be specific and operational.
""".strip(),
    "code_explorer": """
You are an Agent Team code-exploration Subagent.

Investigate the requested topic with the available read-only tools. Trace entry
points, data flow, configuration, tests, and nearby conventions. Return concise
findings with file references and the minimum context needed for implementation.
Avoid duplicating other explorers' work when the request gives you a narrower
question.
""".strip(),
    "code_implementer": """
You are an Agent Team implementation Subagent.

Turn the assigned implementation scope into concrete steps and code-level
guidance. Respect file ownership from the coordinator, avoid unrelated
refactors, and explicitly note conflicts or missing prerequisites. When no
write-capable runner is attached, return an implementation patch plan with exact
files and functions rather than pretending to edit files.
""".strip(),
    "code_reviewer": """
You are an Agent Team code-review Subagent.

Review the assigned scope for correctness, regressions, missing tests, security
or privacy risks, and maintainability. Report only issues that are actionable and
grounded in evidence. Start with findings, ordered by severity, with file
references whenever available.
""".strip(),
    "general_worker": """
You are a general-purpose AoiTalk Subagent. Complete the bounded assignment
without assuming a fixed domain, and use only the capabilities provided for
this run.
""".strip(),
    "general_researcher": """
You are a read-only research Subagent. Cross-check available sources and
return concise evidence without mutating workspace or application data.
""".strip(),
    "story_writer": """
You are a Story writing Subagent. Preserve Story context and character voice,
and use only the Story high-level tools exposed for this run.
""".strip(),
    "story_consistency_reviewer": """
You are a read-only Story consistency-review Subagent. Check world setting,
timeline, character facts, scenes, and terminology without writing changes.
""".strip(),
    "character_voice_reviewer": """
You are a read-only character-voice review Subagent. Check personality,
speech style, and prior statements without writing changes.
""".strip(),
    "story_import": """
You are a Story import Subagent. Analyze and normalize supplied Story material
through the exposed high-level import tools, preserving unrelated data.
""".strip(),
}

_COMPACT_TOOL_DESCRIPTIONS = {
    "search_files": "Find files or folders by name, or grep file contents.",
    "bm25_search": "Rank relevant chunks from authorized Project Files/App source; then read or search the cited paths.",
    "list_directory": "List folder contents, flat or bounded recursive.",
    "read_file": "Read one file by workspace-relative or absolute path.",
    "get_workspace_file_info": "Get workspace file metadata.",
    "get_project_context": "Get the currently selected Project context, or a specified Project.",
    "list_project_information": "List saved project information Docs, Q&A, and tables.",
    "list_record_tables": "List project record tables.",
    "list_tasks": "List project tasks.",
    "create_task": "Create a project task.",
    "update_task": "Update a project task.",
    "delete_task": "Delete a project task.",
    "schedule_task": "Schedule a project task.",
    "get_project_progress": "Summarize project progress from goals, internal WBS, and tasks.",
    "get_upcoming_wbs_tasks": "List upcoming internal WBS.dbtable rows.",
    "get_project_issues": "List project issues.",
}


def _is_context_overflow_error(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    text = f"{exc} {body}".casefold()
    return any(
        term in text
        for term in (
            "context size",
            "context length",
            "context window",
            "maximum context",
            "max context",
            "n_ctx",
            "too many tokens",
            "prompt is too long",
            "requested tokens",
            "exceeds context",
            "exceeded context",
            "exceeds the context",
            "exceeded the context",
            "exceeds maximum context",
            "コンテキスト",
        )
    )


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    try:
        value = getter(key, default)
    except TypeError:
        value = default
    if value is not default or "." not in key:
        return value
    current: Any = config
    for part in key.split("."):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        else:
            nested_getter = getattr(current, "get", None)
            if not callable(nested_getter):
                return default
            current = nested_getter(part, default)
            if current is default:
                return default
    return current


def _normalized_model(value: Any) -> Optional[str]:
    """Return a non-blank model name, or ``None`` for inherit/fallback.

    Character rows and provider settings are user-editable strings.  Treat
    whitespace-only values exactly like an unset model before constructing a
    provider request; sending ``"   "`` to an SDK is never an intentional
    model selection.
    """

    normalized = str(value or "").strip()
    return normalized or None


def _compact_tool_definition(tool_def: ToolDefinition) -> ToolDefinition:
    return ToolDefinition(
        name=tool_def.name,
        description=_COMPACT_TOOL_DESCRIPTIONS.get(tool_def.name, tool_def.name),
        function=tool_def.function,
        parameters=[
            ToolParam(
                name=param.name,
                type=param.type,
                description="",
                required=param.required,
                default=param.default,
                enum=param.enum,
            )
            for param in tool_def.parameters
        ],
        is_async=tool_def.is_async,
    )


def _clone_registry_with_tools(
    registry: ToolRegistry,
    tool_names: Sequence[str],
    *,
    compact: bool = False,
) -> ToolRegistry:
    selected = ToolRegistry()
    seen: set[str] = set()
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        tool_def = registry.get(name)
        if tool_def is None:
            continue
        selected.register(_compact_tool_definition(tool_def) if compact else tool_def)
    return selected


def _is_constrained_openai_compatible_context(
    provider: str,
    context_window_tokens: int,
) -> bool:
    return (
        provider in {"openai_compatible_local", "sglang"}
        and context_window_tokens <= CONSTRAINED_OPENAI_COMPATIBLE_CONTEXT_TOKENS
    )


class SpecialistDelegationRunner:
    """Run a specialist agent from synchronous tool execution."""

    def __init__(
        self,
        config: Config,
        *,
        domain_key: str,
        display_name: str,
        agent_class: Type,
        mcp_server_names: Sequence[str] | None = None,
        model: Optional[str] = None,
        agent_team_profile_id: Optional[str] = None,
        agent_team_team_id: Optional[str] = None,
        tool_required: Optional[bool] = None,
        capabilities: Sequence[str] | None = None,
        work_mode: str = "read",
    ):
        self.config = config
        self.domain_key = domain_key
        self.display_name = display_name
        self.agent_class = agent_class
        self.mcp_server_names = tuple(mcp_server_names or ())
        self._agent_team_profile_id = str(agent_team_profile_id or "").strip() or None
        self._agent_team_team_id = str(agent_team_team_id or "").strip() or None
        self._turn_tool_required = tool_required
        self._agent_team_work_mode = (
            str(work_mode or "read").strip().lower()
            if str(work_mode or "read").strip().lower() in {"read", "write"}
            else "read"
        )
        self._agent_team_capabilities = tuple(
            dict.fromkeys(str(value).strip() for value in (capabilities or ()) if str(value).strip())
        )
        self._agent_team_subagent = None
        self._agent_team_subagent_route = None
        self._agent_team_subagent_target = False
        if agent_team_v3_enabled(config):
            self._agent_team_subagent = next(
                (
                    item
                    for item in agent_team_v3_subagents(config)
                    if str(item.get("subagent_id") or "") == str(domain_key)
                ),
                None,
            )
            if self._agent_team_subagent is not None:
                self._agent_team_subagent_route = resolve_agent_team_v3_route(
                    config,
                    str(domain_key),
                ) or {}
                # A Subagent is an explicit target even when its LLM Profile
                # inherits the Main route.
                self._agent_team_subagent_target = True
        else:
            # Config migration is an application-boundary concern.  Normal
            # runtime never resolves legacy topology data.
            self._agent_team_subagent_route = None
            self._agent_team_subagent_target = False
        self._route_intent = None
        if (
            isinstance(self._agent_team_subagent_route, dict)
            and (
                self._agent_team_subagent_route.get("kind") == "pool"
                or self._agent_team_subagent_route.get("target_type") == "pool"
            )
        ):
            from ..services.free_team_service import (
                free_team_llm_profile,
                pool_route_intent,
            )

            # Free Team overlays only the target of a canonical LLM Profile;
            # Team/Subagent topology remains in App Config.  Resolve that
            # overlay before constructing the pool intent so provider/model
            # metadata and quota routing stay canonical.
            profile_id = str(
                self._agent_team_subagent_route.get("llm_profile_id")
                or self._agent_team_subagent_route.get("profile_id")
                or self._agent_team_profile_id
                or ""
            ).strip()
            if profile_id:
                overlay = free_team_llm_profile(self.config, profile_id)
                if isinstance(overlay, dict):
                    self._agent_team_subagent_route = {
                        **self._agent_team_subagent_route,
                        **overlay,
                        "profile_id": profile_id,
                        "llm_profile_id": profile_id,
                    }

            self._route_intent = pool_route_intent(
                self.config,
                subagent_id=domain_key,
                team_id=str(self._agent_team_team_id or ""),
                llm_profile_id=profile_id,
                profile=self._agent_team_subagent_route,
            )
        self.route_metadata: dict[str, Any] = {
            "team_id": self._agent_team_team_id or None,
            "subagent_id": self.domain_key,
            "llm_profile_id": None,
            "execution_profile_id": str(
                (self._agent_team_subagent_route or {}).get("execution_profile_id")
                or ""
            ) or None,
        }

        self.provider = self._select_provider()
        # Character DB values may be ``None``, empty, or whitespace-only.
        # Normalize before deciding whether to inherit the effective Main
        # route, while preserving any explicit non-blank model verbatim.
        self.model = _normalized_model(model) or _normalized_model(
            self._select_model()
        )
        self._deployment = None
        self._apply_deployment_contract()
        if self._uses_agent_team_subagent_target():
            route_source = str(
                (self._agent_team_subagent_route or {}).get("route_source")
                or ""
            ).strip()
            if not route_source:
                target_type = str(
                    (self._agent_team_subagent_route or {}).get("target_type")
                    or "inherit"
                ).strip().lower()
                route_source = {
                    "inherit": "main_inherit",
                    "static": "static_profile",
                    "pool": "pool_profile",
                }.get(target_type, "main_inherit")
        elif str(model or "").strip() and str(self.domain_key).startswith("character_"):
            route_source = "explicit_character_model"
        else:
            route_source = "main_inherit"
        # ``route_metadata`` is copied into AgentRun instance events by the
        # runtime registry.  Keep provider/model/source together so historical
        # rows can be audited without a schema migration.
        self.route_metadata.update(
            {
                "provider": str(self.provider or "").strip().lower() or None,
                "model": str(self.model or "").strip() or None,
                "route_source": route_source,
            }
        )
        if self._uses_agent_team_subagent_target():
            route = self._agent_team_subagent_route or {}
            effective_effort = str(
                route.get("effort") or route.get("reasoning_effort") or ""
            ).strip() or None
            self.route_metadata["reasoning_effort"] = effective_effort
            requested_effort = str(route.get("requested_reasoning_effort") or "").strip()
            if requested_effort:
                self.route_metadata["requested_reasoning_effort"] = requested_effort
            effort_policy = str(route.get("effort_policy") or "").strip()
            if effort_policy:
                self.route_metadata["effort_policy"] = effort_policy
        self._mode_preset = ""
        if self._uses_agent_team_subagent_target():
            route = self._agent_team_subagent_route or {}
            route_mode = str(
                route.get("effort") or route.get("reasoning_effort") or ""
            ).strip()
            if not route_mode and self.provider == "openai_compatible_local":
                metadata = llama_cpp_reasoning_effort_metadata(self.model)
                if metadata:
                    # An unsupported explicit value is retained so the
                    # profile-aware request projection raises instead of
                    # silently replacing it with the managed default.
                    route_mode = str(
                        route.get("requested_reasoning_effort") or ""
                    ).strip()
                    if not route_mode:
                        route_mode = metadata["default"]
            self._mode_preset = route_mode
        elif self.provider == "openai":
            # Specialist calls that are not an explicit Agent Team route still
            # inherit the main OpenAI effort setting.  Without this, a direct
            # specialist fallback could use the configured model while silently
            # dropping ``openai.reasoning_effort`` before the native request.
            self._mode_preset = self._main_openai_effort()
        elif self.provider == "openai_compatible_local":
            metadata = llama_cpp_reasoning_effort_metadata(self.model)
            if metadata:
                configured = str(
                    _config_get(
                        self.config,
                        "openai_compatible_local.llama_cpp.reasoning_effort",
                    )
                    or ""
                ).strip().lower()
                self._mode_preset = (
                    configured if configured in metadata["options"] else metadata["default"]
                )
        self.cli_backend = (
            self._create_cli_backend() if self.provider in CLI_PROVIDER_NAMES else None
        )

        self._agent_definition = None
        self._tool_registry: ToolRegistry | None = None
        # Direct Agent Team provider paths use the same session-scoped gateway
        # as native runtime calls.  Recreate it when the turn identity changes
        # so aliases can never leak across users/sessions.
        self._privacy_gateway: OutboundPrivacyGateway | None = None
        self._privacy_gateway_key: tuple[str, str] | None = None

    def _privacy_gateway_for_turn(
        self,
        *,
        session_context: Optional[dict[str, Any]] | object = _PRIVACY_SCOPE_UNSET,
        project_metadata: Optional[dict[str, Any]] | object = _PRIVACY_SCOPE_UNSET,
    ) -> OutboundPrivacyGateway:
        turn = get_turn_context()
        inherited = get_privacy_policy_context()
        # Omitted scopes inherit the current request ContextVar.  Passing an
        # explicit ``None`` remains an intentional clear, which prevents a
        # prior project's policy from leaking into a turn without metadata.
        resolved_session_context = (
            inherited.session_context
            if session_context is _PRIVACY_SCOPE_UNSET
            else session_context
        )
        resolved_project_metadata = (
            inherited.project_metadata
            if project_metadata is _PRIVACY_SCOPE_UNSET
            else project_metadata
        )

        def _scope(value: object) -> dict[str, Any]:
            # ``OutboundPrivacyGateway(None)`` means "inherit the ContextVar",
            # so pass an explicit empty mapping for an omitted/cleared scope.
            # This preserves the distinction between omitted (snapshot the
            # current inherited map above) and explicit ``None`` (clear it).
            return dict(value) if isinstance(value, Mapping) else {}

        user_id = str(
            getattr(self, "session_user_id", None)
            or turn.user_id
            or _config_get(self.config, "user_id", None)
            or "default_user"
        ).strip()
        session_id = str(
            getattr(self, "current_session_id", None)
            or turn.session_id
            or _config_get(self.config, "session_id", None)
            or ""
        ).strip()
        key = (user_id, session_id)
        if self._privacy_gateway is None or self._privacy_gateway_key != key:
            self._privacy_gateway = OutboundPrivacyGateway(
                self.config,
                user_id=user_id,
                session_id=session_id,
                session_context=_scope(resolved_session_context),
                project_metadata=_scope(resolved_project_metadata),
            )
            self._privacy_gateway_key = key
        else:
            self._privacy_gateway.update_policy_context(
                session_context=_scope(resolved_session_context),
                project_metadata=_scope(resolved_project_metadata),
            )
        return self._privacy_gateway

    def _apply_deployment_contract(self) -> None:
        """Apply the Enterprise provider boundary before any direct SDK call.

        Specialist runners have a few historical direct-provider paths (the
        OpenAI-compatible and Ollama JSON loops) that do not go through
        ``create_llm_client``.  Keep persisted personal settings untouched,
        but project the effective deployment onto this runner so a stale
        persisted SGLang provider cannot open an SGLang endpoint under a fixed
        Gemma/vLLM release.  Explicit Agent Team targets are preflighted and
        fail closed instead of being silently rewritten; pool routes perform
        the same check for each leased candidate in ``_run_pool_route``.
        """

        from .deployment_resolver import (
            effective_config_overrides,
            preflight_deployment,
            resolve_llm_deployment,
        )

        deployment = resolve_llm_deployment(self.config)
        self._deployment = deployment
        if deployment is None:
            return

        # A Subagent explicitly selected in Agent Team is an engine change,
        # not a stale persisted main setting.  Reject it before chat/network.
        explicit_target = (
            self._route_intent is None and self._uses_agent_team_subagent_target()
        )
        if explicit_target:
            preflight_deployment(
                self.config,
                provider=self.provider,
                model=self.model,
            )
        else:
            available, _ = deployment.provider_available(self.provider)
            if deployment.fixed or not available:
                self.provider = deployment.effective_provider
                self.model = deployment.effective_model

        overrides = effective_config_overrides(self.config)
        if overrides:
            # Import lazily: manager_parts imports this module while the main
            # factory is loading, so a module-level import would create a cycle.
            from .manager import TargetConfig

            self.config = TargetConfig(self.config, overrides)

    async def _run_pool_route(self, request: str) -> str:
        """Agent Teamの1インスタンスに1つの候補を固定して実行する。"""

        from .free_team_client import (
            _call_target_method,
            _error_class,
            _reservation_prompt,
            _target_supports_stream_callback,
            _usage_from_client,
        )
        from .manager import create_llm_client_for_target
        from ..services.free_team_service import (
            acquire_route_lease,
            finalize_route_lease,
            free_team_profile,
        )

        profile = free_team_profile(self.config)
        max_fallbacks = max(0, min(10, int(profile.get("max_fallbacks") or 0)))
        agent_definition = self._get_agent_definition()
        role_instructions = str(
            getattr(agent_definition, "instructions", "") or ""
        ).strip()
        tool_registry = self._build_tool_registry()
        # pool定義を明示的なturn intentとして扱う。heavyは渡された文脈を
        # 推論/レビューするtool-free pool、coding/tool-executor等は必須。
        tool_mode = str(self._route_intent.tool_mode or "auto").lower()
        tools_available = bool(tool_registry.get_names())
        tools_required = (
            bool(self._turn_tool_required)
            if isinstance(self._turn_tool_required, bool)
            else False
            if tool_mode == "disabled"
            else tools_available
        )
        lease_prompt = _reservation_prompt(
            request,
            system_prompt=role_instructions,
            session_metadata={
                "team_id": str(self._agent_team_team_id or ""),
                "subagent_id": self.domain_key,
                "project_id": str(self.current_project_id or ""),
            },
            tool_registry=tool_registry if tools_required else None,
        )
        excluded: set[str] = set()
        last_error: BaseException | None = None
        for fallback_count in range(max_fallbacks + 1):
            lease = await acquire_route_lease(
                self._route_intent,
                prompt=lease_prompt,
                required_capabilities=(
                    {"text", "tools"} if tools_required else {"text"}
                ),
                subagent_id=self.domain_key,
                team_id=str(self._agent_team_team_id or ""),
                excluded_candidate_ids=excluded,
                fallback_count=fallback_count,
            )
            excluded.add(lease.candidate_id)
            self.route_metadata = {
                **dict(lease.safe_metadata() or {}),
                "provider": str(lease.provider or "").strip().lower() or None,
                "model": str(lease.model or "").strip() or None,
                "route_source": "pool_profile",
            }
            client: Any = None
            side_effect_started = False
            side_effect_observable = False

            async def monitor(event_type: str, _data: dict[str, Any]) -> None:
                nonlocal side_effect_started
                event = str(event_type or "").lower()
                if "tool" in event and (
                    "start" in event or "call" in event or "execut" in event
                ):
                    side_effect_started = True

            started = time.perf_counter()
            try:
                if str(lease.provider or "").strip().lower() in CLI_PROVIDER_NAMES:
                    raise RuntimeError(
                        "Agent Team scoped delegation does not support CLI pool "
                        "providers because their native sandbox can bypass the "
                        "declared AoiTalk read-only capabilities."
                    )
                approved_request = await self._approve_pool_route_request(
                    request,
                    provider=lease.provider,
                    model=lease.model,
                )
                if approved_request is None:
                    await finalize_route_lease(
                        lease,
                        success=False,
                        error_class="cancelled",
                    )
                    return f"{self.display_name} delegation cancelled"
                execution_request = approved_request
                client = create_llm_client_for_target(
                    self.config,
                    provider=lease.provider,
                    model=lease.model,
                    effort=lease.effort,
                    base_url=lease.base_url,
                    api_key=lease.api_key,
                    provider_options={
                        **lease.provider_options,
                        "max_output_tokens": lease.max_output_tokens,
                    },
                )
                client.generation_policy = get_current_generation_policy()
                if role_instructions and hasattr(client, "set_system_prompt"):
                    client.set_system_prompt(role_instructions)
                self._configure_pool_client_tools(client, enabled=tools_required)
                side_effect_observable = _target_supports_stream_callback(
                    client, "generate_response_async"
                )
                result = await asyncio.wait_for(
                    _call_target_method(
                        client,
                        "generate_response_async",
                        execution_request,
                        max_tokens=lease.max_output_tokens,
                        stream_callback=monitor,
                    ),
                    timeout=max(1, lease.timeout_seconds),
                )
                await finalize_route_lease(
                    lease,
                    actual_usage=_usage_from_client(client),
                    success=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.provider = lease.provider
                self.model = lease.model
                self._mode_preset = lease.effort
                return str(result)
            except asyncio.CancelledError:
                await asyncio.shield(
                    finalize_route_lease(
                        lease,
                        success=False,
                        consume_reserved_on_failure=True,
                        error_class="cancelled",
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                )
                raise
            except Exception as exc:
                last_error = exc
                error_class = _error_class(exc)
                reported_side_effect = getattr(
                    exc, "free_team_side_effect_started", None
                )
                if reported_side_effect is not None:
                    side_effect_started = bool(reported_side_effect)
                    side_effect_observable = True
                await finalize_route_lease(
                    lease,
                    success=False,
                    consume_reserved_on_failure=error_class == "timeout",
                    error_class=error_class,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                retryable = error_class in {
                    "429",
                    "402",
                    "5xx",
                    "timeout",
                    "connection",
                }
                if (
                    side_effect_started
                    or not side_effect_observable
                    or error_class == "timeout"
                    or not retryable
                    or fallback_count >= max_fallbacks
                ):
                    raise
            finally:
                cleanup = getattr(client, "cleanup", None) if client is not None else None
                if callable(cleanup):
                    value = cleanup()
                    if asyncio.iscoroutine(value):
                        await value
        if last_error:
            raise last_error
        raise RuntimeError("無料Teamの利用可能枠がありません")

    def _configure_pool_client_tools(self, client: Any, *, enabled: bool = True) -> None:
        """専門ロールで許可されたtoolだけを候補clientへ渡す。"""

        registry = self._build_tool_registry() if enabled else ToolRegistry()
        if hasattr(client, "_native_tools_enabled"):
            client._native_tools_enabled = enabled
        setter = getattr(client, "set_tool_registry", None)
        if callable(setter):
            setter(registry)
            return
        if hasattr(client, "_tool_registry"):
            client._tool_registry = registry
        recreate_agent = getattr(client, "_create_character_agent", None)
        if callable(recreate_agent) and hasattr(client, "agent"):
            client.agent = recreate_agent()

    async def _approve_pool_route_request(
        self,
        request: str,
        *,
        provider: str,
        model: str,
    ) -> Optional[str]:
        """動的に決まった外部providerにも既存の確認・redactionを適用する。"""

        if not _requires_external_approval({"provider": provider}):
            return request
        gateway = self._privacy_gateway_for_turn()
        privacy_mode = gateway.mode
        review_policy = gateway.settings.review_policy
        notify = gateway.settings.notify
        if privacy_mode == "local_only":
            try:
                gateway.ensure_provider_allowed(provider)
            except ExternalProviderBlocked:
                return None
        if privacy_mode == "protected":
            original_settings = gateway.settings
            # The existing Agent Team permission dialog is the review surface
            # for this path.  Let the gateway produce findings without asking
            # a second callback (and never fall back to raw text).
            gateway.settings = replace(gateway.settings, review_policy="never")
            try:
                protected = await gateway.protect(
                    request,
                    provider=provider,
                    source_kind="agent_team",
                )
            finally:
                gateway.settings = original_settings
            redacted_prompt = str(protected.payload)
            redaction_findings = [finding.as_dict() for finding in protected.findings]
        else:
            redacted_prompt, redaction_findings = request, []
        return await request_external_model_prompt(
            request,
            redacted_prompt=redacted_prompt,
            redaction_findings=redaction_findings,
            provider=provider,
            model=model,
            description=(
                f"Review the {self.display_name} assistant prompt before "
                f"sending it to {provider}/{model}."
            ),
            confirm=review_policy != "never",
            notify=notify,
            request_kind=f"{self.domain_key}_assistant",
        )

    def _get_agent_configs(self) -> tuple[dict[str, Any], dict[str, Any]]:
        agents_config = _config_get(self.config, "agents", {}) or {}
        if not isinstance(agents_config, dict):
            return {}, {}

        domain_config = agents_config.get(self.domain_key, {}) or {}
        if not isinstance(domain_config, dict):
            domain_config = {}

        return agents_config, domain_config

    def _uses_agent_team_subagent_target(self) -> bool:
        return self._agent_team_subagent_route is not None and self._agent_team_subagent_target

    def _subagent_cli_native_allowed(self) -> bool:
        """Whether this Subagent may use provider-native CLI tools."""
        if not agent_team_v3_enabled(self.config):
            return False
        subagent = self._agent_team_subagent
        if not subagent:
            return False
        if not bool(subagent.get("allow_cli_native_tools", False)):
            return False
        return agent_team_workspace_access(subagent) in {"read", "write"}

    def _subagent_cli_workspace_access(self) -> str:
        """Resolve the actual native CLI sandbox ceiling for this child run."""

        subagent = self._agent_team_subagent
        if not subagent or not self._subagent_cli_native_allowed():
            return "read"
        ceiling = agent_team_workspace_access(subagent)
        if ceiling == "write" and self._agent_team_work_mode == "write":
            return "write"
        return "read"

    def _main_provider(self) -> str:
        provider = str(_config_get(self.config, "llm_provider", "openai")).strip().lower()
        return provider or "openai"

    def _active_provider(self) -> str:
        return _runtime_specialist_provider.get() or self.provider

    def _main_model_for_provider(self, provider: str) -> Optional[str]:
        selected = str(_config_get(self.config, "llm_model", "") or "").strip()
        if selected:
            return selected

        provider_model_keys = {
            "openai": ("openai.model",),
            "gemini": ("gemini.model",),
            "openai_compatible_local": (
                "openai_compatible_local.model",
                "openai_compatible_local_model",
            ),
            "sglang": ("sglang.model", "sglang_model"),
            "openrouter": ("openrouter.model", "openrouter_model"),
            "deepseek": ("deepseek.model", "deepseek_model"),
            "deepinfra": ("deepinfra.model", "deepinfra_model"),
            "kimi": ("kimi.model", "kimi_model"),
            "ollama": ("ollama.model", "ollama_model"),
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "grok-cli": ("grok_cli.model",),
        }
        for key in provider_model_keys.get(provider, ()):
            value = str(_config_get(self.config, key, "") or "").strip()
            if value:
                return value
        return None

    def _main_openai_effort(self) -> str:
        """Resolve the main OpenAI effort for a native specialist request."""

        model = str(self.model or self._main_model_for_provider("openai") or "").strip()
        effort = str(
            _config_get(self.config, "openai.reasoning_effort", "") or ""
        ).strip().lower()
        # gpt-5.6-luna is the standard OpenAI fallback for this runner.  Keep
        # its required default explicit when a legacy config omitted effort.
        model_leaf = model.lower().rsplit("/", 1)[-1]
        if not effort and model_leaf.startswith("gpt-5.6-luna"):
            effort = "max"
        if not effort:
            return ""
        try:
            from ..services.llm_model_catalog import reasoning_effort_options_for_model

            options = reasoning_effort_options_for_model("openai", model)
        except Exception:
            options = []
        return effort if effort in options else ""

    def _select_provider(self) -> str:
        if self._agent_team_subagent_route:
            return self._agent_team_subagent_route["provider"]
        return self._main_provider()

    def _select_native_openai_model(self) -> str:
        configured_model = self.model or self._main_model_for_provider(self.provider)
        if configured_model and str(configured_model).strip().startswith(
            NATIVE_OPENAI_MODEL_PREFIXES
        ):
            return str(configured_model).strip()

        if _config_get(self.config, "openai_api_key"):
            return "gpt-5.6-luna"

        llm_model = str(_config_get(self.config, "llm_model", "")).strip()
        if llm_model.startswith(NATIVE_OPENAI_MODEL_PREFIXES):
            return llm_model

        logger.warning(
            "[%sDelegationRunner] No native OpenAI-compatible model configured; "
            "falling back to gpt-5.6-luna",
            self.display_name,
        )
        return "gpt-5.6-luna"

    def _select_model(self) -> Optional[str]:
        if self._agent_team_subagent_route:
            return _normalized_model(self._agent_team_subagent_route.get("model"))

        configured_model = _normalized_model(self._main_model_for_provider(self.provider))

        if self.provider in CLI_PROVIDER_NAMES:
            return configured_model or _normalized_model(
                _config_get(self.config, "llm_model")
            )

        if self.provider == OLLAMA_PROVIDER_NAME:
            ollama_config = _config_get(self.config, "ollama", {}) or {}
            return configured_model or _normalized_model(
                _config_get(self.config, "ollama_model")
            ) or _normalized_model(
                _config_get(self.config, "llm_model")
            ) or _normalized_model(
                ollama_config.get("model") if isinstance(ollama_config, dict) else None
            ) or "gemma4:e4b"

        if self.provider in OPENAI_COMPATIBLE_PROVIDER_NAMES:
            return configured_model

        if self.provider == "openai":
            return configured_model or "gpt-5.6-luna"

        if self.provider == "gemini":
            return configured_model or "gemini-3-flash-preview"

        return self._select_native_openai_model()

    def _create_cli_backend(self):
        if self.provider == "antigravity-cli":
            from .cli_backends.antigravity import AntigravityCLIBackend

            return AntigravityCLIBackend(model=self.model)

        if self.provider == "claude-cli":
            from .cli_backends.claude import ClaudeCLIBackend

            return ClaudeCLIBackend(
                model=self.model,
                reasoning_effort=(
                    self._mode_preset
                    if self._uses_agent_team_subagent_target()
                    else _config_get(self.config, "claude_cli.reasoning_effort")
                ),
            )

        if self.provider == "codex-cli":
            from .cli_backends.codex import CodexCLIBackend

            return CodexCLIBackend(
                model=self.model,
                reasoning_effort=(
                    self._mode_preset
                    if self._uses_agent_team_subagent_target()
                    else _config_get(self.config, "codex_cli.reasoning_effort")
                ),
            )

        if self.provider == "grok-cli":
            from .cli_backends.grok import GrokCLIBackend

            return GrokCLIBackend(model=self.model)

        raise ValueError(f"Unsupported specialist CLI provider: {self.provider}")

    def _deepseek_effort_for_request(self) -> str:
        if self._uses_agent_team_subagent_target():
            effort = str(self._mode_preset or "").strip().lower()
            return effort if effort in {"none", "high", "max"} else ""
        effort = str(
            self._mode_preset
            or _config_get(self.config, "deepseek.reasoning_effort", "high")
            or "high"
        ).strip().lower()
        return effort if effort in {"none", "high", "max"} else "high"

    def _deepinfra_effort_for_request(self) -> str:
        if self._uses_agent_team_subagent_target():
            effort = str(self._mode_preset or "").strip().lower()
            return effort if effort in {"none", "low", "medium", "high"} else ""
        effort = str(
            self._mode_preset
            or _config_get(self.config, "deepinfra.reasoning_effort", "high")
            or "high"
        ).strip().lower()
        return effort if effort in {"none", "low", "medium", "high"} else "high"

    def _mode_extra_body(self) -> dict[str, Any]:
        if self.provider == "deepseek":
            effort = self._deepseek_effort_for_request()
            if not effort:
                return {}
            return {
                "thinking": {
                    "type": "disabled" if effort == "none" else "enabled"
                }
            }
        if self.provider == "deepinfra":
            effort = self._deepinfra_effort_for_request()
            if not effort:
                return {}
            return {"reasoning_effort": effort}
        if self.provider not in {"openai_compatible_local", "sglang"}:
            return {}
        mode = str(self._mode_preset or "").strip().lower()
        if self.provider == "openai_compatible_local":
            metadata = llama_cpp_reasoning_effort_metadata(self.model)
            if metadata:
                if mode and mode not in metadata["options"]:
                    raise ValueError(
                        "Unsupported reasoning effort for managed local profile: "
                        f"{mode!r}; expected one of {metadata['options']}"
                    )
                if not mode:
                    mode = metadata["default"]
                extra_body = llama_cpp_reasoning_effort_request_extra_body(
                    self.model,
                    mode,
                )
                if extra_body is None:
                    raise ValueError(
                        "Managed local profile has invalid reasoning effort wire metadata"
                    )
                return extra_body
        if mode not in {"fast", "thinking"}:
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": mode == "thinking",
            }
        }

    def _with_native_mode_preset(self, agent: Any) -> Any:
        mode = str(self._mode_preset or "").strip()
        if self.provider != "openai" or not mode:
            return agent

        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        if mode not in reasoning_effort_options_for_model("openai", getattr(agent, "model", "")):
            return agent

        current_settings = getattr(agent, "model_settings", None) or NativeModelSettings()
        return replace(
            agent,
            model_settings=replace(
                current_settings,
                reasoning=Reasoning(effort=mode),
            ),
        )

    def _configure_model_environment(self) -> None:
        openai_api_key = _config_get(self.config, "openai_api_key")
        gemini_api_key = _config_get(self.config, "gemini_api_key")

        if openai_api_key:
            os.environ["OPENAI_API_KEY"] = openai_api_key
        if gemini_api_key:
            os.environ["GOOGLE_API_KEY"] = gemini_api_key

    def _get_mcp_config(
        self,
        project_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        # Agent Team v3 children are never allowed to open an arbitrary MCP
        # server. This also blocks a configured Director/browser MCP lane.
        if self._uses_agent_team_subagent_target():
            return {}
        if Features.is_enterprise() or not self.mcp_server_names or not _config_get(
            self.config, "mcp_enabled", False
        ):
            return {}

        configured_servers = _config_get(self.config, "mcp", {}).get("servers", {})
        selected = {
            name: configured_servers[name]
            for name in self.mcp_server_names
            if name in configured_servers
        }
        return {"servers": selected} if selected else {}

    def _augment_request_with_project_context(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        project_context = self._project_context_for_turn(project_context)
        if not project_context:
            return request

        project_block = format_project_context_for_prompt(project_context)
        if not project_block:
            return request

        guidance = (
            "Use this project context as the default target when the user does not specify "
            "another project."
        )
        return f"{project_block}\n{guidance}\n\nUser request:\n{request}"

    def _project_context_for_turn(
        self,
        project_context: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Hide the selected Project's rich context when this turn is OFF.

        A specialist may still receive an explicitly targeted *different*
        Project context from the root agent.  Only the context that merely
        mirrors the UI-selected Project is suppressed on an OFF turn.
        """

        if not project_context:
            return None
        turn = get_turn_context()
        if project_context_enabled_for_client(self):
            return project_context
        selected_project_id = turn.project_id or getattr(self, "current_project_id", None)
        if (
            not project_context_enabled_for_client(self)
            and str(project_context.get("id") or "").strip()
            == str(selected_project_id or "").strip()
        ):
            return None
        return project_context

    def _get_agent_definition(self):
        if self._agent_definition is None:
            self._agent_definition = self._create_agent_instance(
                model=self.model or self._select_native_openai_model()
            ).agent
        return self._agent_definition

    def _create_agent_instance(self, model: Optional[str] = None):
        try:
            return self.agent_class(model=model, config=self.config)
        except TypeError:
            return self.agent_class(model=model)

    def _build_tool_registry(self) -> ToolRegistry:
        if self._tool_registry is not None:
            return self._tool_registry

        registry = ToolRegistry()
        allow_qa_browser = bool(
            self._uses_agent_team_subagent_target()
            and "browser_qa" in (self._agent_team_capabilities or ())
            and str(self.domain_key or "").strip().casefold().replace(" ", "-")
            in {
                "qa",
                "qa-worker",
                "qa_worker",
                "ui-qa",
                "ui_qa",
                "ui-qa-worker",
                "ui_qa_worker",
                "browser-qa",
                "browser_qa",
                "browser-qa-worker",
                "browser_qa_worker",
            }
        )
        for agent_tool in self._get_agent_definition().tools:
            tool_def = self._convert_agent_tool(agent_tool)
            if self._uses_agent_team_subagent_target() and not _child_tool_allowed(
                tool_def,
                allow_qa_browser=allow_qa_browser,
            ):
                logger.warning(
                    "[%sDelegationRunner] Suppressed forbidden child tool %s",
                    self.display_name,
                    tool_def.name,
                )
                continue
            registry.register(tool_def)

        self._tool_registry = registry
        return registry

    def _convert_agent_tool(self, agent_tool: Any) -> ToolDefinition:
        if isinstance(agent_tool, ToolDefinition):
            return agent_tool

        raise TypeError(
            "Specialist agent tools must be native ToolDefinition instances; "
            f"got {type(agent_tool)!r}"
        )

    def _build_cli_system_context(self) -> str:
        agent_definition = self._get_agent_definition()
        registry = self._build_tool_registry()

        parts = [str(getattr(agent_definition, "instructions", "")).strip()]
        if self.provider != "codex-cli" and self._subagent_cli_native_allowed():
            parts.extend(
                [
                    "",
                    "For ad-hoc Python analysis, use `python`; it resolves to the "
                    "AoiTalk runtime environment. If the target workspace has "
                    "its own Python environment, prefer that project-specific "
                    "interpreter when appropriate.",
                ]
            )

        if len(registry):
            parts.extend(
                [
                    "",
                    CLIAdapter.to_prompt_text(registry.get_all()),
                ]
            )

        parts.extend(
            [
                "",
                "あなたは専門アシスタントツールとして実行されています。",
                "依頼を完了するために必要な場合は、利用可能なツールを使ってください。",
                "ツールが必要な場合は [TOOL_CALL: tool_name(key=value)] 形式で出力してください。",
                "ツール結果が返された後は、その結果を根拠に自然に回答してください。",
            ]
        )

        if self._uses_agent_team_subagent_target():
            parts.extend(["", _WORKER_PUBLICATION_INSTRUCTIONS])

        return "\n".join(part for part in parts if part is not None)

    def _build_cli_follow_up_prompt(
        self,
        original_input: str,
        initial_response: str,
        tool_results_text: str,
    ) -> str:
        return "\n".join(
            [
                "# ツール実行結果",
                f"元の専門依頼: {original_input}",
                "",
                "直前の出力:",
                initial_response,
                "",
                tool_results_text,
                "",
                "# 最終回答",
                "上記のツール結果に基づいて、ユーザーの依頼を完了してください。",
                "追加ツールがまだ必要な場合だけ、次のツール呼び出しを出力してください。",
                "最終的にユーザーへ返す回答だけを出力してください。",
            ]
        )

    def _usage_client(
        self,
        *,
        project_context: Optional[dict[str, Any]] = None,
    ) -> _SpecialistUsageClient:
        """Build identity metadata for one specialist usage record."""

        turn_context = get_turn_context()
        runtime_project = project_context or get_runtime_project_context() or {}
        if not isinstance(runtime_project, dict):
            runtime_project = {}

        user_id = (
            getattr(self, "session_user_id", None)
            or turn_context.user_id
            or _config_get(self.config, "user_id", None)
            or "default_user"
        )
        session_id = (
            getattr(self, "current_session_id", None)
            or turn_context.session_id
            or _config_get(self.config, "session_id", None)
        )
        project_id = (
            getattr(self, "current_project_id", None)
            or turn_context.project_id
            or runtime_project.get("id")
            or _config_get(self.config, "project_id", None)
        )
        return _SpecialistUsageClient(
            user_id=str(user_id).strip() if user_id else None,
            session_id=str(session_id).strip() if session_id else None,
            project_id=str(project_id).strip() if project_id else None,
            agent_name=self.display_name,
        )

    def _persist_specialist_usage(
        self,
        usage: Any,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        resolved_model: Optional[str] = None,
        project_context: Optional[dict[str, Any]] = None,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> bool:
        """Normalize and persist one provider-confirmed specialist request."""

        if usage is None:
            return False
        provider_name = str(provider or self.provider or "").strip().lower()
        model_name = str(model or self.model or "").strip()
        if not provider_name or not model_name:
            return False
        try:
            normalized = normalize_usage(
                usage,
                provider=provider_name,
                resolved_model=resolved_model,
            )
            if not normalized:
                return False
            # Do not turn a missing provider usage payload into a fabricated
            # zero-token row. Explicitly reported zeroes remain valid.
            if (
                normalized.get("input_tokens") is None
                and normalized.get("output_tokens") is None
            ):
                return False
            return persist_usage_sync(
                self._usage_client(project_context=project_context),
                provider=provider_name,
                model=model_name,
                usage=normalized,
                request_type=request_type,
                latency_ms=max(0, int(latency_ms or 0)),
                is_streaming=bool(is_streaming),
            )
        except Exception:
            logger.debug(
                "[%sDelegationRunner] usage persistence failed",
                self.display_name,
                exc_info=True,
            )
            return False

    def _persist_specialist_response_usage(
        self,
        response: Any,
        *,
        started_at: float,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        project_context: Optional[dict[str, Any]] = None,
        request_type: str = "chat",
        is_streaming: bool = False,
    ) -> bool:
        return self._persist_specialist_usage(
            _response_usage(response),
            provider=provider,
            model=model,
            resolved_model=_response_model(response),
            project_context=project_context,
            request_type=request_type,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            is_streaming=is_streaming,
        )

    def _run_via_cli(
        self,
        request: str,
        *,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if self._privacy_gateway_for_turn().mode != "direct":
            return (
                f"{self.display_name} delegation blocked: external CLI providers "
                "are disabled in protected/local_only privacy mode"
            )
        if self.cli_backend is None:
            return f"{self.display_name} CLI backend is not configured"

        # A trusted repository run may never use an arbitrary test/custom
        # backend that calls the host shell directly.  Built-in providers all
        # inherit ``CLIBackendBase`` and route scoped execution through the
        # verified WSL2+bubblewrap process owner.  Unsupported custom runners
        # fail closed before the provider receives the request.
        run_scope = None
        try:
            from ..security.agent_run_scope import get_current_run_scope

            run_scope = get_current_run_scope()
        except Exception:
            run_scope = None
        if run_scope is not None and not isinstance(self.cli_backend, CLIBackendBase):
            return (
                f"{self.display_name} delegation blocked: active AgentRunScope "
                "requires a scope-capable CLI backend (WSL2+bwrap)"
            )
        if run_scope is not None and not bool(
            getattr(self.cli_backend, "supports_scoped_run", False)
        ):
            return (
                f"{self.display_name} delegation blocked: CLI backend does not "
                "support the active AgentRunScope"
            )
        if (
            run_scope is not None
            and type(self.cli_backend).execute_prompt is not CLIBackendBase.execute_prompt
            and not bool(getattr(self.cli_backend, "scoped_execution_delegate", False))
        ):
            return (
                f"{self.display_name} delegation blocked: custom CLI override "
                "must delegate to the verified WSL2+bwrap execution seam"
            )

        registry = self._build_tool_registry()
        system_context = self._build_cli_system_context()
        required_tools = self._required_native_openai_tool_names(request)
        if not required_tools:
            required_tools = self._required_ollama_tool_names(request)

        def _execute_cli_prompt(
            prompt: str,
            *,
            system_context: Optional[str] = None,
        ) -> tuple[bool, str]:
            started_at = time.perf_counter()
            success = False
            try:
                kwargs: dict[str, Any] = {
                    "prompt": prompt,
                    # The process cwd is not a security boundary.  When a
                    # parent supplied a trusted scope, point the provider at
                    # the selected repository root; CLIBackendBase then maps
                    # that root to /workspace inside WSL+bwrap.
                    "cwd": (
                        Path(run_scope.canonical_root)
                        if run_scope is not None
                        else Path.cwd()
                    ),
                }
                if system_context is not None:
                    kwargs["system_context"] = system_context
                native_context = nullcontext()
                if self.provider == "codex-cli" and self._subagent_cli_native_allowed():
                    try:
                        from .cli_backends.codex import agent_team_cli_context

                        native_context = agent_team_cli_context(
                            workspace_access=self._subagent_cli_workspace_access()
                        )
                    except Exception:
                        logger.debug("Agent Team Codex native context unavailable", exc_info=True)
                with native_context:
                    success, output = self.cli_backend.execute_prompt(**kwargs)
                return success, output
            finally:
                consume_usage = getattr(self.cli_backend, "consume_last_usage", None)
                usage = None
                if callable(consume_usage):
                    try:
                        usage = consume_usage()
                    except Exception:
                        logger.debug(
                            "[%sDelegationRunner] CLI usage取得に失敗しました",
                            self.display_name,
                            exc_info=True,
                        )
                # A nonzero CLI exit can still represent a provider-billed
                # request (for example a usage-limit event). Persist only the
                # provider-confirmed usage payload; a missing payload remains
                # a no-op and never becomes a fabricated zero-token row.
                self._persist_specialist_usage(
                    usage,
                    model=(
                        getattr(self.cli_backend, "_model", None)
                        or self.model
                    ),
                    request_type="cli",
                    project_context=project_context,
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                )

        success, cli_output = _execute_cli_prompt(
            request,
            system_context=system_context,
        )
        if not success:
            logger.error(
                "[%sDelegationRunner] CLI execution failed: %s",
                self.display_name,
                cli_output,
            )
            return f"{self.display_name} delegation error: {cli_output}"

        turn_result = run_cli_tool_call_loop(
            original_input=request,
            initial_output=cli_output,
            registry=registry,
            parse_tool_calls=self.cli_backend.parse_tool_calls,
            execute_follow_up=lambda follow_up: _execute_cli_prompt(follow_up),
            build_follow_up_prompt=self._build_cli_follow_up_prompt,
            log_prefix=f"{self.display_name}DelegationRunner",
            config=self.config,
            user_input=request,
        )
        tool_call_records = [
            OpenAIToolCallRecord(
                tool=tool_result.call.tool,
                arguments=dict(tool_result.call.arguments),
                result=tool_result.model_output,
            )
            for tool_result in turn_result.tool_results
        ]

        if required_tools:
            return self._validate_openai_tool_loop_result(
                request,
                OpenAIToolCallLoopResult(
                    final_output=turn_result.final_output,
                    tool_calls=tool_call_records,
                ),
                required_tools,
            )
        return turn_result.final_output

    def _run_via_ollama_json_tool_loop(
        self,
        request: str,
        *,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        registry = self._build_tool_registry()
        agent_definition = self._get_agent_definition()
        instructions = str(getattr(agent_definition, "instructions", "")).strip()
        system_prompt = build_json_tool_loop_system_prompt(instructions, registry)

        ollama_config = _config_get(self.config, "ollama", {}) or {}
        base_url = (
            os.getenv("OLLAMA_BASE_URL")
            or _config_get(self.config, "ollama_base_url")
            or ollama_config.get("base_url")
            or "http://127.0.0.1:11434/v1"
        )
        base_url = str(base_url).rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"

        api_key = (
            os.getenv("OLLAMA_API_KEY")
            or _config_get(self.config, "ollama_api_key")
            or ollama_config.get("api_key")
            or "ollama"
        )
        client = OpenAI(base_url=base_url, api_key=api_key)
        model = self.model or "gemma4:e4b"

        initial_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "User request:",
                        request,
                        "",
                        "Decide whether a tool is needed.",
                        (
                            "Choose a relevant available tool only when the request requires external, "
                            "current, or otherwise tool-backed information; do not infer a tool from a single keyword."
                        ),
                        (
                            "When calling a tool with a `request` parameter, copy the user's request exactly "
                            "unless a shorter accurate query is obvious."
                        ),
                    ]
                ),
            },
        ]

        def _create(messages_payload: list[dict[str, Any]]) -> str:
            api_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages_payload,
                "temperature": 0,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
            }
            reasoning_effort = ollama_reasoning_effort_for_mode(
                model,
                self._mode_preset,
            )
            if reasoning_effort:
                api_kwargs["reasoning_effort"] = reasoning_effort
            started_at = time.perf_counter()
            protected = self._privacy_gateway_for_turn().protect_sync(
                api_kwargs,
                provider=self.provider,
                base_url=base_url,
                source_kind="agent_team_model_request",
            )
            response = client.chat.completions.create(**protected.payload)
            self._persist_specialist_response_usage(
                response,
                started_at=started_at,
                model=model,
                project_context=project_context,
                request_type="chat",
            )
            return self._privacy_gateway_for_turn().restore(
                response.choices[0].message.content or ""
            )

        required_tools = self._required_ollama_tool_names(request)
        require_all_tools = self._require_all_required_tools(request, required_tools)
        result = run_json_tool_loop(
            create_completion=_create,
            initial_messages=initial_messages,
            registry=registry,
            original_request=request,
            required_tool_names=required_tools,
            required_tool_reason=self._required_ollama_tool_reason(request, required_tools),
            require_all_required_tools=require_all_tools,
            return_result=bool(required_tools),
            restore_tool_arguments=self._privacy_gateway_for_turn().restore_tool_arguments,
            failure_breaker=(
                ToolFailureCircuitBreaker(max_same_failure=2, failed_tool_budget=8)
                if agent_team_v3_enabled(self.config)
                and agent_team_v3_delegation_enabled(self.config)
                else None
            ),
        )
        if isinstance(result, JsonToolLoopResult):
            validated = self._validate_ollama_tool_loop_result(
                request, result, required_tools
            )
            return self._privacy_gateway_for_turn().restore(validated)
        return self._privacy_gateway_for_turn().restore(result)

    def _openai_compatible_connection(self) -> tuple[str, str]:
        provider = self.provider
        if provider == "openai_compatible_local":
            base_url = (
                os.getenv("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
                or _config_get(self.config, "openai_compatible_local.base_url")
                or "http://127.0.0.1:8080/v1"
            )
            api_key = (
                os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
                or _config_get(self.config, "openai_compatible_local.api_key")
                or "dummy"
            )
        elif provider == "sglang":
            base_url = resolve_sglang_base_url(self.config)
            api_key = (
                os.getenv("SGLANG_API_KEY")
                or _config_get(self.config, "sglang_api_key")
                or "dummy"
            )
        elif provider == "openrouter":
            base_url = (
                os.getenv("OPENROUTER_BASE_URL")
                or _config_get(self.config, "openrouter.base_url")
                or "https://openrouter.ai/api/v1"
            )
            api_key = _config_get(self.config, "openrouter_api_key") or os.getenv("OPENROUTER_API_KEY")
        elif provider == "kimi":
            base_url = (
                os.getenv("MOONSHOT_BASE_URL")
                or _config_get(self.config, "kimi.base_url")
                or "https://api.moonshot.ai/v1"
            )
            api_key = _config_get(self.config, "kimi_api_key") or os.getenv("MOONSHOT_API_KEY")
        elif provider == "deepseek":
            base_url = (
                _config_get(self.config, "deepseek_base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or _config_get(self.config, "deepseek.base_url")
                or "https://api.deepseek.com"
            )
            api_key = _config_get(self.config, "deepseek_api_key") or os.getenv(
                "DEEPSEEK_API_KEY"
            )
        elif provider == "deepinfra":
            base_url = (
                _config_get(self.config, "deepinfra.base_url")
                or _config_get(self.config, "deepinfra_base_url")
                or os.getenv("DEEPINFRA_BASE_URL")
                or "https://api.deepinfra.com/v1/openai"
            )
            api_key = _config_get(self.config, "deepinfra_api_key") or os.getenv(
                "DEEPINFRA_TOKEN"
            )
        else:
            raise ValueError(f"Unsupported OpenAI-compatible specialist provider: {provider}")

        clean_base_url = str(base_url).rstrip("/")
        if provider in {"openai_compatible_local", "sglang"} and not clean_base_url.endswith("/v1"):
            clean_base_url = f"{clean_base_url}/v1"
        return clean_base_url, str(api_key or "dummy")

    def _create_openai_compatible_completion(self, client: OpenAI, api_kwargs: dict[str, Any]) -> Any:
        gateway = self._privacy_gateway_for_turn()
        base_url = str(getattr(client, "base_url", "") or "")
        protected_kwargs = gateway.protect_sync(
            api_kwargs,
            provider=self.provider,
            base_url=base_url,
            source_kind="agent_team_model_request",
        ).payload
        try:
            return client.chat.completions.create(**protected_kwargs)
        except Exception as exc:
            if _is_context_overflow_error(exc):
                raise
            if self.provider in {"kimi", "deepseek", "deepinfra", "openrouter"}:
                raise
            retry_kwargs = dict(protected_kwargs)
            removed = []
            preserve_qwen_effort = bool(
                self.provider == "openai_compatible_local"
                and llama_cpp_reasoning_effort_metadata(self.model)
            )
            for key in ("tools", "tool_choice", "response_format"):
                if key in retry_kwargs:
                    retry_kwargs.pop(key, None)
                    removed.append(key)
            if "extra_body" in retry_kwargs:
                if preserve_qwen_effort:
                    mode_extra = self._mode_extra_body()
                    if mode_extra and retry_kwargs.get("extra_body") != mode_extra:
                        retry_kwargs["extra_body"] = mode_extra
                        removed.append("extra_body(non-mode)")
                else:
                    retry_kwargs.pop("extra_body", None)
                    removed.append("extra_body")
            if not removed:
                raise
            logger.warning(
                "[%sDelegationRunner] Retrying without %s: %s",
                self.display_name,
                ", ".join(removed),
                exc,
            )
            return client.chat.completions.create(**retry_kwargs)

    def _run_via_openai_compatible_tool_loop(
        self,
        request: str,
        *,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        registry = self._build_tool_registry()
        agent_definition = self._get_agent_definition()
        instructions = str(getattr(agent_definition, "instructions", "")).strip()
        base_url, api_key = self._openai_compatible_connection()
        client = OpenAI(base_url=base_url, api_key=api_key)
        context_budget = resolve_context_budget(
            config=self.config,
            provider_key=self.provider,
            base_url=base_url,
            model_name=self.model,
            api_key=api_key,
        )
        required_tools = self._required_openai_compatible_tool_names(request)
        registry = self._openai_compatible_registry_for_budget(
            registry,
            request=request,
            context_window_tokens=context_budget.context_window_tokens,
            required_tools=required_tools,
        )

        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": clip_text_preserve_tail(
                    request,
                    context_budget.message_budget_chars,
                ),
            },
        ]
        api_kwargs: dict[str, Any] = {
            "model": self.model or "local-model",
            "messages": messages,
        }
        deepseek_effort = ""
        if self.provider == "deepseek":
            deepseek_effort = self._deepseek_effort_for_request()
            if deepseek_effort and deepseek_effort != "none":
                api_kwargs["reasoning_effort"] = deepseek_effort
            api_kwargs["max_tokens"] = context_budget.response_tokens
        elif self.provider == "kimi" and self.model == "kimi-k3":
            kimi_effort = "max"
            if self._uses_agent_team_subagent_target():
                route = self._agent_team_subagent_route or {}
                route_policy = str(route.get("effort_policy") or "").strip().lower()
                route_effort = str(
                    route.get("effort") or route.get("reasoning_effort") or ""
                ).strip()
                # Explicit Agent Team routes are capability-checked against
                # the resolved Main model.  If that check dropped an
                # unsupported value (for example ``ultra`` on Kimi K3), do
                # not silently map it to Kimi's fixed ``max`` mode.
                if route_policy in {"same", "lower", "explicit", "default"}:
                    from ..services.llm_model_catalog import reasoning_effort_options_for_model

                    options = reasoning_effort_options_for_model("kimi", self.model)
                    kimi_effort = route_effort if route_effort in options else ""
            if kimi_effort:
                api_kwargs["reasoning_effort"] = kimi_effort
            api_kwargs["max_completion_tokens"] = context_budget.response_tokens
        elif self.provider == "deepinfra":
            api_kwargs["max_tokens"] = context_budget.response_tokens
        else:
            api_kwargs["temperature"] = 0
            api_kwargs["max_tokens"] = context_budget.response_tokens
        extra_body = self._mode_extra_body()
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        if self.provider == "openrouter":
            api_kwargs["extra_body"] = merge_provider_options_into_extra_body(
                api_kwargs.get("extra_body"),
                self.config,
                self.model,
            )
        if len(registry) > 0:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(registry.get_all())
            if self.provider != "deepseek" or deepseek_effort == "none":
                api_kwargs["tool_choice"] = "required" if required_tools else "auto"
        else:
            required_tools = set()

        def _create_and_record(request_kwargs: dict[str, Any]) -> Any:
            started_at = time.perf_counter()
            response = self._create_openai_compatible_completion(client, request_kwargs)
            self._persist_specialist_response_usage(
                response,
                started_at=started_at,
                model=request_kwargs.get("model"),
                project_context=project_context,
                request_type="chat",
            )
            return response

        response = _create_and_record(api_kwargs)
        message = response.choices[0].message
        if getattr(message, "tool_calls", None):
            result = run_openai_tool_call_loop(
                initial_messages=messages,
                assistant_message=message,
                api_kwargs=api_kwargs,
                registry=registry,
                create_completion=_create_and_record,
                log_prefix=f"{self.display_name}DelegationRunner",
                max_rounds=5,
                return_result=bool(required_tools),
                max_tool_result_chars=context_budget.tool_result_chars,
                config=self.config,
                user_input=request,
            restore_tool_arguments=self._privacy_gateway_for_turn().restore_tool_arguments,
            )
            if isinstance(result, OpenAIToolCallLoopResult):
                validated = self._validate_openai_tool_loop_result(
                    request,
                    result,
                    required_tools,
                )
                return self._privacy_gateway_for_turn().restore(validated)
            return self._privacy_gateway_for_turn().restore(result)
        if required_tools:
            validated = self._validate_openai_tool_loop_result(
                request,
                OpenAIToolCallLoopResult(
                    final_output=getattr(message, "content", None) or "",
                    tool_calls=[],
                ),
                required_tools,
            )
            return self._privacy_gateway_for_turn().restore(validated)
        return self._privacy_gateway_for_turn().restore(
            getattr(message, "content", None) or ""
        )

    def _openai_compatible_registry_for_budget(
        self,
        registry: ToolRegistry,
        *,
        request: str,
        context_window_tokens: int,
        required_tools: set[str],
    ) -> ToolRegistry:
        if not _is_constrained_openai_compatible_context(
            self.provider,
            context_window_tokens,
        ):
            return registry

        compact_names = self._compact_openai_compatible_tool_names(
            request,
            required_tools=required_tools,
        )
        if not compact_names:
            return registry

        compact_registry = _clone_registry_with_tools(
            registry,
            compact_names,
            compact=True,
        )
        return compact_registry if len(compact_registry) > 0 else registry

    def _compact_openai_compatible_tool_names(
        self,
        request: str,
        *,
        required_tools: set[str],
    ) -> list[str]:
        return sorted(required_tools)

    async def _approve_external_model_request(self, delegated_request: str) -> Optional[str]:
        if not self._uses_agent_team_subagent_target():
            return delegated_request
        if not subagent_requires_external_approval(self.config, self.domain_key):
            return delegated_request

        gateway = self._privacy_gateway_for_turn()
        privacy_mode = gateway.mode
        review_policy = gateway.settings.review_policy
        notify = gateway.settings.notify
        if privacy_mode == "local_only":
            try:
                gateway.ensure_provider_allowed(self.provider)
            except ExternalProviderBlocked:
                return None
        if privacy_mode == "protected":
            original_settings = gateway.settings
            gateway.settings = replace(gateway.settings, review_policy="never")
            try:
                protected = await gateway.protect(
                    delegated_request,
                    provider=self.provider,
                    source_kind="agent_team",
                )
            finally:
                gateway.settings = original_settings
            redacted_prompt = str(protected.payload)
            redaction_findings = [finding.as_dict() for finding in protected.findings]
        else:
            redacted_prompt, redaction_findings = delegated_request, []
        return await request_external_model_prompt(
            delegated_request,
            redacted_prompt=redacted_prompt,
            redaction_findings=redaction_findings,
            provider=self.provider,
            model=self.model or "",
            description=(
                f"Review the {self.display_name} assistant prompt before "
                f"sending it to {self.provider}/{self.model}."
            ),
            confirm=review_policy != "never",
            notify=notify,
            request_kind=f"{self.domain_key}_assistant",
        )

    def _required_ollama_tool_names(self, request: str) -> set[str]:
        return set()

    def _required_openai_compatible_tool_names(self, request: str) -> set[str]:
        return self._required_ollama_tool_names(request)

    def _require_all_required_tools(
        self,
        request: str,
        required_tools: set[str],
    ) -> bool:
        return False

    def _required_ollama_tool_reason(
        self,
        request: str,
        required_tools: set[str],
    ) -> str | None:
        return None

    def _validate_ollama_tool_loop_result(
        self,
        request: str,
        result: JsonToolLoopResult,
        required_tools: set[str],
    ) -> str:
        return result.final_output

    def _validate_openai_tool_loop_result(
        self,
        request: str,
        result: OpenAIToolCallLoopResult,
        required_tools: set[str],
    ) -> str:
        return result.final_output

    def _required_native_openai_tool_names(self, request: str) -> set[str]:
        return set()

    def _tool_loop_result_from_native_run_result(
        self,
        result: Any,
    ) -> OpenAIToolCallLoopResult:
        records: list[OpenAIToolCallRecord] = []
        pending_calls: list[tuple[str, dict[str, Any]]] = []

        for item in getattr(result, "new_items", []) or []:
            item_type = getattr(item, "type", "")
            raw_item = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = str(getattr(raw_item, "name", "") or "")
                args_text = getattr(raw_item, "arguments", "{}") or "{}"
                try:
                    arguments = json.loads(args_text)
                except Exception:
                    arguments = {}
                if name:
                    pending_calls.append(
                        (name, arguments if isinstance(arguments, dict) else {})
                    )
                continue

            if item_type != "tool_call_output_item":
                continue

            output = getattr(item, "output", "")
            if pending_calls:
                name, arguments = pending_calls.pop(0)
            else:
                name = str(
                    getattr(raw_item, "name", "")
                    or getattr(raw_item, "tool_name", "")
                    or ""
                )
                arguments = {}
            if name:
                records.append(
                    OpenAIToolCallRecord(
                        tool=name,
                        arguments=arguments,
                        result=str(output or ""),
                    )
                )

        return OpenAIToolCallLoopResult(
            final_output=str(getattr(result, "final_output", "") or ""),
            tool_calls=records,
        )

    async def _run_async(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        if not request or not request.strip():
            return f"{self.display_name} request is empty"

        # Keep the selected Project available to authorization/usage code, but
        # do not expose its rich context to a specialist on an explicit OFF
        # turn.  A different Project passed explicitly remains valid.
        runtime_project_context = (
            project_context
            if project_context is not None
            else get_runtime_project_context()
        )
        model_project_context = (
            None
            if project_context is None
            and not project_context_enabled_for_client(self)
            else self._project_context_for_turn(runtime_project_context)
        )
        project_metadata = (
            dict(runtime_project_context.get("metadata") or {})
            if isinstance(runtime_project_context, dict)
            and isinstance(runtime_project_context.get("metadata"), dict)
            and runtime_project_context.get("metadata")
            else None
        )
        privacy_gateway = self._privacy_gateway_for_turn(
            project_metadata=project_metadata,
        )

        if self._route_intent is not None:
            delegated_request = self._augment_request_with_project_context(
                request,
                project_context=model_project_context,
            )
            project_context_token = (
                set_runtime_project_context(runtime_project_context)
                if runtime_project_context
                else None
            )
            try:
                return await self._run_pool_route(delegated_request)
            finally:
                if project_context_token is not None:
                    reset_runtime_project_context(project_context_token)

        # CLI providers cannot honor the outbound privacy boundary.  Reject
        # them before approval prompts or subprocess creation whenever the
        # effective, request-scoped gateway is not direct.
        if self.provider in CLI_PROVIDER_NAMES and privacy_gateway.mode != "direct":
            return (
                f"{self.display_name} delegation blocked: external CLI providers "
                "are disabled in protected/local_only privacy mode"
            )

        self._configure_model_environment()
        delegated_request = self._augment_request_with_project_context(
            request,
            project_context=model_project_context,
        )
        project_context_token = (
            set_runtime_project_context(runtime_project_context)
            if runtime_project_context
            else None
        )

        plugin: MCPPlugin | None = None
        mcp_config = self._get_mcp_config(project_context=runtime_project_context)

        try:
            if self.mcp_server_names:
                if not mcp_config:
                    return f"{self.display_name} MCP is not configured"

                plugin = MCPPlugin()
                success = await plugin.initialize(mcp_config)
                if not success:
                    return f"Failed to initialize {self.display_name} MCP"
                set_mcp_plugin(plugin)

            approved_request = await self._approve_external_model_request(delegated_request)
            if approved_request is None:
                return f"{self.display_name} delegation cancelled"
            delegated_request = approved_request

            # Gemini's native Agent Team path currently has no stable mapping
            # for an explicit catalog effort.  Keep legacy static routes that
            # inherited the provider default (empty effort) runnable, but
            # fail closed for an explicit value rather than silently dropping
            # it before the provider request.
            if self.provider == "gemini" and self._uses_agent_team_subagent_target():
                route = self._agent_team_subagent_route or {}
                if (
                    str(route.get("effort_policy") or "").strip().lower() == "explicit"
                    and str(route.get("effort") or route.get("reasoning_effort") or "").strip()
                ):
                    return (
                        f"{self.display_name} delegation error: Gemini explicit "
                        "effort is unavailable in the native Agent Team runtime"
                    )

            if self.provider in CLI_PROVIDER_NAMES:
                if self._turn_tool_required is not None and not self._subagent_cli_native_allowed():
                    return (
                        f"{self.display_name} delegation error: Agent Team CLI "
                        "providers cannot enforce the declared read-only capability "
                        "boundary; configure a native provider or a CLI-native Subagent."
                    )
                return await asyncio.to_thread(
                    self._run_via_cli,
                    delegated_request,
                    project_context=model_project_context,
                )

            if self.provider == OLLAMA_PROVIDER_NAME:
                return await asyncio.to_thread(
                    self._run_via_ollama_json_tool_loop,
                    delegated_request,
                    project_context=model_project_context,
                )

            if self.provider in OPENAI_COMPATIBLE_PROVIDER_NAMES:
                return await asyncio.to_thread(
                    self._run_via_openai_compatible_tool_loop,
                    delegated_request,
                    project_context=model_project_context,
                )

            agent = self._create_agent_instance(
                model=self.model or self._select_native_openai_model()
            ).agent
            agent = self._with_native_mode_preset(agent)
            # ``run_native_agent_once`` is OpenAI-compatible, but its
            # historical defaults are the official OpenAI transport.  Pass
            # the already-resolved provider transport explicitly so a Gemini
            # Main/Agent-Team/Character route cannot silently use
            # ``OPENAI_API_KEY`` or the OpenAI base URL.  The shared resolver
            # keeps this in step with GroupChat's existing provider handling
            # (including Gemini's /v1beta/openai endpoint and local/compatible
            # providers if a future native path uses them).
            from .group_chat_manager import _resolve_native_transport

            native_provider, native_transport = _resolve_native_transport(
                self.config,
                self.provider,
                model=str(getattr(agent, "model", None) or self.model or "").strip(),
            )
            # Execution Profile resolution owns policy for every Agent Team
            # route.  Preserve its marker even when the effective value is
            # empty (model-default or dropped unsupported explicit) so
            # downstream adapters cannot resurrect provider-global defaults.
            native_config = self.config
            if self._uses_agent_team_subagent_target():
                from .manager import TargetConfig

                route = self._agent_team_subagent_route or {}
                route_policy = str(route.get("effort_policy") or "").strip()
                route_effort = str(
                    route.get("effort")
                    or route.get("reasoning_effort")
                    or ""
                ).strip()
                native_config = TargetConfig(
                    self.config,
                    {
                        # Mark every canonical Agent Team route, including
                        # model-default with an empty effective value.  This
                        # tells downstream adapters that the route resolver
                        # intentionally chose the model default and prevents
                        # provider-specific fallback/remapping from changing
                        # that meaning (for example Kimi K3's fixed max).
                        "runtime.agent_team_effort_policy": (
                            route_policy
                            if route_policy in {"same", "lower", "explicit", "default"}
                            else ""
                        ),
                        "runtime.agent_team_effective_effort": route_effort,
                    },
                )
            native_kwargs = {
                "api_key": native_transport.get("api_key"),
                "base_url": native_transport.get("base_url"),
                "default_headers": native_transport.get("default_headers"),
                "provider_label": native_provider,
                "config": native_config,
                "privacy_gateway": privacy_gateway,
                "session_id": str(getattr(self, "current_session_id", None) or "")
                or None,
                "user_id": str(getattr(self, "session_user_id", None) or "") or None,
            }
            # A few legacy embedders monkeypatch the native runner with the
            # historical ``(agent, prompt, config=...)`` callable.  Filter
            # only when the replacement explicitly rejects **kwargs; the
            # production runner accepts the complete provider transport above.
            try:
                native_signature = inspect.signature(run_native_agent_once)
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in native_signature.parameters.values()
                )
                if not accepts_kwargs:
                    native_kwargs = {
                        key: value
                        for key, value in native_kwargs.items()
                        if key in native_signature.parameters
                    }
            except (TypeError, ValueError):
                # Signature introspection is diagnostics/compatibility-only;
                # the native call itself remains authoritative.
                pass
            result = await run_native_agent_once(
                agent,
                delegated_request,
                **native_kwargs,
            )
            # Native runtime intentionally returns one normalized usage record
            # per actual provider request. Persist each record independently;
            # this branch has no long-lived target client that could otherwise
            # persist the same requests.
            for usage in getattr(result, "usage_records", []) or []:
                self._persist_specialist_usage(
                    usage,
                    model=getattr(agent, "model", None),
                    project_context=runtime_project_context,
                    request_type="chat",
                )
            # Preserve a structured non-retryable high-level tool failure for
            # the parent Agent Team runtime.  Models often paraphrase a Docs
            # error into ``Error: ...``; returning the original envelope keeps
            # the parent circuit breaker independent of provider wording and
            # prevents a retry storm from argument variation.
            failure_candidates = [
                getattr(record, "result", None)
                for record in (getattr(result, "tool_calls", []) or [])
            ]
            # Responses/compatibility adapters may retain only the serialized
            # tool message (rather than ``tool_calls``) on the normalized
            # result.  Inspect those bounded tool payloads as a fallback so a
            # model paraphrase cannot hide a structured Docs failure.
            failure_candidates.extend(
                message.get("content")
                for message in (getattr(result, "messages", []) or [])
                if isinstance(message, dict) and message.get("role") == "tool"
            )
            for candidate in failure_candidates:
                failure = parse_structured_tool_failure(candidate)
                if failure and not bool(failure.get("retryable")):
                    return str(candidate or "")
            required_tools = self._required_native_openai_tool_names(delegated_request)
            if required_tools:
                return self._validate_openai_tool_loop_result(
                    delegated_request,
                    OpenAIToolCallLoopResult(
                        final_output=result.final_output,
                        tool_calls=[
                            OpenAIToolCallRecord(
                                tool=record.tool,
                                arguments=record.arguments,
                                result=record.result,
                            )
                            for record in result.tool_calls
                        ],
                    ),
                    required_tools,
                )
            return result.final_output
        except GenerationInterrupted:
            # Parent steering is a lifecycle boundary, not a specialist
            # failure string.  Propagate it through the delegate tool so the
            # response handler can apply the continuation delta.
            raise
        except Exception as exc:
            logger.exception(
                "[%sDelegationRunner] Delegation failed", self.display_name
            )
            return f"{self.display_name} delegation error: {exc}"
        finally:
            if project_context_token is not None:
                reset_runtime_project_context(project_context_token)
            if plugin is not None:
                try:
                    await plugin.cleanup()
                except Exception:
                    logger.debug(
                        "[%sDelegationRunner] MCP cleanup failed",
                        self.display_name,
                        exc_info=True,
                    )
                finally:
                    set_mcp_plugin(None)

    def run(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        role_token = set_current_agent_team_role(self.domain_key)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(
                    self._run_scoped_async(request, project_context=project_context)
                )
            finally:
                reset_current_agent_team_role(role_token)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                context = contextvars.copy_context()
                future = pool.submit(
                    lambda: context.run(
                        asyncio.run,
                        self._run_scoped_async(request, project_context=project_context),
                    ),
                )
                return future.result()
        finally:
            reset_current_agent_team_role(role_token)

    async def run_async(
        self,
        request: str,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Run delegation on the caller's event loop."""
        role_token = set_current_agent_team_role(self.domain_key)
        try:
            return await self._run_scoped_async(request, project_context=project_context)
        finally:
            reset_current_agent_team_role(role_token)

    async def _run_scoped_async(
        self,
        request: str,
        *,
        project_context: Optional[dict[str, Any]] = None,
    ) -> str:
        run_scope = self._run_scope_from_project_context(project_context)
        if (
            self._uses_agent_team_subagent_target()
            and agent_team_workspace_access(self._agent_team_subagent) in {"read", "write"}
            and isinstance(project_context, dict)
            and bool(
                project_context.get("require_run_scope")
                or (
                    isinstance(project_context.get("metadata"), dict)
                    and project_context["metadata"].get("require_run_scope")
                )
            )
            and run_scope is None
        ):
            return (
                f"{self.display_name} delegation blocked: parent controller must "
                "provide an immutable AgentRunScope for repository work"
            )
        if run_scope is None:
            return await self._run_async(request, project_context=project_context)
        from ..security.agent_run_scope import run_scope_context

        with run_scope_context(run_scope):
            return await self._run_async(request, project_context=project_context)

    @staticmethod
    def _run_scope_from_project_context(
        project_context: Optional[dict[str, Any]],
    ) -> Any:
        """Resolve the explicit repository run scope carried by a child turn.

        Existing chat/project turns do not carry a repository and retain their
        historical behavior.  Coding-agent callers opt in by passing an
        immutable ``AgentRunScope`` instance in the trusted context; raw paths
        from model/project metadata are never converted into a scope.
        """

        if not isinstance(project_context, dict):
            return None
        from ..security.agent_run_scope import AgentRunScope

        candidate = project_context.get("run_scope")
        if isinstance(candidate, AgentRunScope):
            return candidate
        # A child must not be able to turn an arbitrary model-supplied path
        # into a mutation scope.  The parent controller must construct and
        # pass the immutable AgentRunScope object itself.
        return None


class _AgentTeamSubagentAgent:
    def __init__(
        self,
        *,
        model: str,
        subagent_id: str,
        label: str,
        tools: Sequence[ToolDefinition],
        instructions: str | None = None,
    ) -> None:
        self.model = model
        self.subagent_id = subagent_id
        self.label = label
        self.tools = list(tools)
        self.instructions = str(instructions or "").strip()
        self._agent: NativeAgentDefinition | None = None

    @property
    def agent(self) -> NativeAgentDefinition:
        if self._agent is None:
            instructions = self.instructions or TEAM_SUBAGENT_INSTRUCTIONS.get(
                self.subagent_id,
                f"You are the Agent Team {self.label} Subagent.",
            )
            # API/native workers receive this instruction through their system
            # message (CLI workers get the equivalent in
            # ``_build_cli_system_context``).  Tool filtering is authoritative;
            # this text keeps the model's plan aligned with the leaf boundary
            # and avoids asking a child to perform nested orchestration.
            instructions = f"{instructions}\n\n{_WORKER_PUBLICATION_INSTRUCTIONS}"
            self._agent = NativeAgentDefinition(
                name=f"AgentTeam_{self.subagent_id}",
                model=self.model,
                instructions=instructions,
                model_settings=NativeModelSettings(tool_choice="auto"),
                tools=list(self.tools),
            )
        return self._agent


def _agent_team_bm25_tools(*, tools_required: bool = True) -> list[ToolDefinition]:
    """Return the managed BM25 bridge for read-capable Agent Team children.

    The CLI backend may also expose provider-native filesystem tools, but BM25
    must stay on the AoiTalk-managed path so its scope/ACL checks do not depend
    on the subprocess working directory.
    """
    if not tools_required:
        return []
    try:
        from ..tools.bm25_search import build_bm25_search_tool_definition

        context: dict[str, Any] = {}
        runtime_context = get_runtime_project_context()
        if isinstance(runtime_context, dict):
            context.update(runtime_context)
        turn = get_turn_context()
        if getattr(turn, "user_id", None) and "user_id" not in context:
            context["user_id"] = turn.user_id
        if getattr(turn, "project_id", None) and "project_id" not in context:
            context["project_id"] = turn.project_id
        return ensure_tool_definitions(
            [build_bm25_search_tool_definition(context=context)]
        )
    except Exception:
        logger.debug("BM25 tool unavailable for Agent Team subagent", exc_info=True)
        return []


def _agent_team_read_tools(
    capabilities: Sequence[str] = (),
    *,
    tools_required: bool = True,
) -> list[ToolDefinition]:
    """Resolve the small read-only capability set declared by a Team Subagent."""
    if not tools_required:
        return []

    requested = {str(value).strip() for value in capabilities if str(value).strip()}
    tools: list[Any] = []
    if "workspace_read" in requested:
        from ..tools.file_explorer.file_explorer_tools import get_workspace_file_info
        from ..tools.os_operations import list_directory, read_file, search_files

        tools.extend([list_directory, search_files, read_file, get_workspace_file_info])
        tools.extend(_agent_team_bm25_tools(tools_required=True))
    if "repo_map" in requested:
        from ..tools.repo_map.tools import get_repo_map

        tools.append(get_repo_map)
    return ensure_tool_definitions(tools)


def _app_development_team_active(config: Any, team_id: str) -> bool:
    """Check the selected Team's canonical App Development activation."""

    clean_team_id = str(team_id or "").strip()
    if not clean_team_id:
        return False
    selected = next(
        (
            item
            for item in agent_team_v3_teams(config)
            if str(item.get("team_id") or "") == clean_team_id
        ),
        None,
    )
    if not selected or not selected.get("enabled", True):
        return False
    activation = selected.get("activation") or {}
    contexts = {
        str(item).strip().lower()
        for item in activation.get("contexts", []) or []
        if str(item).strip()
    }
    if "app_development" not in contexts:
        return False
    from .tool_packs import contextual_agent_team_scope

    scope = contextual_agent_team_scope(
        config,
        project_context=get_runtime_project_context(),
    )
    return bool(
        scope.get("app_development_active")
        and clean_team_id in set(scope.get("active_team_ids") or ())
    )


def _apps_feature_enabled(config: Any) -> bool:
    """Read the canonical Apps feature flag without widening child ACLs."""

    if config is None:
        return True
    try:
        apps_section = config.get("apps", {})
    except (AttributeError, TypeError):
        apps_section = {}
    if isinstance(apps_section, Mapping):
        return bool(apps_section.get("enabled", True))
    try:
        return bool(config.get("apps.enabled", True))
    except (AttributeError, TypeError):
        return True


def _bind_story_tools_to_turn(
    tools: Sequence[ToolDefinition],
) -> list[ToolDefinition]:
    """Bind Story child tools to the trusted TurnContext session ID."""

    bound: list[ToolDefinition] = []
    for item in tools:
        if item.name not in {
            "get_story_context",
            "write_episode_body",
            "revise_episode_body",
            "add_story_note",
            "get_character_voice",
        }:
            bound.append(item)
            continue
        if not any(param.name == "conversation_id" for param in item.parameters):
            bound.append(item)
            continue
        original = item.function

        def _trusted_id(kwargs: dict[str, Any]) -> str:
            turn_session_id = str(getattr(get_turn_context(), "session_id", None) or "").strip()
            if not turn_session_id:
                raise PermissionError(
                    "Story child tool requires a trusted TurnContext session_id"
                )
            supplied = str(kwargs.pop("conversation_id", "") or "").strip()
            if supplied and supplied != turn_session_id:
                raise PermissionError(
                    "Story child tool conversation_id must match the trusted TurnContext"
                )
            return turn_session_id

        if inspect.iscoroutinefunction(original):
            async def _async_bound(
                *args: Any,
                _original: Any = original,
                **kwargs: Any,
            ) -> Any:
                trusted_id = _trusted_id(kwargs)
                return await _original(
                    *args,
                    conversation_id=trusted_id,
                    **kwargs,
                )

            wrapper = _async_bound
        else:
            def _sync_bound(
                *args: Any,
                _original: Any = original,
                **kwargs: Any,
            ) -> Any:
                trusted_id = _trusted_id(kwargs)
                return _original(
                    *args,
                    conversation_id=trusted_id,
                    **kwargs,
                )

            wrapper = _sync_bound
        bound.append(
            replace(
                item,
                function=wrapper,
                is_async=inspect.iscoroutinefunction(wrapper),
                parameters=[
                    param
                    for param in item.parameters
                    if param.name != "conversation_id"
                ],
            )
        )
    return bound


def _agent_team_tools_for_capabilities(
    capabilities: Sequence[str] = (),
    *,
    tools_required: bool = True,
    work_mode: str = "read",
    backend: str = "api",
    config: Any = None,
    app_development_team: bool = False,
    app_subagent: dict[str, Any] | None = None,
    project_context: Mapping[str, Any] | None = None,
) -> list[ToolDefinition]:
    """Build capability-filtered ToolDefinitions for v3 Subagents.

    CLI native filesystem/shell capabilities stay with the CLI backend.  Only
    AoiTalk application data is exposed as high-level bridge tools; in
    particular docs_operator never receives a database/session handle.
    """
    if not tools_required:
        return []
    requested = tuple(str(value).strip() for value in capabilities if str(value).strip())
    tools: list[Any] = []
    qa_role = str((app_subagent or {}).get("subagent_id") or "").strip()
    allow_qa_browser = "browser_qa" in requested and bool(qa_role)
    if allow_qa_browser:
        tools.extend(_qa_browser_tools_from_context(project_context, role=qa_role))
    if str(backend or "api").lower() != "cli" and (
        "workspace_read" in requested or "workspace_write" in requested
    ):
        tools.extend(_agent_team_read_tools(("workspace_read",), tools_required=True))
        if "workspace_write" in requested and work_mode == "write":
            try:
                from ..tools.file_explorer.file_explorer_tools import (
                    copy_workspace_item,
                    create_workspace_directory,
                    delete_workspace_item,
                    move_workspace_item,
                    upload_workspace_file,
                )
                from ..tools.os_operations import (
                    append_to_file,
                    create_file,
                    delete_file,
                    edit_file,
                    insert_to_file,
                    undo_edit,
                )

                tools.extend(
                    ensure_tool_definitions(
                        [
                            create_workspace_directory,
                            upload_workspace_file,
                            delete_workspace_item,
                            move_workspace_item,
                            copy_workspace_item,
                            create_file,
                            delete_file,
                            append_to_file,
                            edit_file,
                            insert_to_file,
                            undo_edit,
                        ]
                    )
                )
            except Exception:
                logger.debug("Workspace write tools unavailable for Agent Team subagent", exc_info=True)
    elif str(backend or "api").lower() == "cli" and "workspace_read" in requested:
        # Native filesystem/search remains provider-owned for CLI children;
        # BM25 is the one managed high-level bridge that still carries the
        # AoiTalk authorization boundary.
        tools.extend(_agent_team_bm25_tools(tools_required=True))
    if "repo_map" in requested:
        tools.extend(_agent_team_read_tools(("repo_map",), tools_required=True))
    if "web_read" in requested:
        # Web Search is a Shared Tool: API and CLI Subagents receive the same
        # high-level function directly, never a nested research agent.
        try:
            from ..tools.basic.web_search import web_search_with_config

            def web_search(query: str) -> str:
                """Search the public web for fresh or time-sensitive information."""

                return web_search_with_config(query, config=config)

            tools.append(web_search)
        except Exception:
            logger.debug("Web Search tool unavailable for Agent Team Subagent", exc_info=True)
    if "docs_read" in requested or "docs_write" in requested:
        try:
            from ..tools.docs_direct import build_docs_direct_tools

            for item in ensure_tool_definitions(build_docs_direct_tools()):
                is_read = item.name in DOCS_READ_TOOL_NAMES
                is_mutation = item.name in DOCS_MUTATION_TOOL_NAMES
                if not (
                    (is_read and "docs_read" in requested)
                    or (
                        is_mutation
                        and "docs_write" in requested
                        and work_mode == "write"
                    )
                ):
                    continue
                tools.append(
                    replace(
                        item,
                        owner="docs",
                        side_effect="mutation" if is_mutation else "none",
                        risk="medium" if is_mutation else "low",
                        requires_approval=is_mutation,
                        supports_parallel=not is_mutation,
                    )
                )
        except Exception:
            logger.debug("Docs high-level tools unavailable for Agent Team subagent", exc_info=True)
    if "project_read" in requested or "project_write" in requested:
        try:
            from ..agents.project_management_agent import ProjectManagementAgent

            for item in ProjectManagementAgent(model="").agent.tools:
                is_read = item.name in PROJECT_MANAGEMENT_READ_TOOL_NAMES
                is_mutation = item.name in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
                if not (
                    (is_read and "project_read" in requested)
                    or (
                        is_mutation
                        and "project_write" in requested
                        and work_mode == "write"
                    )
                ):
                    continue
                tools.append(
                    replace(
                        item,
                        owner="project_management",
                        side_effect="mutation" if is_mutation else "none",
                        risk="medium" if is_mutation else "low",
                        requires_approval=is_mutation,
                        supports_parallel=False,
                    )
                )
        except Exception:
            logger.debug("Project high-level tools unavailable for Agent Team subagent", exc_info=True)
    if "story_write" in requested:
        try:
            from ..tools.writing_tools import (
                add_story_note,
                get_character_voice,
                get_story_context,
                revise_episode_body,
                write_episode_body,
            )

            tools.extend(
                ensure_tool_definitions(
                    [
                        get_story_context,
                        write_episode_body,
                        revise_episode_body,
                        add_story_note,
                        get_character_voice,
                    ]
                )
            )
        except Exception:
            logger.debug("Story writing tools unavailable for Agent Team subagent", exc_info=True)
    if "story_read" in requested:
        try:
            from ..tools.writing_tools import get_character_voice, get_story_context

            tools.extend(
                ensure_tool_definitions([get_story_context, get_character_voice])
            )
        except Exception:
            logger.debug("Story read tools unavailable for Agent Team Subagent", exc_info=True)
    if "story_import" in requested:
        try:
            from ..tools.import_tools import (
                analyze_import_files,
                import_file_as_character,
                import_file_as_lore,
                import_file_as_scene,
            )

            tools.extend(
                ensure_tool_definitions(
                    [
                        analyze_import_files,
                        import_file_as_character,
                        import_file_as_lore,
                        import_file_as_scene,
                    ]
                )
            )
        except Exception:
            logger.debug("Story import tools unavailable for Agent Team subagent", exc_info=True)
    if "media" in requested:
        try:
            from ..agents.media_agent import MediaAgent

            tools.extend(MediaAgent(model="").agent.tools)
        except Exception:
            logger.debug("Media tools unavailable for Agent Team subagent", exc_info=True)
    if app_development_team and _apps_feature_enabled(config):
        try:
            runtime_context = get_runtime_project_context()
            app_tools = build_app_tool_definitions(
                runtime_context,
                deployment_config=config if isinstance(config, dict) else getattr(config, "config", None),
            )
            if work_mode == "read" or not agent_team_subagent_allows_write(
                app_subagent
            ):
                app_tools = [
                    item
                    for item in app_tools
                    if item.side_effect == "none" and not item.requires_approval
                ]
            tools.extend(app_tools)
        except Exception:
            logger.debug(
                "App Development high-level tools unavailable for Agent Team subagent",
                exc_info=True,
            )
    # Spotify is a Shared Integration and is never an Agent Team capability.
    # Direct Spotify tools are registered by the root runtime only when the
    # integration is enabled and loaded through the shared tool pack.
    # Never expose native shell definitions as AoiTalk ToolDefinitions for API
    # Subagents.  A CLI backend receives its native tools from the provider.
    if str(backend or "api").lower() != "cli":
        tools = [item for item in ensure_tool_definitions(tools) if item.name not in {"shell_tool", "unified_exec", "command_execute"}]
    # This helper is only used for Team children.  Apply the same final deny
    # list as the generic registry builder after all capability packs have
    # contributed tools, so a future pack cannot accidentally reintroduce a
    # nested delegate, Director/browser, MCP, or publication tool.
    tools = [
        item
        for item in ensure_tool_definitions(tools)
        if _child_tool_allowed(item, allow_qa_browser=allow_qa_browser)
    ]
    tools = _bind_story_tools_to_turn(tools)
    unique: dict[str, ToolDefinition] = {}
    for item in ensure_tool_definitions(tools):
        if not _child_tool_allowed(item, allow_qa_browser=allow_qa_browser):
            continue
        unique.setdefault(item.name, item)
    return list(unique.values())


class AgentTeamSubagentDelegationRunner(SpecialistDelegationRunner):
    def __init__(
        self,
        config: Config,
        *,
        subagent_id: str,
        display_name: str,
        model: Optional[str] = None,
        team_id: Optional[str] = None,
        llm_profile_id: Optional[str] = None,
        tool_required: Optional[bool] = None,
        capabilities: Sequence[str] | None = None,
        work_mode: str = "read",
    ):
        super().__init__(
            config,
            domain_key=subagent_id,
            display_name=display_name,
            agent_class=object,
            model=model,
            agent_team_profile_id=llm_profile_id,
            agent_team_team_id=team_id,
            tool_required=tool_required,
            capabilities=capabilities,
            work_mode=work_mode,
        )

    def _create_agent_instance(self, model: Optional[str] = None):
        if agent_team_v3_enabled(self.config) and self._agent_team_subagent:
            subagent = self._agent_team_subagent
            subagent_policy = subagent
            route = self._agent_team_subagent_route or {}
            backend = str(route.get("backend") or ("cli" if self.provider in CLI_PROVIDER_NAMES else "api"))
            requested = filter_agent_team_capabilities(
                subagent_policy,
                requested=self._agent_team_capabilities or None,
                work_mode=self._agent_team_work_mode,
                backend=backend,
            )
            # ``workspace_*`` IDs are provider-native for CLI and therefore
            # omitted by the API capability resolver.  API Subagents still use
            # AoiTalk-managed workspace ToolDefinitions (with ACL/approval),
            # so carry those logical capabilities into the tool bridge only.
            if backend != "cli":
                workspace_access = agent_team_workspace_access(subagent_policy)
                requested = tuple(
                    dict.fromkeys(
                        [
                            *requested,
                            *(
                                capability
                                for capability in (subagent_policy.get("capability_ids") or [])
                                if capability in {"workspace_read", "workspace_write"}
                                and (
                                    (
                                        capability == "workspace_read"
                                        and workspace_access in {"read", "write"}
                                    )
                                    or (
                                        capability == "workspace_write"
                                        and workspace_access == "write"
                                        and self._agent_team_work_mode == "write"
                                    )
                                )
                            ),
                        ]
                    )
                )
            elif "workspace_read" in (
                subagent_policy.get("capability_ids") or []
            ):
                # ``workspace_read`` is provider-native for CLI and is
                # intentionally stripped by the canonical capability filter.
                # Carry the logical read marker back only so the managed BM25
                # bridge is included; native filesystem/search stays with the
                # CLI provider and still requires its existing opt-in.
                requested = tuple(dict.fromkeys([*requested, "workspace_read"]))
            return _AgentTeamSubagentAgent(
                model=model or self.model or self._select_native_openai_model(),
                subagent_id=self.domain_key,
                label=self.display_name,
                tools=_agent_team_tools_for_capabilities(
                    requested,
                    tools_required=self._turn_tool_required is not False,
                    work_mode=self._agent_team_work_mode,
                    backend=backend,
                    config=self.config,
                    app_development_team=_app_development_team_active(
                        self.config,
                        str(self._agent_team_team_id or ""),
                    ),
                    app_subagent=subagent_policy,
                    project_context=get_runtime_project_context(),
                ),
                instructions=str(subagent_policy.get("instructions") or "").strip(),
            )
        return _AgentTeamSubagentAgent(
            model=model or self.model or self._select_native_openai_model(),
            subagent_id=self.domain_key,
            label=self.display_name,
            tools=_agent_team_read_tools(
                self._agent_team_capabilities,
                tools_required=self._turn_tool_required is not False,
            ),
        )


class MediaDelegationRunner(SpecialistDelegationRunner):
    def __init__(self, config: Config, model: Optional[str] = None):
        from ..agents.media_agent import MediaAgent

        super().__init__(
            config,
            domain_key="media",
            display_name="Media",
            agent_class=MediaAgent,
            model=model,
        )
