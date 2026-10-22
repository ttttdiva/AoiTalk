"""Native-first capability classification for CLI coding-agent backends.

Claude Code / Codex CLI already own repository coding primitives (file search,
read, edit and shell) inside their working directory.  AoiTalk must not show a
second, equivalent copy of those primitives to the same model, but it must keep
its high-level bridges for data the CLI cannot reach by itself (Docs,
Projects/Tasks, Calendar, Record Tables, BM25 with AoiTalk authorization,
managed workspace tree/placement, Media/Story/App tools, ...).

The generic AoiTalk file tools are only *equivalent* to the provider-native
ones when both act on the same root.  That is the case while a trusted
``AgentRunScope`` is bound: the CLI runs with ``cwd`` = scope root (inside
WSL+bwrap when available) and the AoiTalk tools resolve paths through the same
scope.  Outside a repository scope the AoiTalk tools address the user's
managed workspace with AoiTalk ACL/conversion semantics, so they remain a
high-level bridge and are not removed.
"""

from __future__ import annotations

import logging
import inspect
import os
import platform
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..tools.registry import ToolRegistry
from ..features import Features

logger = logging.getLogger(__name__)

NATIVE_READ = "read"
NATIVE_EDIT = "edit"
NATIVE_SHELL = "shell"

# Generic repository primitives, grouped by the native capability that makes
# them redundant.  Managed-workspace / Docs placement / user-file tools and
# ``bm25_search`` are intentionally absent: they carry AoiTalk authorization.
# ``get_repo_map`` is an AoiTalk analysis (not a native primitive) and stays a
# bridge, matching the Agent Team CLI capability mapping.
CLI_NATIVE_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "list_directory", "search_files"}
)
CLI_NATIVE_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_file",
        "delete_file",
        "append_to_file",
        "edit_file",
        "insert_to_file",
        "undo_edit",
    }
)
CLI_NATIVE_SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "execute_command",
        "read_command_output",
        "write_command_input",
        "stop_command",
        "list_commands",
    }
)
CLI_NATIVE_REPOSITORY_TOOL_NAMES: frozenset[str] = (
    CLI_NATIVE_READ_TOOL_NAMES | CLI_NATIVE_EDIT_TOOL_NAMES | CLI_NATIVE_SHELL_TOOL_NAMES
)
_TOOLS_BY_CAPABILITY: dict[str, frozenset[str]] = {
    NATIVE_READ: CLI_NATIVE_READ_TOOL_NAMES,
    NATIVE_EDIT: CLI_NATIVE_EDIT_TOOL_NAMES,
    NATIVE_SHELL: CLI_NATIVE_SHELL_TOOL_NAMES,
}

# Bundled AoiTalk MCP servers (``python -m src.mcp_server_entry <key>``) whose
# whole surface is generic filesystem/shell.  Only these are skipped for a CLI
# with native repository tools; every other bundled server and every
# user-added external MCP server is passed through unchanged.
BUNDLED_MCP_NATIVE_DUPLICATE_SERVER_KEYS: frozenset[str] = frozenset({"os_operations"})
_BUNDLED_MCP_ENTRY_MODULE = "src.mcp_server_entry"

# Only these server identities are eligible for a native Agent turn.  The
# server configuration must additionally opt into the formal work-context
# contract below; a display name alone never grants MCP access.
FORMAL_AGENT_MCP_SERVER_KEYS: frozenset[str] = frozenset(
    {"agent_activity", "agent_dialogue", "dialogue8", "agent_dialogue8"}
)
_FORMAL_AGENT_ENTRY_MODULE = "src.mcp_server_entry"
_FORMAL_AGENT_SERVER_SETTING_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "formal_agent_work_context",
        "agent_work_context",
    }
)
_FORMAL_AGENT_ENV_SHADOW_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "AOITALK_PYTHON",
    }
)


def backend_has_native_repository_tools(backend: Any) -> bool:
    return bool(getattr(backend, "native_repository_tools", False))


