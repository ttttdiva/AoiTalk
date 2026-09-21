"""Server-controlled runtime tools for agent-generated managed scripts."""

from __future__ import annotations

import inspect
from typing import Any, Mapping

from ..memory.database import get_database_manager
from ..services.managed_tool_service import (
    ManagedToolPromotionPolicy,
    ManagedToolService,
)
from ..services.project_context import (
    get_runtime_project_context,
    runtime_project_context_is_bound,
)
from ..services.turn_context import get_turn_context
from .core import ToolDefinition, ToolParam


def _effective_context(context: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Resolve request-local identity, never a stale registry closure.

    Provider registries are reused across turns.  Once a runtime context or
    TurnContext is explicitly bound, an empty binding must fail closed instead
    of falling back to the context captured while building the registry.
    """
    turn = get_turn_context()
    turn_projection = {
        key: getattr(turn, key)
        for key in ("user_id", "project_id", "session_id")
        if getattr(turn, key, None) is not None
    }
    runtime = get_runtime_project_context()
    if isinstance(runtime, dict):
        effective = dict(runtime)
        # The request boundary is the principal authority.  Fill omitted
        # identity fields from it, but fail closed on a mismatch rather than
        # letting a stale provider closure select another user/Project.
        bound_user = str(getattr(turn, "user_id", "") or "").strip() or None
        runtime_user = str(effective.get("user_id") or "").strip() or None
        if bound_user and runtime_user and bound_user != runtime_user:
            raise PermissionError("managed-tool runtime user does not match TurnContext")
        if bound_user and not runtime_user:
            effective["user_id"] = bound_user
        bound_project = str(getattr(turn, "project_id", "") or "").strip() or None
        runtime_project = str(
            effective.get("project_id") or effective.get("id") or ""
        ).strip() or None
        if bound_project and runtime_project and bound_project != runtime_project:
            raise PermissionError("managed-tool runtime Project does not match TurnContext")
        if bound_project and not runtime_project:
            effective["project_id"] = bound_project
        return effective
    if runtime_project_context_is_bound():
        # An explicit runtime binding of ``None`` means Project/App context
        # is off, not that authenticated user identity disappeared.  Project
        # and user fields from TurnContext remain trusted; constructor context
        # is still ignored.
        return turn_projection
    if turn_projection or getattr(turn, "include_project_context", None) is not None:
        return turn_projection
    # Registries outlive the turn that constructed them.  A captured mapping
    # must never restore stale principal authority when no request-local
    # TurnContext/runtime binding exists.
    return {}


def _context_value(context: Mapping[str, Any] | None, key: str) -> Any:
    return _effective_context(context).get(key)


def _user_id(context: Mapping[str, Any] | None) -> str:
    value = _context_value(context, "user_id")
    if not value:
        raise PermissionError("authenticated user_id is required")
    return str(value)


def _project_id(context: Mapping[str, Any] | None, requested: str | None = None) -> str | None:
    effective = _effective_context(context)
    trusted = effective.get("project_id") or effective.get("id")
    requested_value = str(requested or "").strip() or None
    if requested_value and trusted and requested_value != str(trusted):
        raise PermissionError("project_id must match the server-selected Project context")
    if requested_value and not trusted:
        raise PermissionError("model-supplied project_id cannot establish Project authority")
    return str(trusted) if trusted else None


def _service_policy(config: Any | None) -> ManagedToolPromotionPolicy | None:
    if config is None:
        return None
    return ManagedToolPromotionPolicy.from_config(config)


def build_managed_tool_definitions(
    context: Mapping[str, Any] | None = None,
    *,
    workspace_root: str | None = None,
    policy: ManagedToolPromotionPolicy | Mapping[str, Any] | None = None,
    config: Any | None = None,
    service: ManagedToolService | None = None,
) -> list[ToolDefinition]:
    """Build the four dedicated managed-tool tools for one runtime context.

    No path/capability argument is accepted as an authority input.  The
    service receives only authenticated identity and the server-selected
    Project context from ``TurnContext``/the closure.
    """

    runtime_context = dict(context or {})
    managed_service = service or ManagedToolService(
        workspace_root=workspace_root,
        policy=policy if policy is not None else _service_policy(config),
    )

    async def _with_session(callback):
        session = await get_database_manager().get_session()
        try:
            result = await callback(session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    # ``_with_session`` commits and returns ORM rows; normalize at the tool
    # boundary so provider backends receive only JSON-compatible payloads.
    async def _create_result(awaitable):
        lineage = await awaitable
        return lineage.to_dict()

    async def create_managed_tool(
        name: str,
        content: str,
        runtime: str = "python",
        description: str = "",
        semantic_key: str = "",
    ) -> dict[str, Any]:
        owner = _user_id(runtime_context)
        trusted_project = _project_id(runtime_context)
        # AgentRun identity is server-issued via ContextVar; never accept a
        # model-supplied UUID that could manufacture independent evidence.
        from ..services.agent_run_service import get_current_agent_run_id

        current_run_id = get_current_agent_run_id()
        return await _create_result(
            _with_session(
                lambda session: managed_service.create_tool(
                    session,
                    user_id=owner,
                    name=name,
                    content=content,
                    runtime=runtime,
                    description=description,
                    semantic_key=semantic_key,
                    project_id=trusted_project,
                    agent_run_id=current_run_id,
                )
            )
        )

    async def update_managed_tool(
        lineage_id: str,
        content: str,
        runtime: str = "",
        description: str = "",
        semantic_key: str = "",
    ) -> dict[str, Any]:
        owner = _user_id(runtime_context)
        trusted_project = _project_id(runtime_context)
        from ..services.agent_run_service import get_current_agent_run_id

        current_run_id = get_current_agent_run_id()
        result = await _with_session(
            lambda session: managed_service.update_tool(
                session,
                lineage_id,
                user_id=owner,
                content=content,
                context_project_id=trusted_project,
                runtime=runtime or None,
                description=description if description else None,
                semantic_key=semantic_key,
                agent_run_id=current_run_id,
            )
        )
        return result.to_dict()

    async def list_managed_tools(include_evidence: bool = False) -> list[dict[str, Any]]:
        owner = _user_id(runtime_context)
        trusted_project = _project_id(runtime_context)
        return await _with_session(
            lambda session: managed_service.list_tools(
                session,
                user_id=owner,
                project_id=trusted_project,
                include_evidence=bool(include_evidence),
            )
        )

    async def execute_managed_tool(
        lineage_id: str,
        input_json: dict[str, Any] | None = None,
        semantic_key: str = "",
    ) -> dict[str, Any]:
        owner = _user_id(runtime_context)
        trusted_project = _project_id(runtime_context)
        from ..services.agent_run_service import get_current_agent_run_id

        current_run_id = get_current_agent_run_id()
        return await _with_session(
            lambda session: managed_service.execute_tool(
                session,
                lineage_id,
                user_id=owner,
                context_project_id=trusted_project,
                input_json=input_json,
                semantic_key=semantic_key,
                agent_run_id=current_run_id,
            )
        )

    def _tool(name: str, description: str, fn: Any, params: list[ToolParam], **kwargs: Any) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            function=fn,
            parameters=params,
            is_async=inspect.iscoroutinefunction(fn),
            owner="managed_tools",
            **kwargs,
        )

    return [
        _tool(
            "create_managed_tool",
            "Create runnable code or a script for the current task in a server-managed user workspace. Use this instead of create_file for task automation that may be reused; successful independent runs can promote it to an App automatically.",
            create_managed_tool,
            [
                ToolParam("name", "string"),
                ToolParam("content", "string"),
                ToolParam("runtime", "string", required=False, default="python"),
                ToolParam("description", "string", required=False, default=""),
                ToolParam("semantic_key", "string", required=False, default=""),
            ],
            risk="medium",
            side_effect="filesystem,database",
            supports_parallel=False,
        ),
        _tool(
            "update_managed_tool",
            "Update one managed-tool lineage by content and record a new SHA revision.",
            update_managed_tool,
            [
                ToolParam("lineage_id", "string"),
                ToolParam("content", "string"),
                ToolParam("runtime", "string", required=False, default=""),
                ToolParam("description", "string", required=False, default=""),
                ToolParam("semantic_key", "string", required=False, default=""),
            ],
            risk="medium",
            side_effect="filesystem,database",
            supports_parallel=False,
        ),
        _tool(
            "list_managed_tools",
            "List the authenticated user's managed tools and promotion discovery evidence.",
            list_managed_tools,
            [ToolParam("include_evidence", "boolean", required=False, default=False)],
        ),
        _tool(
            "execute_managed_tool",
            "Execute one managed script, record this AgentRun's success evidence, and automatically reuse or promote the same lineage when it proves reusable.",
            execute_managed_tool,
            [
                ToolParam("lineage_id", "string"),
                ToolParam("input_json", "object", required=False, default={}),
                ToolParam("semantic_key", "string", required=False, default=""),
            ],
            risk="medium",
            side_effect="process,database",
            supports_parallel=False,
        ),
    ]


# Compatibility names used by runtime registry integrations.
build_managed_script_tool_definitions = build_managed_tool_definitions
build_managed_tools = build_managed_tool_definitions


__all__ = [
    "build_managed_tool_definitions",
    "build_managed_script_tool_definitions",
    "build_managed_tools",
]
