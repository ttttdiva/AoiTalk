"""Compact tool context selection for CLI LLM backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..tools.adapters import CLIAdapter
from ..tools.core import ToolDefinition
from ..tools.registry import ToolRegistry
from .agent_runtime import (
    DIRECT_FILESYSTEM_TOOL_HINT_NAMES,
    DIRECT_MEMORY_TOOL_HINT_NAMES,
    DIRECT_PROJECT_TOOL_HINT_NAMES,
    DIRECT_SEARCH_TOOL_HINT_NAMES,
)
from .tool_policy import (
    FILESYSTEM_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    looks_like_filesystem_request,
    looks_like_memory_request,
    looks_like_project_management_request,
    looks_like_search_request,
    project_management_required_mutation_tools,
)


ENTRY_TOOL_NAMES: tuple[str, ...] = (
    "utility_assistant",
    "media_assistant",
    "scenario_assistant",
    "writing_assistant",
    "import_assistant",
    "web_search",
    "find_workspace_items",
    "read_workspace_file",
    "get_project_context",
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

    if looks_like_memory_request(request):
        add_names(DIRECT_MEMORY_TOOL_HINT_NAMES, "過去会話")

    if looks_like_search_request(request):
        search_names = [
            tool.name
            for tool in all_tools
            if tool.owner == "search"
            or tool.name in SEARCH_TOOL_NAMES
            or tool.name in DIRECT_SEARCH_TOOL_HINT_NAMES
        ]
        add_names(search_names, "検索")

    if looks_like_filesystem_request(request):
        filesystem_names = [
            tool.name
            for tool in all_tools
            if tool.owner == "filesystem"
            or tool.name in FILESYSTEM_TOOL_NAMES
            or tool.name in DIRECT_FILESYSTEM_TOOL_HINT_NAMES
        ]
        add_names(filesystem_names, "ファイル")

    if force_project_tools or looks_like_project_management_request(request):
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
) -> str:
    selection = select_cli_context_tools(
        user_input=user_input,
        registry=registry,
        force_project_tools=force_project_tools,
    )
    if not selection.tools:
        return ""

    groups = ", ".join(selection.included_groups) or "なし"
    header = (
        f"この発話で利用できるAoiTalkツール "
        f"({len(selection.tools)} / {selection.total_tool_count}; 種別: {groups}):"
    )
    return f"{header}\n{CLIAdapter.to_prompt_text(selection.tools)}"