def _current_run_scope() -> Any | None:
    try:
        from ..security.agent_run_scope import get_current_run_scope

        return get_current_run_scope()
    except Exception:
        return None


def _agent_execution_active(client: Any | None = None) -> bool:
    """Return whether this invocation belongs to a pinned Chat Agent turn."""

    try:
        from ..services.turn_context import get_turn_context

        context = get_turn_context()
        return bool(
            getattr(context, "agent_execution_binding", None) is not None
            or getattr(client, "_agent_native_scope_required", False)
        )
    except Exception:
        # An ephemeral Agent client is an authoritative marker even when a
        # worker thread lost TurnContext propagation. Legacy clients have no
        # marker and retain their ordinary path.
        return bool(getattr(client, "_agent_native_scope_required", False))


def _agent_dialogue_policy(client: Any | None) -> tuple[bool, bool, bool] | None:
    """Return ``(bound, readonly, native_allowed)`` for a CLI Agent client.

    A client marker alone is not a workspace authority.  The adapter attaches
    an immutable ``AgentDialogueWorkspace`` plus an active lease; the native
    gate rechecks both before every provider invocation.  ``None`` keeps the
    legacy context-only compatibility path used by older gate unit tests.
    """

    if client is None:
        return None
    if not bool(getattr(client, "_agent_native_scope_required", False)):
        return None
    # The ephemeral client marker and workspace are not authority on their
    # own.  Provider execution may hop to a worker thread where the
    # ``TurnContext`` was lost; in that case the native authority guard would
    # see no binding and (for historical compatibility) return an allowed
    # decision.  Require the exact server-issued binding that the client was
    # built with to still be present on the current turn before exposing any
    # provider-native tools.
    try:
        from ..services.turn_context import get_turn_context

        context_binding = getattr(get_turn_context(), "agent_execution_binding", None)
    except Exception:
        context_binding = None
    client_binding = getattr(client, "_agent_execution_binding", None)
    if context_binding is None or client_binding is None:
        return (False, False, False)
    if context_binding is not client_binding:
        return (False, False, False)
    workspace = getattr(client, "_agent_dialogue_workspace", None)
    if workspace is None:
        return (False, False, False)
    try:
        from ..services.agent_dialogue_workspace import AgentDialogueWorkspace

        if not isinstance(workspace, AgentDialogueWorkspace):
            return (False, False, False)
        workspace.assert_provider_entry()
    except Exception:
        return (False, False, False)
    return (
        True,
        bool(getattr(workspace, "readonly", False)),
        bool(getattr(workspace, "native_allowed", False)),
    )


def _agent_native_scope_proof(client: Any | None) -> Any | None:
    """Return the exact server-issued proof attached to one isolated client."""

    proof = getattr(client, "_agent_native_scope_proof", None) if client is not None else None
    if proof is None:
        return None
    try:
        proof.assert_lease()
        proof.assert_run(getattr(proof, "run_id", None))
    except Exception:
        return None
    return proof


def _formal_agent_work_context(client: Any | None) -> dict[str, str] | None:
    """Build the minimum opaque context a formal Agent MCP server must check."""

    proof = _agent_native_scope_proof(client)
    binding = getattr(client, "_agent_execution_binding", None) if client is not None else None
    if proof is None or binding is None:
        return None
    try:
        from ..services.agent_run_service import get_current_agent_run_id

        current_run_id = str(get_current_agent_run_id() or "").strip()
    except Exception:
        current_run_id = ""
    if current_run_id and current_run_id != str(proof.run_id):
        return None
    return {
        "run_id": str(proof.run_id),
        "work_item_id": str(proof.work_item_id),
        "session_id": str(proof.session_id),
        "agent_id": str(proof.agent_id),
        "agent_revision_id": str(proof.agent_revision_id),
        "binding_id": str(proof.binding_id),
        "binding_snapshot_hash": str(proof.binding_snapshot_hash),
        "capability_manifest_digest": str(proof.capability_manifest_digest),
        "authority_digest": str(proof.authority_digest),
        "lease_owner": str(proof.lease_owner),
        "lease_token": str(proof.lease_token),
    }


