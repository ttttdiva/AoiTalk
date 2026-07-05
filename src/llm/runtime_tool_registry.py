"""Runtime tool registry helpers for LLM clients."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..services.project_context import get_runtime_project_context
from ..skills.executor import invoke_skill
from ..tools.app_factory import create_instant_app_package, set_app_factory_tool_config
from ..tools.core import ToolDefinition, ensure_tool_definitions, tool as tool_decorator
from ..tools.registry import ToolRegistry
from ..services.advanced_reasoning_service import advanced_reasoning_enabled
from ..services.agent_run_service import AgentRunService, get_current_agent_run_id
from ..services.agent_team_service import (
    AGENT_TEAM_MEMBER_KEYS,
    SCALABLE_MEMBER_KEYS,
    agent_team_clamp_instances,
    agent_team_delegate_member,
    agent_team_enabled,
    agent_team_member_for,
    agent_team_scalable_members,
)
from .tool_policy import (
    FILESYSTEM_MUTATION_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    check_tool_call_allowed,
    format_blocked_tool_result,
    get_current_user_input,
    is_memory_search_enabled,
)
from .specialist_delegate import (
    AgentTeamRoleDelegationRunner,
    MediaDelegationRunner,
    ScenarioDelegationRunner,
    SpotifyDelegationRunner,
    UtilityDelegationRunner,
    WritingDelegationRunner,
    ImportDelegationRunner,
)


def _agent_enabled(config: Any, domain_key: str, default: bool = True) -> bool:
    if not config:
        return default
    if domain_key in AGENT_TEAM_MEMBER_KEYS:
        return agent_team_member_for(config, domain_key) is not None
    return bool(config.get("agents", {}).get(domain_key, {}).get("enabled", default))


def _app_factory_enabled(config: Any, default: bool = True) -> bool:
    if not config:
        return default
    if isinstance(config, dict):
        return bool((config.get("app_factory") or {}).get("enabled", default))
    getter = getattr(config, "get", None)
    if callable(getter):
        return bool(getter("app_factory.enabled", default))
    return default


def _project_management_direct_tools_enabled(config: Any, default: bool = True) -> bool:
    if not config:
        return default
    agents = config.get("agents", {}) if hasattr(config, "get") else {}
    project_config = agents.get("project_management", {}) if isinstance(agents, dict) else {}
    if isinstance(project_config, dict) and "direct_tools_enabled" in project_config:
        return bool(project_config.get("direct_tools_enabled"))
    return default


def _configured_model(config: Any, default: str = "gpt-4o-mini") -> str:
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


def _search_x_enabled(config: Any) -> bool:
    search_config = _config_get(config, "search", {}) or {}
    if not isinstance(search_config, dict):
        return False
    return bool(
        search_config.get(
            "x_enabled",
            search_config.get("grok_x_enabled", False),
        )
    )


def _search_knowledge_enabled(config: Any) -> bool:
    search_config = _config_get(config, "search", {}) or {}
    if not isinstance(search_config, dict):
        return False
    return bool(search_config.get("knowledge_enabled", False))


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

    from ..tools.basic.web_search import web_search_with_config

    @tool_decorator
    def web_search(query: str) -> str:
        """Search the public web for fresh or time-sensitive information."""
        return web_search_with_config(query, config=config)

    registered = _register_tool_definition(
        registry,
        web_search,
        owner="search",
        risk="medium",
        supports_parallel=False,
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

    if _search_knowledge_enabled(config):
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

    if is_memory_search_enabled(config):
        from ..tools.memory import search_memory

        registered = (
            _register_tool_definition(
                registry,
                search_memory,
                owner="search",
                risk="low",
                supports_parallel=False,
            )
            or registered
        )

    return registered


def _register_filesystem_direct_tools(registry: ToolRegistry) -> bool:
    """Expose file, workspace, repository, and command tools at root level."""

    from ..tools.file_explorer.file_explorer_tools import (
        create_workspace_directory,
        delete_workspace_item,
        find_workspace_items,
        get_workspace_file_info,
        inspect_workspace_tree,
        list_workspace_files,
        move_workspace_item,
        read_workspace_file,
        upload_workspace_file,
    )
    from ..tools.os_operations import (
        append_to_file,
        create_file,
        delete_file,
        edit_file,
        execute_command,
        insert_to_file,
        list_directory,
        search_files,
        undo_edit,
        view_file,
    )
    from ..tools.repo_map.tools import get_repo_map
    from ..tools.workspaces.file_tools import (
        delete_user_file,
        download_user_file,
        get_user_file_info,
        list_user_files,
        upload_user_file,
    )

    registered = False
    for tool_def in ensure_tool_definitions(
        [
            list_workspace_files,
            find_workspace_items,
            inspect_workspace_tree,
            create_workspace_directory,
            upload_workspace_file,
            read_workspace_file,
            delete_workspace_item,
            move_workspace_item,
            get_workspace_file_info,
            execute_command,
            view_file,
            create_file,
            delete_file,
            append_to_file,
            edit_file,
            insert_to_file,
            undo_edit,
            list_directory,
            search_files,
            get_repo_map,
            upload_user_file,
            download_user_file,
            list_user_files,
            delete_user_file,
            get_user_file_info,
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

        project_context = get_runtime_project_context()
        return await runner.run_async(
            request,
            project_context=project_context,
        )

    _delegate.__name__ = tool_name
    _delegate.__doc__ = description
    registry.register(tool_decorator(_delegate))


def _register_agent_team_delegate_tool(
    registry: ToolRegistry,
    *,
    config: Any,
) -> None:
    if "agent_team_delegate" in registry:
        return
    if not agent_team_enabled(config) or not agent_team_scalable_members(config):
        return

    async def agent_team_delegate(role: str, task: str, instances: int = 1) -> str:
        """Delegate work to scalable Agent Team teammates.

        Args:
            role: One of architect, explorer, implementer, reviewer.
            task: The bounded assignment for the teammate(s).
            instances: How many same-role teammates to run. Clamped by the role max.
        """
        decision = check_tool_call_allowed(
            "agent_team_delegate",
            user_input=get_current_user_input(),
            tool_args={"role": role, "task": task, "instances": instances},
            config=config,
        )
        if not decision.allowed:
            print(f"[ToolPolicy] blocked agent_team_delegate: {decision.reason}")
            return format_blocked_tool_result("agent_team_delegate", decision)

        member = agent_team_delegate_member(config, role)
        if not member:
            enabled_roles = ", ".join(
                item["member_key"] for item in agent_team_scalable_members(config)
            )
            return f"Agent Team role is not available: {role}. Enabled roles: {enabled_roles}"

        member_key = str(member.get("member_key") or role)
        if member_key not in SCALABLE_MEMBER_KEYS:
            return f"Agent Team role is not scalable: {member_key}"

        count = agent_team_clamp_instances(config, member_key, instances)
        if count <= 0:
            return f"Agent Team role is disabled: {member_key}"

        project_context = get_runtime_project_context()
        label = str(member.get("label") or member_key)
        provider = str(member.get("provider") or "").strip()
        model = str(member.get("model") or "").strip()
        parent_agent_run_id = get_current_agent_run_id()
        agent_run_service = AgentRunService() if parent_agent_run_id else None

        def _instance_payload(index: int) -> dict[str, Any]:
            instance_label = f"{label}-{index}"
            return {
                "actor_type": "agent_team",
                "actor_key": member_key,
                "agent_member_key": member_key,
                "agent_instance_key": f"{member_key}-{index}",
                "actor_label": instance_label,
                "agent_label": instance_label,
                "provider": provider or None,
                "model": model or None,
                "role": member_key,
                "instance_index": index,
                "instance_count": count,
                "task": task,
            }

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

        for index in range(1, count + 1):
            await _record_instance_event(
                "agent_team.instance_started",
                index,
                status="running",
                message=f"{label}-{index} を実行しています",
            )

        async def _run_one(index: int) -> str:
            runner = AgentTeamRoleDelegationRunner(
                config,
                member_key=member_key,
                display_name=f"{label}-{index}",
            )
            scoped_task = (
                f"You are {label}-{index} in the Agent Team.\n"
                f"Role: {member_key}\n"
                f"Team size for this delegation: {count}\n"
                "Coordinate by keeping your scope independent and returning a concise result.\n\n"
                f"Assignment:\n{task}"
            )
            return await runner.run_async(scoped_task, project_context=project_context)

        import asyncio

        results = await asyncio.gather(
            *[_run_one(index + 1) for index in range(count)],
            return_exceptions=True,
        )
        for index, result in enumerate(results, start=1):
            if isinstance(result, Exception):
                await _record_instance_event(
                    "agent_team.instance_failed",
                    index,
                    status="failed",
                    message=f"{label}-{index} の実行に失敗しました",
                    extra={"error": str(result)},
                )
            else:
                await _record_instance_event(
                    "agent_team.instance_succeeded",
                    index,
                    status="succeeded",
                    message=f"{label}-{index} が完了しました",
                    extra={"result_preview": str(result)[:1200]},
                )

        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise failures[0]

        successful_results = [str(result) for result in results]
        if count == 1:
            return successful_results[0]
        return "\n\n".join(
            f"## {label}-{index + 1}\n{result}"
            for index, result in enumerate(successful_results)
        )

    registry.register(
        replace(
            tool_decorator(agent_team_delegate),
            owner="agent_team",
            risk="medium",
            side_effect="none",
            supports_parallel=False,
        )
    )


def build_runtime_tool_registry(config: Any) -> ToolRegistry:
    """Build the root runtime registry with direct tools and high-level delegates."""
    registry = ToolRegistry()

    if config and not config.get("use_tools", True):
        return registry

    if config and config.get("skills", {}).get("enabled", True) and invoke_skill is not None:
        registry.register(invoke_skill)

    if _app_factory_enabled(config, True):
        set_app_factory_tool_config(config)
        registry.register(create_instant_app_package)

    if not config:
        return registry

    if advanced_reasoning_enabled(config):
        if "advanced_reasoning_assistant" not in registry:
            def advanced_reasoning_assistant(request: str, redacted_request: str = "") -> str:
                """Delegate tool-free hard reasoning to the configured external model.

                Args:
                    request: The original task prompt the main model wants to send.
                    redacted_request: A version of request with confidential names, secrets,
                        internal URLs, local paths, and personal data hidden. This redacted
                        prompt is the default outbound text shown to the user and sent after approval.
                """
                decision = check_tool_call_allowed(
                    "advanced_reasoning_assistant",
                    user_input=get_current_user_input(),
                    tool_args={"request": request, "redacted_request": redacted_request},
                    config=config,
                )
                if not decision.allowed:
                    print(f"[ToolPolicy] blocked advanced_reasoning_assistant: {decision.reason}")
                    return format_blocked_tool_result("advanced_reasoning_assistant", decision)

                from ..services.advanced_reasoning_service import AdvancedReasoningService

                return AdvancedReasoningService(config).run_sync(
                    request,
                    redacted_prompt=redacted_request,
                )

            advanced_reasoning_assistant.__doc__ = (
                "Run a tool-free hard reasoning or review request with the configured "
                "Agent Team advanced reasoning member. Use this only after any required search, file, "
                "project, or utility specialist work is already done, because this "
                "assistant cannot use tools. The outgoing prompt can "
                "be shown to the user for approval and editing first. Include "
                "`redacted_request` whenever the original request may contain "
                "confidential names, internal URLs, local paths, IDs, or personal data.\n\n"
                "Args:\n"
                "    request: Original task prompt the main model wants to send.\n"
                "    redacted_request: Redacted version of request. This is the default "
                "outbound prompt shown to the user and sent after approval.\n"
            )
            registry.register(tool_decorator(advanced_reasoning_assistant))

    _register_agent_team_delegate_tool(registry, config=config)

    if _agent_enabled(config, "search", True):
        _register_search_direct_tools(registry, config=config)

    spotify_enabled = (
        bool(config)
        and _agent_enabled(config, "spotify", True)
        and bool(config.get("spotify", {}).get("enabled", True))
    )
    if spotify_enabled:
        _register_delegation_tool(
            registry,
            tool_name="spotify_assistant",
            description="Delegate Spotify work to the Spotify specialist agent.",
            runner=SpotifyDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "filesystem", True):
        _register_filesystem_direct_tools(registry)

    if _agent_enabled(config, "utility", True):
        _register_delegation_tool(
            registry,
            tool_name="utility_assistant",
            description=(
                "Delegate current time, weather, calculation, and exact utility lookups "
                "to the utility assistant tool. Use the returned value before "
                "claiming the lookup or calculation was done."
            ),
            runner=UtilityDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "media", True):
        _register_delegation_tool(
            registry,
            tool_name="media_assistant",
            description="Delegate image and streaming media work to the media specialist agent.",
            runner=MediaDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "project_management", True):
        _register_project_management_direct_tools(
            registry,
            config=config,
        )

    if _agent_enabled(config, "docs", True):
        _register_docs_direct_tools(registry)

    if _agent_enabled(config, "scenario", True):
        _register_delegation_tool(
            registry,
            tool_name="scenario_assistant",
            description="Delegate TRPG scenario management, dice rolls, and play state tracking to the scenario specialist agent.",
            runner=ScenarioDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "writing", True):
        _register_delegation_tool(
            registry,
            tool_name="writing_assistant",
            description="小説・シナリオの執筆支援: コンテキスト取得、本文生成、保存、Canon更新を執筆専門エージェントに委譲する。",
            runner=WritingDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "import", True):
        _register_delegation_tool(
            registry,
            tool_name="import_assistant",
            description="シナリオ素材のインポート: ディレクトリ分析、キャラ/世界設定/シーンの柔軟な取り込みをインポート専門エージェントに委譲する。",
            runner=ImportDelegationRunner(config),
            config=config,
        )

    # Block duplicate low-level entrypoints so direct root tools stay the only
    # entrypoint for search, filesystem, and project-management work.
    try:
        from ..services.character_service import build_character_agent_tools

        blocked_duplicate_tool_names = {
            "filesystem_assistant",
            "project_management_assistant",
            "search_assistant",
        }
        for tool_def in build_character_agent_tools(config):
            if tool_def.name in blocked_duplicate_tool_names:
                continue
            if tool_def.name not in registry:
                registry.register(tool_def)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "キャラクターエージェントツールの登録に失敗しました", exc_info=True
        )

    return registry
