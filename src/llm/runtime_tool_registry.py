"""Runtime tool registry helpers for LLM clients."""

from __future__ import annotations

from typing import Any

from ..services.project_context import get_runtime_project_context
from ..skills.executor import invoke_skill
from ..tools.core import tool as tool_decorator
from ..tools.registry import ToolRegistry
from ..services.model_sharing_service import model_sharing_enabled
from .tool_policy import (
    check_tool_call_allowed,
    format_blocked_tool_result,
    get_current_user_input,
)
from .specialist_delegate import (
    FilesystemDelegationRunner,
    MediaDelegationRunner,
    ProjectManagementDelegationRunner,
    ScenarioDelegationRunner,
    SearchDelegationRunner,
    SpotifyDelegationRunner,
    UtilityDelegationRunner,
    WritingDelegationRunner,
    ImportDelegationRunner,
)


def _agent_enabled(config: Any, domain_key: str, default: bool = True) -> bool:
    if not config:
        return default
    return bool(config.get("agents", {}).get(domain_key, {}).get("enabled", default))


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

        if tool_name in {"filesystem_assistant", "project_management_assistant"}:
            print(f"[ToolPolicy] allowed {tool_name}: {decision.reason}")
        return await runner.run_async(
            request,
            project_context=get_runtime_project_context(),
        )

    _delegate.__name__ = tool_name
    _delegate.__doc__ = description
    registry.register(tool_decorator(_delegate))


def build_runtime_tool_registry(config: Any) -> ToolRegistry:
    """Build a runtime registry with specialist delegation tools."""
    registry = ToolRegistry()

    if config and not config.get("use_tools", True):
        return registry

    if config and config.get("skills", {}).get("enabled", True) and invoke_skill is not None:
        registry.register(invoke_skill)

    if not config:
        return registry

    if model_sharing_enabled(config):
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

                from ..services.model_sharing_service import AdvancedReasoningService

                return AdvancedReasoningService(config).run_sync(
                    request,
                    redacted_prompt=redacted_request,
                )

            advanced_reasoning_assistant.__doc__ = (
                "Run a tool-free hard reasoning or review request with the configured "
                "model-sharing target. Use this only after any required search, file, "
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

    if _agent_enabled(config, "search", True):
        _register_delegation_tool(
            registry,
            tool_name="search_assistant",
            description=(
                "Delegate public web search, optional X/Twitter search, "
                "Knowledge document search, and memory search to the search specialist agent."
            ),
            runner=SearchDelegationRunner(config),
            config=config,
        )

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
        _register_delegation_tool(
            registry,
            tool_name="filesystem_assistant",
            description=(
                "Delegate local file and workspace work to the filesystem specialist "
                "agent, including finding named folders/files, inspecting folder trees, "
                "and reading workspace documents before answering."
            ),
            runner=FilesystemDelegationRunner(config),
            config=config,
        )

    if _agent_enabled(config, "utility", True):
        _register_delegation_tool(
            registry,
            tool_name="utility_assistant",
            description="Delegate utility work to the utility specialist agent.",
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
        _register_delegation_tool(
            registry,
            tool_name="project_management_assistant",
            description="Delegate project information, task, WBS, schedule, and case-management work to the project specialist agent.",
            runner=ProjectManagementDelegationRunner(config),
            config=config,
        )

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

    # DB登録キャラクターエージェントツール
    try:
        from ..services.character_service import build_character_agent_tools

        for tool_def in build_character_agent_tools(config):
            if tool_def.name not in registry:
                registry.register(tool_def)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "キャラクターエージェントツールの登録に失敗しました", exc_info=True
        )

    return registry