def _agent_tool_free_invocation_controls(
    backend: Any,
) -> dict | None:
    """Return a provider-attested text-only invocation, or refuse to start."""

    tool_free_args = getattr(backend, "tool_free_args", None)
    try:
        supports_disable = "disable_native_tools" in inspect.signature(
            backend.execute_prompt
        ).parameters
    except (AttributeError, TypeError, ValueError):
        supports_disable = False
    provider_attested = getattr(backend, "supports_tool_free_capability_gates", None) is True
    if supports_disable and provider_attested:
        return {
            "disable_native_tools": True,
            # Caller-supplied args can contain a native MCP/configuration
            # override.  Tool-free mode owns the complete argument surface.
            "extra_args": [],
        }
    if tool_free_args is not None and provider_attested:
        return {
            "extra_args": list(tool_free_args),
        }
    return None


def _agent_native_mcp_disable_args(
    backend: Any,
    *,
    allowed_mcp_servers: Iterable[str] = (),
) -> list[str] | None:
    """Return a provider gate that blocks every native MCP schema.

    A configured AoiTalk MCP list is not the complete provider MCP surface:
    resumed/native sessions may also see host-level servers.  Agent turns
    therefore require a provider-wide MCP deny rule before native execution.
    """

    allowed = {str(item).strip() for item in allowed_mcp_servers if str(item).strip()}
    if allowed:
        # Claude's strict ``--tools`` allowlist below is the provider-wide
        # boundary for the approved formal servers.  Adding ``mcp__*`` here
        # would deny those same tools. Providers without this explicit
        # allowlist contract are rejected by the caller.
        if not bool(getattr(backend, "supports_formal_agent_mcp", False)):
            return None
        return []
    gate = getattr(backend, "native_feature_gate_args", None)
    if not callable(gate):
        return None
    try:
        args = gate({"mcp"})
    except Exception:
        logger.error("native MCP gate resolution failed", exc_info=True)
        return None
    if args is None:
        return None
    return [str(item) for item in args]


def _remove_mcp_wildcard_deny(
    args: Iterable[Any],
    *,
    allowed_mcp_servers: Iterable[str] = (),
) -> list[str]:
    """Drop only MCP denies for formal servers already allowlisted.

    The ordinary feature gate runs before the request-scoped Agent context is
    available and can therefore emit a server-specific deny. Remove only the
    exact formal server patterns proven by the current receiver context;
    every other MCP/root deny remains in place.
    """

    values = [str(item) for item in (args or ())]
    if not values:
        return []
    removable = {"mcp__*"}
    removable.update(
        f"mcp__{str(name).strip()}__*"
        for name in allowed_mcp_servers
        if str(name).strip()
    )
    result: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value != "--disallowedTools":
            result.append(value)
            index += 1
            continue
        result.append(value)
        index += 1
        while index < len(values) and not values[index].startswith("--"):
            if values[index] not in removable:
                result.append(values[index])
            index += 1
    return result


def native_repository_capabilities(backend: Any) -> frozenset[str]:
    """Return the native capabilities that duplicate AoiTalk tools right now.

    Empty unless the backend declares native repository tools and a trusted
    repository scope is bound (the only case where both act on one root).
    """

    if not backend_has_native_repository_tools(backend):
        return frozenset()
    scope = _current_run_scope()
    if scope is None:
        return frozenset()
    resolver = getattr(backend, "native_repository_capabilities_for_scope", None)
    if not callable(resolver):
        return frozenset({NATIVE_READ})
    try:
        capabilities = resolver(scope)
    except Exception:
        logger.debug("native repository capability resolution failed", exc_info=True)
        return frozenset()
    return frozenset(
        str(item) for item in (capabilities or ()) if str(item) in _TOOLS_BY_CAPABILITY
    )


