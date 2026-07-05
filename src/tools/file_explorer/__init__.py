# File Explorer module
from importlib import import_module

from .file_explorer_service import (
    get_root_dir,
    list_directory,
    create_directory,
    upload_file,
    download_file,
    download_items,
    resolve_file_path,
    rename_item,
    move_item,
    copy_item,
    archive_items,
    extract_archives,
    delete_item,
    get_file_info,
    get_preview,
    get_directory_tree,
    inspect_workspace_tree,
    find_workspace_items,
    # Editor functions
    save_file,
    get_full_content,
    search_files,
    # Folder thumbnail
    set_folder_thumbnail,
    clear_folder_thumbnail,
)
_TOOL_EXPORTS = {
    "list_workspace_files": "list_workspace_files",
    "find_workspace_items_tool": "find_workspace_items",
    "inspect_workspace_tree_tool": "inspect_workspace_tree",
    "create_workspace_directory": "create_workspace_directory",
    "upload_workspace_file": "upload_workspace_file",
    "read_workspace_file": "read_workspace_file",
    "delete_workspace_item": "delete_workspace_item",
    "move_workspace_item": "move_workspace_item",
    "get_workspace_file_info": "get_workspace_file_info",
}


def __getattr__(name: str):
    if name not in _TOOL_EXPORTS:
        raise AttributeError(name)
    file_explorer_tools = import_module(f"{__name__}.file_explorer_tools")
    value = getattr(file_explorer_tools, _TOOL_EXPORTS[name])
    globals()[name] = value
    return value

__all__ = [
    # Service functions
    "get_root_dir",
    "list_directory",
    "create_directory",
    "upload_file",
    "download_file",
    "download_items",
    "resolve_file_path",
    "rename_item",
    "move_item",
    "copy_item",
    "archive_items",
    "extract_archives",
    "delete_item",
    "get_file_info",
    "get_preview",
    "get_directory_tree",
    "inspect_workspace_tree",
    "find_workspace_items",
    # Editor functions
    "save_file",
    "get_full_content",
    "search_files",
    # Folder thumbnail
    "set_folder_thumbnail",
    "clear_folder_thumbnail",
    # LLM Tools
    "list_workspace_files",
    "find_workspace_items_tool",
    "inspect_workspace_tree_tool",
    "create_workspace_directory",
    "upload_workspace_file",
    "read_workspace_file",
    "delete_workspace_item",
    "move_workspace_item",
    "get_workspace_file_info",
]
