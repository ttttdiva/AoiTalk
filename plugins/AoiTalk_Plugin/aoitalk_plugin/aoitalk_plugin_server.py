"""MCP surface for the AoiTalk plugin; deliberately independent of server.py."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import Sequence
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import aoitalk, aoitalk_auth, aoitalk_agent_tasks, aoitalk_cli_sessions, aoitalk_manage, aoitalk_tasks
from .aoitalk_task_schema import structured_task_tool
from .aoitalk_agent_task_schema import structured_agent_task_tool
from .aoitalk_cli_session_schema import structured_cli_session_tool
from .telemetry import ToolTelemetryMiddleware, instrument_tool
from .runtime import prepare_runtime


@asynccontextmanager
async def lifespan(server):
    await aoitalk_auth.activate()
    try:
        yield {}
    finally:
        await aoitalk_auth.deactivate()


def build_mcp_server(*, auth_server_provider: Any = None, token_verifier: Any = None,
                     auth: Any = None, extensions: Sequence[Any] = (),
                     middleware: Sequence[Any] = ()) -> MCPServer:
    """Build an AoiTalk server with the established catalog and wrappers.

    The canonical module-level server calls this with no auth or extensions,
    preserving the stdio/Tunnel surface. Optional HTTP compositions call the
    same builder with their explicitly injected verifier and extension so a
    second server can be constructed without mutating the canonical one.
    """
    server = MCPServer(
        "AoiTalk Plugin",
        title="AoiTalk management and Agent tools",
        description="Dedicated MCP tools for AoiTalk clips, tasks, Docs, Knowledge, API Agents and CLI sessions.",
        instructions=(
            "Use AoiTalk-specific tools for AoiTalk records. Task mutations require a caller-held operation_id. "
            "After an uncertain write, inspect its status and retry only with the same identifier and identical arguments."
        ),
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
        auth=auth,
        extensions=extensions,
        lifespan=lifespan,
        middleware=[ToolTelemetryMiddleware(), *middleware],
    )

    # The existing 21 tools keep their native functions, wire schemas and wrappers.
    for function in aoitalk.TOOLS:
        writes_docs = function is aoitalk.aoitalk_clip_ingest
        _register_on(server, function, read_only=not writes_docs, destructive=writes_docs)

    for function in aoitalk_tasks.TOOLS:
        read_only = function in aoitalk_tasks.READ_ONLY_TOOLS
        _register_on(server, function, read_only=read_only,
                     destructive=function is aoitalk_tasks.aoitalk_local_task_start,
                     open_world=not read_only,
                     wrapper=structured_task_tool)

    for function in aoitalk_agent_tasks.TOOLS:
        read_only = function in aoitalk_agent_tasks.READ_ONLY_TOOLS
        _register_on(server, function, read_only=read_only, destructive=not read_only,
                     open_world=not read_only,
                     wrapper=structured_agent_task_tool)

    for function in aoitalk_cli_sessions.TOOLS:
        read_only = function in aoitalk_cli_sessions.READ_ONLY_TOOLS
        # reconnect is a real read-only observation and retains its established annotation.
        _register_on(server, function, read_only=read_only, destructive=not read_only,
                     open_world=not read_only,
                     wrapper=structured_cli_session_tool)

    for function in aoitalk_manage.TOOLS:
        read_only = function in aoitalk_manage.READ_ONLY_TOOLS
        is_prepare = function is aoitalk_manage.aoitalk_docs_prepare_edit
        _register_on(server, function, read_only=read_only and not is_prepare,
                     destructive=function is aoitalk_manage.aoitalk_docs_mutate
                     or function in {aoitalk_manage.aoitalk_task_create,
                                     aoitalk_manage.aoitalk_task_update,
                                     aoitalk_manage.aoitalk_task_delete,
                                     aoitalk_manage.aoitalk_task_restore},
                     idempotent=True)
    return server


def _register_on(server: MCPServer, function: Any, *, read_only: bool, destructive: bool = False,
                 idempotent: bool = True, open_world: bool = True, wrapper=None) -> None:
    target = instrument_tool(function)
    if wrapper is not None:
        target = wrapper(target)
    server.tool(annotations=ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    ))(target)


mcp = build_mcp_server()


def main() -> None:
    # Defense in depth for direct console/module entry as well as stdio entry.
    prepare_runtime()
    from .dots_dialogue_dedicated import (
        DedicatedDotsConfigurationError,
        load_dedicated_dots_connection_config,
    )

    try:
        dedicated_config = load_dedicated_dots_connection_config()
    except DedicatedDotsConfigurationError:
        # Do not print configuration material. The existing catalog server
        # remains available, but the DOTS Events extension stays absent.
        logging.getLogger(__name__).error(
            "Dedicated DOTS connection configuration is unavailable; Events remain disabled"
        )
        mcp.run()
        return
    if dedicated_config is None:
        mcp.run()
        return
    try:
        from .dots_dialogue_dedicated_stdio import build_dedicated_stdio_dots_composition

        composition = build_dedicated_stdio_dots_composition(dedicated_config)
    except Exception:
        logging.getLogger(__name__).error(
            "Dedicated DOTS Events adapter could not be constructed; Events remain disabled"
        )
        mcp.run()
        return
    composition.server.run()


if __name__ == "__main__":
    main()