def cli_native_duplicate_tool_names(backend: Any) -> frozenset[str]:
    names: set[str] = set()
    for capability in native_repository_capabilities(backend):
        names |= _TOOLS_BY_CAPABILITY[capability]
    return frozenset(names)


def registry_without_names(registry: ToolRegistry, names: Iterable[str]) -> ToolRegistry:
    hidden = frozenset(names)
    if not hidden:
        return registry
    view = ToolRegistry()
    for tool in registry.get_all():
        if tool.name not in hidden:
            view.register(tool)
    return view


def bundled_aoitalk_mcp_server_key(server_config: Any) -> str | None:
    """Identify a bundled AoiTalk MCP server by its launch command, not name."""

    if not isinstance(server_config, Mapping):
        return None
    candidates: list[Any] = [server_config]
    for platform_key in ("windows", "linux"):
        value = server_config.get(platform_key)
        if isinstance(value, Mapping):
            candidates.append(value)
    for candidate in candidates:
        args = candidate.get("args")
        if not isinstance(args, (list, tuple)):
            continue
        values = [str(item).strip() for item in args]
        if _BUNDLED_MCP_ENTRY_MODULE not in values:
            continue
        index = values.index(_BUNDLED_MCP_ENTRY_MODULE)
        if index + 1 < len(values):
            return values[index + 1]
    return None


