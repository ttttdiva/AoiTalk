"""Filesystem specialist agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..tools.adapters import OpenAIAgentAdapter
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
from .base import BaseAgent


class FilesystemAgent(BaseAgent):
    """Specialized agent for local workspace and filesystem tasks."""

    def _create_agent(self) -> Agent:
        tools = [
            *OpenAIAgentAdapter.convert_all(
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
                ]
            ),
            upload_user_file,
            download_user_file,
            list_user_files,
            delete_user_file,
            get_user_file_info,
        ]

        instructions = """
You are a filesystem specialist.

Handle workspace browsing, file CRUD, directory operations, repository maps,
and command execution directly with tools. Prefer precise file operations over
describing manual steps.

Exploration policy:
- For "can you read/find/check this folder/file?" requests, do not stop at
  existence checks. First locate the named item, then inspect enough structure
  to answer what is actually present.
- Prefer `find_workspace_items` for named files/folders in the workspace. After
  finding a relevant folder, call `inspect_workspace_tree` with a bounded depth
  before reading individual files.
- When a folder appears to be a project handoff, knowledge base, or shared
  material, read the likely orientation files first when present: README,
  AGENTS, CLAUDE, TODO, index, activeContext, projectInfo, progress, techSpec,
  and similarly named files.
- If the first inventory shows important subfolders, continue one level deeper
  into the relevant subfolders instead of summarizing from the top level only.
- In the final answer, distinguish "read/inspected" from "found but not yet
  read". Do not say a folder was read if only its name or top-level listing was
  checked.

If the delegated request is not actually about files, folders, repositories,
workspace documents, or local commands, say that no filesystem action is needed
and do not call a filesystem tool.
""".strip()

        return Agent(
            name="FilesystemAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="auto"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "filesystem_assistant"

    def get_tool_description(self) -> str:
        return (
            "Filesystem assistant - manage workspace files, inspect repositories, "
            "edit files, and run local commands"
        )
