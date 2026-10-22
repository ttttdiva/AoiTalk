"""Run-owned read-only Home MCP connection over the existing MCPPlugin.

No inference loop, shell permission, global MCP connection or runtime lease is
created here. Trusted AgentWork code supplies the identity; the model can only
choose bounded arguments for four fixed filesystem operations.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
from dataclasses import replace
import math
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from ..tools.external.mcp_plugin import MCPPlugin
from .turn_context import get_turn_context

READ_ONLY_TOOLS = frozenset({"list_directory", "path_info", "read_text", "search_text"})
_IDENT = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


def _setting(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    marker = object()
    value = config.get(key, marker)
    if value is not marker:
        return value
    value = config
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return default
        value = value[part]
    return value


def _bounded_arguments(tool: str, arguments: Any) -> str:
    if tool not in READ_ONLY_TOOLS or not isinstance(arguments, Mapping):
        raise PermissionError("Local MCP tool denied")
    allowed = {
        "list_directory": {"path", "max_results"},
        "path_info": {"path"},
        "read_text": {"path", "max_chars"},
        "search_text": {"root", "query", "file_glob", "max_results", "max_files", "timeout_sec"},
    }[tool]
    required = {"root", "query"} if tool == "search_text" else {"path"}
    if set(arguments) - allowed or not required <= set(arguments):
        raise PermissionError("Local MCP arguments denied")
    path = arguments.get("root" if tool == "search_text" else "path")
    if not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\x00" in path:
        raise PermissionError("Local MCP path denied")
    ranges = {"max_results": (1, 100 if tool == "search_text" else 500),
              "max_chars": (1, 24000), "max_files": (1, 500)}
    for key, (low, high) in ranges.items():
        if key in arguments:
            value = arguments[key]
            if type(value) is not int or not low <= value <= high:
                raise PermissionError("Local MCP bounds denied")
    if tool == "search_text":
        query = arguments["query"]
        pattern = arguments.get("file_glob", "*")
        duration = arguments.get("timeout_sec", 5)
        if (not isinstance(query, str) or not 1 <= len(query) <= 1000
                or not isinstance(pattern, str) or not 1 <= len(pattern) <= 128
                or any(char in pattern for char in "/\\:")
                or type(duration) not in (int, float) or not math.isfinite(duration)
                or not 0 < duration <= 10):
            raise PermissionError("Local MCP bounds denied")
    return path


def _checked_path(workspace: Path, raw: str, scope: Any) -> None:
    # The existing run scope is the first authority boundary; the lexical
    # checks also reject links/Win32 aliases before a child receives a path.
    supplied = Path(raw)
    if raw.startswith(("\\\\", "//")) or ".." in supplied.parts:
        raise PermissionError("Local MCP path denied")
    candidate = supplied if supplied.is_absolute() else workspace / supplied
    try:
        relative = candidate.relative_to(workspace)
    except ValueError:
        raise PermissionError("Local MCP path denied") from None
    from ..security.sensitive_files import is_sensitive_relative_path
    if (is_sensitive_relative_path(str(relative))
            or any(part.casefold() == ".git" or ":" in part or part.endswith((".", " ")) for part in relative.parts)):
        raise PermissionError("Local MCP path denied")
    for part in (*reversed(candidate.parents), candidate):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError("Local MCP link denied")
    actual = Path(scope.assert_read_allowed(str(candidate))).resolve(strict=True)
    actual.relative_to(workspace)
    info = actual.stat()
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise PermissionError("Local MCP path denied")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise PermissionError("Local MCP hardlink denied")


class _BoundRun:
    def __init__(self, *, workspace: Path, owner_id: str, agent_run_id: str, config: Any):
        from ..security.agent_run_scope import AgentRunScope
        context = get_turn_context()
        self.workspace = workspace
        self.owner_id = owner_id
        self.agent_run_id = agent_run_id
        self.agent_id = str(context.agent_id or "")
        self.revision_id = str(context.agent_revision_id or "")
        self.session_id = str(context.session_id or "")
        self.project_id = str(context.project_id or "")
        self.config = config
        self.closed = False
        self.scope = AgentRunScope.for_repository(str(workspace), run_id=agent_run_id, workspace_access_level="read")
        self.assert_identity()

    def assert_identity(self) -> None:
        from .agent_run_service import get_current_agent_run_id
        context = get_turn_context()
        if (self.closed or not self.agent_id or not self.revision_id or not self.session_id
                or context.agent_execution_binding is None
                or str(context.user_id or "") != self.owner_id
                or str(context.agent_id or "") != self.agent_id
                or str(context.agent_revision_id or "") != self.revision_id
                or str(context.session_id or "") != self.session_id
                or str(context.project_id or "") != self.project_id
                or str(get_current_agent_run_id() or "") != self.agent_run_id):
            raise PermissionError("Local MCP run identity denied")

    async def guard(self, tool: str, arguments: Any) -> bool:
        self.assert_identity()
        if _setting(self.config, "local_agent_tasks.mcp.enabled", False) is not True:
            raise PermissionError("Local MCP disabled")
        path = _bounded_arguments(tool, arguments)
        _checked_path(self.workspace, path, self.scope)
        from .agent_authority_guard import guard_agent_tool_call_async
        # Canonical capability checked live. This fixed read-only lane cannot
        # inherit generic MCP/shell permissions or grant itself new scope.
        decision = await guard_agent_tool_call_async("workspace_read", {}, config=self.config)
        if not decision.allowed:
            raise PermissionError("Local MCP authority revoked")
        return True


@asynccontextmanager
async def scoped_local_mcp_tools(*, config: Any, workspace: str, owner_id: str, agent_run_id: str):
    """Yield read-only definitions whose transport stays on this owner loop.

    Consume them from the existing synchronous run_inline worker. Definitions
    are invalid as soon as the context ends; cancellation closes only this
    run's MCP transport through MCPClient/AsyncResourceScope.
    """
    settings = _setting(config, "local_agent_tasks.mcp", {})
    if not isinstance(settings, Mapping):
        raise ValueError("Invalid local MCP configuration")
    if settings.get("enabled", False) is not True:
        yield []
        return
    if not _IDENT.fullmatch(owner_id) or not _IDENT.fullmatch(agent_run_id):
        raise ValueError("Invalid local MCP run identity")
    command, script, cwd = (Path(str(settings.get(key) or "")) for key in ("command", "script", "cwd"))
    if not all(path.is_absolute() for path in (command, script, cwd)):
        raise ValueError("Local MCP command, script and cwd must be absolute")
    if not command.is_file() or not script.is_file() or not cwd.is_dir():
        raise ValueError("Local MCP configured runtime is unavailable")
    # Never mix the two applications' Python modules or use a tunnel endpoint.
    if script.name != "gateway_stdio.py" or script.parent.parent.resolve() != cwd.resolve():
        raise ValueError("Local MCP must use the configured gateway_stdio.py")
    root = Path(workspace)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("Invalid local MCP workspace")
    for ancestor in (*reversed(root.parents), root):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Local MCP workspace contains a link")
    root = root.resolve(strict=True)
    binding = _BoundRun(workspace=root, owner_id=owner_id, agent_run_id=agent_run_id, config=config)
    owner_loop = asyncio.get_running_loop()
    plugin = MCPPlugin()
    plugin.configure_runtime(lambda: config)
    server_name = "localagent"
    env = {"HOME_MCP_PROFILE": "local_agent_read_only", "HOME_MCP_WORKSPACE": str(root),
           "HOME_MCP_AGENT_RUN_ID": agent_run_id, "HOME_MCP_OWNER_ID": owner_id,
           "HOME_MCP_AOITALK_ENABLED": "0"}
    try:
        if not await plugin.initialize({}):
            raise RuntimeError("Local MCP SDK unavailable")
        plugin.client.set_tool_call_guard(server_name, binding.guard)
        if not await plugin.add_server(server_name, str(command), [str(script)], env, cwd=str(cwd), isolated=True):
            raise RuntimeError("Local MCP connection failed")
        # The exception is limited to this launcher's immutable four-tool
        # profile. General MCP/browser/Jev tools retain external classification.
        plugin.client.set_server_metadata(server_name, {"egress_scope": "local", "endpoint": "stdio://localhost/home-mcp/read-only"})
        definitions = await plugin.list_tools()
        targets = plugin._native_tool_targets
        exposed = {targets[item.name][1] for item in definitions}
        if exposed != READ_ONLY_TOOLS:
            raise RuntimeError("Local MCP tool profile mismatch")
        tools = []
        for definition in definitions:
            original = definition.function
            def invoke(_original=original, **arguments):
                if binding.closed or owner_loop.is_closed() or not owner_loop.is_running():
                    raise PermissionError("Local MCP run closed")
                try:
                    if asyncio.get_running_loop() is owner_loop:
                        raise RuntimeError("Local MCP synchronous tools require the run worker")
                except RuntimeError as exc:
                    if str(exc) != "no running event loop":
                        raise
                # run_coroutine_threadsafe captures the caller's current
                # ContextVars, including TurnContext/AgentRun, on each call.
                future = asyncio.run_coroutine_threadsafe(_original(**arguments), owner_loop)
                try:
                    return future.result(timeout=35)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise TimeoutError("Local MCP call timed out") from None
            tools.append(replace(definition, function=invoke, is_async=False,
                                 owner="workspace_read", side_effect="none", risk="low",
                                 supports_parallel=False, requires_approval=False))
        yield tools
    finally:
        binding.closed = True
        await plugin.cleanup()