def mcp_servers_for_cli_backend(
    mcp_servers: Mapping[str, Any] | None,
    backend: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Drop bundled filesystem/shell MCP servers for native-tool CLIs.

    This filter is intentionally independent of ``AgentRunScope``.  The
    bundled ``os_operations`` MCP process is a generic filesystem/shell
    server without AoiTalk's managed-workspace ACL/conversion boundary, so it
    must not be handed to a native CLI even on an unscoped chat.  Unscoped
    chats keep the AoiTalk registry's managed-workspace bridge; this function
    only controls MCP servers passed into the provider process.  A bundled
    server is removed only when its launch command proves it is AoiTalk's own
    server; an external server with the same display key is retained.

    Returns ``(kept_servers, skipped_names)``.  External servers are never
    removed, even if their name happens to match a bundled key.
    """

    servers = dict(mcp_servers or {})
    if not servers or not backend_has_native_repository_tools(backend):
        return servers, []
    kept: dict[str, Any] = {}
    skipped: list[str] = []
    for name, config in servers.items():
        if bundled_aoitalk_mcp_server_key(config) in BUNDLED_MCP_NATIVE_DUPLICATE_SERVER_KEYS:
            skipped.append(str(name))
            continue
        kept[name] = config
    return kept, skipped


def _formal_agent_platform_settings(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    """Select one platform config without accepting wrapper/plugin fields."""

    if not isinstance(settings, Mapping):
        return None
    allowed_top = {
        "windows",
        "linux",
        "command",
        "args",
        "env",
        "formal_agent_work_context",
        "agent_work_context",
    }
    if any(str(key) not in allowed_top for key in settings):
        return None
    selected: dict[str, Any] = {
        key: value
        for key, value in settings.items()
        if key not in {"windows", "linux"}
    }
    platform_key = "windows" if platform.system() == "Windows" else "linux"
    platform_value = settings.get(platform_key)
    if not isinstance(platform_value, Mapping):
        return selected
    if any(str(key) not in _FORMAL_AGENT_SERVER_SETTING_KEYS for key in platform_value):
        return None
    selected.update(dict(platform_value))
    return selected


def _trusted_formal_agent_mcp_launch(
    name: Any,
    settings: Any,
) -> bool:
    """Accept only the canonical first-party Python module invocation.

    A marker or server display name is never sufficient. The actual command
    must be the repository/runtime Python launcher with exactly
    ``-m src.mcp_server_entry <canonical-key>`` and no shadowing Python env.
    """

    canonical_name = str(name or "")
    if canonical_name not in FORMAL_AGENT_MCP_SERVER_KEYS:
        return False
    selected = _formal_agent_platform_settings(settings)
    if selected is None:
        return False
    command = str(selected.get("command") or "").strip()
    args = selected.get("args")
    if not command or not isinstance(args, (list, tuple)):
        return False
    if [str(item) for item in args] != ["-m", _FORMAL_AGENT_ENTRY_MODULE, canonical_name]:
        return False
    env = selected.get("env")
    if env is not None:
        if not isinstance(env, Mapping):
            return False
        if any(str(key).upper() in _FORMAL_AGENT_ENV_SHADOW_KEYS for key in env):
            return False

    repo_root = Path(__file__).resolve().parents[2]
    trusted_python_paths = {
        os.path.normcase(os.path.normpath(str(Path(sys.executable).resolve()))),
        os.path.normcase(os.path.normpath(str(repo_root / "venv" / "Scripts" / "python.exe"))),
        os.path.normcase(os.path.normpath(str(repo_root / "venv" / "bin" / "python"))),
    }
    if os.path.isabs(command):
        try:
            command_key = os.path.normcase(os.path.normpath(str(Path(command).resolve())))
        except (OSError, RuntimeError, ValueError):
            return False
        return command_key in trusted_python_paths
    relative_command = os.path.normcase(os.path.normpath(command.replace("/", os.sep)))
    trusted_relative = {
        os.path.normcase(os.path.normpath(os.path.join("venv", "Scripts", "python.exe"))),
        os.path.normcase(os.path.normpath(os.path.join("venv", "bin", "python"))),
    }
    return relative_command in trusted_relative


def _formal_agent_mcp_server(
    name: Any,
    settings: Any,
    *,
    context: Mapping[str, str] | None,
) -> bool:
    """Check one configured server's formal AgentWorkContext declaration."""

    key = str(name or "").strip().casefold()
    if key not in FORMAL_AGENT_MCP_SERVER_KEYS or context is None:
        return False
    if not _trusted_formal_agent_mcp_launch(name, settings):
        return False
    # The declaration is retained as descriptive metadata only. The receiver
    # performs the actual durable lease/revision/authority checks per call.
    return True


def formal_agent_mcp_servers_for_cli_backend(
    mcp_servers: Mapping[str, Any] | None,
    backend: Any,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return only formal Agent MCP servers for an exact Claude Agent turn."""

    if not _agent_execution_active(client):
        return {}
    policy = _agent_dialogue_policy(client)
    if policy is None or not policy[0] or not policy[2]:
        return {}
    if not bool(getattr(backend, "supports_formal_agent_mcp", False)):
        return {}
    context = _formal_agent_work_context(client)
    if context is None:
        return {}
    result: dict[str, Any] = {}
    for name, settings in dict(mcp_servers or {}).items():
        if not _formal_agent_mcp_server(name, settings, context=context):
            continue
        if not isinstance(settings, Mapping):
            continue
        copied = dict(settings)
        copied["_aoitalk_formal_agent_context"] = dict(context)
        result[str(name)] = copied
    return result


def current_cli_mcp_servers(
    config: Any,
    backend: Any,
    fallback_servers=None,
    *,
    include_native_duplicates=False,
    client: Any | None = None,
) -> dict:
    """Project current shared gates onto configuration-owned MCP metadata."""
    from .tool_policy import _policy_config_get, _runtime_capability_for_tool

    formal_agent_context = bool(
        client is not None
        and bool(getattr(backend, "supports_formal_agent_mcp", False))
        and _formal_agent_work_context(client) is not None
    )
    if Features.is_enterprise() or (
        _runtime_capability_for_tool("call_mcp_tool", config=config) is not None
        and not formal_agent_context
    ):
        return {}
    configured = _policy_config_get(config, "mcp.servers", None)
    servers = configured if isinstance(configured, dict) else fallback_servers or {}
    if not isinstance(servers, dict):
        return {}
    if not include_native_duplicates:
        servers, _skipped = mcp_servers_for_cli_backend(servers, backend)
    if _agent_execution_active(client):
        servers = formal_agent_mcp_servers_for_cli_backend(
            servers,
            backend,
            client=client,
        )
    required = {
        "web_search": ("web_search",), "x_search": ("grok_x_search",),
        "media": ("media_assistant",), "os_operations": ("read_file",),
        "memory_knowledge": ("memory_search", "knowledge_search"),
    }
    platform_key = "windows" if platform.system() == "Windows" else "linux"

    def available(name, settings):
        if not isinstance(settings, dict):
            return False
        # Match MCPPlugin: platform settings override shared metadata. The
        # gate reads explicit markers, never an external name or command.
        metadata = dict(settings)
        actual = settings.get(platform_key)
        if isinstance(actual, dict):
            metadata.update(actual)
        return _runtime_capability_for_tool(
            f"mcp_{name}_server", config=config,
            tool_definition={"owner": f"mcp:{name}", "availability": metadata},
        ) is None

    return {
        name: settings for name, settings in servers.items()
        if available(name, settings)
        and all(_runtime_capability_for_tool(tool, config=config) is None
                for tool in required.get(bundled_aoitalk_mcp_server_key(settings), ()))
    }


def _ordinary_chat_mcp_integration_off(config: Any) -> bool:
    """MCP OFF in Personal chat controls AoiTalk's server injection only.

    Provider-owned MCP settings remain the owner's responsibility. Enterprise
    and issued Agent scopes retain the provider-wide exclusion contract.
    This exception never applies to a global/runtime tool prohibition.
    """
    from .tool_policy import (
        _policy_config_bool,
        _policy_config_get,
        _runtime_capability_for_tool,
        get_current_agent_dialogue_authority,
        get_current_agent_team_role,
        get_current_agent_tool_scope,
        get_current_agent_workspace_scope,
    )

    return bool(
        not Features.is_enterprise()
        and not _agent_execution_active()
        and _current_run_scope() is None
        and get_current_agent_team_role() is None
        and get_current_agent_tool_scope() is None
        and get_current_agent_dialogue_authority() is None
        and get_current_agent_workspace_scope() is None
        and not _policy_config_bool(
            _policy_config_get(config, "mcp_enabled", True), default=True,
        )
        and _runtime_capability_for_tool("calculate", config=config) is None
    )


def disabled_cli_native_features(config: Any) -> frozenset[str]:
    from .tool_policy import _runtime_capability_for_tool

    return frozenset(
        feature for feature, tool in (("filesystem", "read_file"), ("search", "web_search"), ("mcp", "call_mcp_tool"))
        if _runtime_capability_for_tool(tool, config=config) is not None
        and not (feature == "mcp" and _ordinary_chat_mcp_integration_off(config))
    ) | (frozenset({"mcp"}) if Features.is_enterprise() else frozenset())


def native_cli_feature_gate_args(backend: Any, config: Any, disabled_features=None, fallback_servers=None):
    from .tool_policy import _policy_config_get

    disabled = disabled_cli_native_features(config) if disabled_features is None else disabled_features
    gate = getattr(backend, "native_feature_gate_args", None)
    args = gate(disabled) if disabled and callable(gate) else ([] if not disabled else None)
    if args is None:
        return None
    configured = _policy_config_get(config, "mcp.servers", None)
    if (
        "mcp" not in disabled
        and isinstance(configured, dict)
        and not _ordinary_chat_mcp_integration_off(config)
    ):
        # Native duplicate suppression is a routing choice, not a disabled
        # capability. It must not cause an otherwise-enabled CLI to refuse.
        blocked = set(configured) - set(current_cli_mcp_servers(
            config, backend, fallback_servers, include_native_duplicates=True,
        ))
        if blocked:
            mcp_gate = getattr(backend, "native_mcp_gate_args", None)
            mcp_args = mcp_gate(blocked) if callable(mcp_gate) else None
            if mcp_args is None:
                return None
            # A repeated variadic option can replace earlier deny rules.
            if args and mcp_args and args[0] == mcp_args[0] == "--disallowedTools":
                args = ["--disallowedTools", *sorted(set(args[1:]) | set(mcp_args[1:]))]
            else:
                args = [*args, *mcp_args]
    return args


def native_cli_invocation_controls(
    backend: Any,
    config: Any,
    native_args,
    extra_args=None,
    *,
    client: Any | None = None,
) -> dict | None:
    """Return enforced controls, or refuse a backend with no safe fallback."""
    from .tool_policy import _runtime_capability_for_tool

    if _agent_execution_active(client):
        dialogue_policy = _agent_dialogue_policy(client)
        if dialogue_policy is not None:
            workspace_bound, readonly, native_allowed = dialogue_policy
            if not workspace_bound:
                # A real ephemeral Agent client must have an active session
                # workspace before any provider process starts.  The
                # provider may still use its attested tool-free lane, but it
                # must never receive a native allowlist without that proof.
                return _agent_tool_free_invocation_controls(backend)
            if not native_allowed:
                # Framework-tool turns use the common AoiTalk JSON loop.  Do
                # not allow the provider's ambient filesystem/shell/MCP
                # surface even when an AgentRunScope happens to be present.
                return _agent_tool_free_invocation_controls(backend)
        # A Chat Agent must never enter a provider process with its native
        # filesystem/shell/apply-patch surface merely because the human is
        # allowed to use that project.  Without an already-issued finite
        # AgentRunScope, keep the conversation lane available only when the
        # provider can prove a tool-free invocation.  Codex deliberately
        # advertises ``supports_tool_free_capability_gates=False`` and is
        # denied here before its subprocess starts.
        scope = _current_run_scope()
        if scope is None:
            return _agent_tool_free_invocation_controls(
                backend,
            )

        # An issued repository scope is still subject to the current Agent
        # authority.  Native read/edit/shell calls bypass RegistryToolRouter,
        # so map the finite capability set back through the same per-call
        # AgentAuthorityResolver intersection before starting the provider.
        native_capabilities = native_repository_capabilities(backend)
        if dialogue_policy is not None and readonly:
            # A readonly Dialogue may use provider-native read only when the
            # issued scope and provider attest that capability.  Edits/shell
            # remain disabled at this boundary.
            native_capabilities &= frozenset({NATIVE_READ})
        if native_capabilities:
            try:
                from ..services.agent_authority_guard import (
                    guard_agent_native_capabilities_sync,
                )

                decision = guard_agent_native_capabilities_sync(
                    {
                        {
                            NATIVE_READ: "workspace_read",
                            NATIVE_EDIT: "workspace_write",
                            NATIVE_SHELL: "command_execute",
                        }[capability]
                        for capability in native_capabilities
                    },
                    config=config,
                )
            except Exception:
                logger.error("Agent native authority resolution failed", exc_info=True)
                decision = None
            if decision is None or not decision.allowed:
                return _agent_tool_free_invocation_controls(
                    backend,
                )

        # MCP is a provider-owned side effect surface.  The configured
        # AoiTalk server list is not an exhaustive host/native-session list,
        # so a finite Agent scope cannot permit it unless the provider can
        # attest a provider-wide deny rule for this invocation.
        formal_mcp_servers: dict[str, Any] = {}
        if dialogue_policy is not None:
            formal_mcp_servers = current_cli_mcp_servers(
                config,
                backend,
                getattr(client, "_mcp_servers", {}),
                include_native_duplicates=True,
                client=client,
            )
        mcp_disable_args = _agent_native_mcp_disable_args(
            backend,
            allowed_mcp_servers=formal_mcp_servers,
        )
        if mcp_disable_args is None:
            return _agent_tool_free_invocation_controls(
                backend,
            )
        allowlist_builder = getattr(backend, "native_agent_tool_allowlist_args", None)
        if not callable(allowlist_builder):
            return _agent_tool_free_invocation_controls(backend)
        try:
            try:
                allowlist_args = allowlist_builder(
                    native_capabilities,
                    tuple(sorted(formal_mcp_servers)),
                )
            except TypeError:
                # Compatibility for narrow third-party test doubles. The
                # production Claude backend implements the two-argument form.
                allowlist_args = allowlist_builder(native_capabilities)
        except Exception:
            logger.error("Agent native tool allowlist resolution failed", exc_info=True)
            return _agent_tool_free_invocation_controls(backend)
        if allowlist_args is None:
            return _agent_tool_free_invocation_controls(backend)
        formal_mcp_args: list[str] = []
        if formal_mcp_servers:
            mcp_builder = getattr(backend, "get_mcp_args", None)
            if not callable(mcp_builder):
                return _agent_tool_free_invocation_controls(backend)
            try:
                formal_mcp_args = [
                    str(item) for item in (mcp_builder(formal_mcp_servers) or [])
                ]
            except Exception:
                logger.error("formal Agent MCP argument resolution failed", exc_info=True)
                return _agent_tool_free_invocation_controls(backend)
        native_prefix = (
            _remove_mcp_wildcard_deny(
                native_args,
                allowed_mcp_servers=formal_mcp_servers,
            )
            if formal_mcp_servers
            else list(native_args or ())
        )
        return {
            "extra_args": [
                *native_prefix,
                *formal_mcp_args,
                *allowlist_args,
                *mcp_disable_args,
            ],
        }

    if _runtime_capability_for_tool("calculate", config=config) is not None or native_args is None:
        return _agent_tool_free_invocation_controls(backend)
    return {"extra_args": [*(extra_args or []), *native_args]} if native_args else {}


def native_cli_capability_gate_error(
    backend: Any,
    config: Any,
    *,
    client: Any | None = None,
) -> str:
    """Explain the rejected settings and a usable next step to the user."""
    from .tool_policy import _policy_config_get, _runtime_capability_for_tool

    name_getter = getattr(backend, "get_provider_name", None)
    provider = name_getter() if callable(name_getter) else type(backend).__name__
    if _agent_execution_active(client):
        return (
            f"{provider}のAgent実行を停止しました。"
            "現在のAgentRevisionに対応する有限のAgentRunScopeと、"
            "provider側で検証可能なnative tool/MCP制御が必要です。"
        )
    if _runtime_capability_for_tool("calculate", config=config) is not None:
        reasons = ["ツール全体OFF"]
    else:
        labels = {"filesystem": "Filesystem OFF", "search": "Search OFF", "mcp": "MCP全体OFF"}
        disabled = disabled_cli_native_features(config)
        reasons = [label for feature, label in labels.items() if feature in disabled]
        if "mcp" in disabled and Features.is_enterprise():
            reasons.append("EnterpriseのMCP制限")
        configured = _policy_config_get(config, "mcp.servers", None)
        if (
            "mcp" not in disabled
            and isinstance(configured, dict)
            and not _ordinary_chat_mcp_integration_off(config)
        ):
            blocked = set(configured) - set(current_cli_mcp_servers(config, backend, include_native_duplicates=True))
            if blocked:
                reasons.append("MCP server除外: " + ", ".join(sorted(blocked)))
    reason = "、".join(reasons) or "現在のツール制限"
    return (
        f"{provider}では無効設定（{reason}）に対応するツール除外を保証できないため、実行を停止しました。"
        "この設定を維持して続けるには、機能別のツール除外に対応するClaude Codeなどの別providerを選択してください。"
    )

