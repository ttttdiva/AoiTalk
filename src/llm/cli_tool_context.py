"""Compact tool context selection for CLI LLM backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..tools.adapters import CLIAdapter
from ..tools.core import ToolDefinition
from ..tools.registry import ToolRegistry
from .agent_runtime import (
    DIRECT_PROJECT_TOOL_HINT_NAMES,
    DIRECT_SEARCH_TOOL_HINT_NAMES,
)
from .tool_packs import DEFERRED_TOOL_PACKS, LOAD_TOOL_PACK_TOOL_NAME
from .tool_policy import (
    PROJECT_COMMAND_CAPABILITIES,
    SEARCH_TOOL_NAMES,
    command_capabilities_from_text,
    looks_like_search_request,
    looks_like_managed_workspace_request,
    FILESYSTEM_TOOL_NAMES,
    DOCS_MUTATION_TOOL_NAMES,
    DOCS_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    project_management_required_mutation_tools,
)


ENTRY_TOOL_NAMES: tuple[str, ...] = (
    LOAD_TOOL_PACK_TOOL_NAME,
    "media_assistant",
    "agent_team_delegate",
    "web_search",
    "bm25_search",
    "search_files",
    "read_file",
    "get_project_context",
    "get_current_time",
    "get_weather_info",
    "calculate",
)

TITLE_GENERATION_MARKERS: tuple[str, ...] = (
    "\u5c65\u6b74\u4e00\u89a7\u3067\u8b58\u5225\u3057\u3084\u3059\u3044\u77ed\u3044\u65e5\u672c\u8a9e\u30bf\u30a4\u30c8\u30eb",
    "\u30bf\u30a4\u30c8\u30eb\u306e\u307f\u51fa\u529b",
)

PROJECT_BASE_TOOL_NAMES: tuple[str, ...] = (
    "get_project_context",
    "list_projects",
    "list_record_tables",
    "list_project_information",
    "get_project_progress",
    "list_tasks",
    "list_calendar",
    "get_time_report",
    "get_project_issues",
    "get_upcoming_wbs_tasks",
    "render_project_diagram",
    "summarize_project_requests",
)


@dataclass(frozen=True)
class CLIToolContextSelection:
    tools: list[ToolDefinition]
    included_groups: tuple[str, ...]
    total_tool_count: int


def select_cli_context_tools(
    *,
    user_input: str | None,
    registry: ToolRegistry,
    force_project_tools: bool = False,
    loaded_pack_ids: Iterable[str] = (),
) -> CLIToolContextSelection:
    """Select the tools worth describing in the current CLI system prompt."""
    all_tools = registry.get_all()
    tools_by_name = {tool.name: tool for tool in all_tools}
    selected_names: list[str] = []
    included_groups: list[str] = []

    def add_names(names: Iterable[str], group: str) -> None:
        added = False
        for name in names:
            if name in tools_by_name and name not in selected_names:
                selected_names.append(name)
                added = True
        if added and group not in included_groups:
            included_groups.append(group)

    request = str(user_input or "")
    if _looks_like_internal_title_generation_request(request):
        return CLIToolContextSelection(
            tools=[],
            included_groups=(),
            total_tool_count=len(all_tools),
        )

    add_names(ENTRY_TOOL_NAMES, "入口")

    loaded_packs = {str(pack_id).strip() for pack_id in loaded_pack_ids if str(pack_id).strip()}
    if loaded_packs:
        loaded_pack_names: list[str] = []
        for pack in DEFERRED_TOOL_PACKS:
            if pack.pack_id not in loaded_packs:
                continue
            loaded_pack_names.extend(
                tool.name
                for tool in all_tools
                if pack.matches(tool.name, getattr(tool, "owner", ""))
            )
        add_names(loaded_pack_names, "ロード済みpack")

    # Search is only expanded for a trusted command capability.  Ordinary
    # "search/調べて" wording must not narrow the CLI catalog.
    if looks_like_search_request(request):
        search_names = [
            tool.name
            for tool in all_tools
            if tool.owner == "search"
            or tool.name in SEARCH_TOOL_NAMES
            or tool.name in DIRECT_SEARCH_TOOL_HINT_NAMES
        ]
        add_names(search_names, "検索")

    # A server-verified attachment or explicit workspace capability opts into
    # the managed-operation tree/place/link tool set.  Ordinary standalone
    # file or Docs words remain un-routed below.
    if looks_like_managed_workspace_request(request):
        filesystem_names = [
            tool.name
            for tool in all_tools
            if tool.owner == "filesystem" or tool.name in FILESYSTEM_TOOL_NAMES
        ]
        docs_names = [
            tool.name
            for tool in all_tools
            if tool.owner == "docs"
            and tool.name in (*DOCS_READ_TOOL_NAMES, *DOCS_MUTATION_TOOL_NAMES)
        ]
        add_names(filesystem_names, "ファイル")
        add_names(docs_names, "Docs")

    # Project/Docs/Files are intentionally not re-routed by natural keywords.
    # ``force_project_tools`` is a trusted internal/UI signal; command context
    # headers are the other explicit signal available to this CLI selector.
    capabilities = command_capabilities_from_text(request)
    if force_project_tools or capabilities & PROJECT_COMMAND_CAPABILITIES:
        project_names = [
            *PROJECT_BASE_TOOL_NAMES,
            *DIRECT_PROJECT_TOOL_HINT_NAMES,
            *sorted(PROJECT_MANAGEMENT_READ_TOOL_NAMES),
            *sorted(project_management_required_mutation_tools(request)),
        ]
        add_names(project_names, "案件/タスク")

    return CLIToolContextSelection(
        tools=[tools_by_name[name] for name in selected_names],
        included_groups=tuple(included_groups),
        total_tool_count=len(all_tools),
    )


def _looks_like_internal_title_generation_request(text: str) -> bool:
    raw = str(text or "")
    return all(marker in raw for marker in TITLE_GENERATION_MARKERS)


def build_cli_tool_context(
    *,
    user_input: str | None,
    registry: ToolRegistry,
    force_project_tools: bool = False,
    loaded_pack_ids: Iterable[str] = (),
) -> str:
    selection = select_cli_context_tools(
        user_input=user_input,
        registry=registry,
        force_project_tools=force_project_tools,
        loaded_pack_ids=loaded_pack_ids,
    )
    if not selection.tools:
        return ""

    groups = ", ".join(selection.included_groups) or "なし"
    header = (
        f"この発話で利用できるAoiTalkツール "
        f"({len(selection.tools)} / {selection.total_tool_count}; 種別: {groups}):"
    )
    return f"{header}\n{CLIAdapter.to_prompt_text(selection.tools)}"
