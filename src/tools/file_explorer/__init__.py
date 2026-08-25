# File Explorer module
from importlib import import_module

from .file_explorer_service import (
    get_root_dir,
    workspace_root_context,
    is_safe_workspace_path,
    list_directory,
    resolve_workspace_path,
    resolve_upload_target,
    create_directory,
    upload_file,
    upload_file_stream,
    download_file,
    download_items,
    resolve_file_path,
    rename_item,
    move_item,
    copy_item,
    archive_items,
    extract_archives,
    delete_item,
    restore_from_trash,
    get_file_info,
    get_preview,
    get_directory_tree,
    walk_workspace_tree,
    search_workspace_entries,
    # Editor functions
    save_file,
    get_full_content,
    search_files,
    # Folder thumbnail
    set_folder_thumbnail,
    clear_folder_thumbnail,
)
_TOOL_EXPORTS = {
    "create_workspace_directory": "create_workspace_directory",
    "upload_workspace_file": "upload_workspace_file",
    "delete_workspace_item": "delete_workspace_item",
    "move_workspace_item": "move_workspace_item",
    "copy_workspace_item": "copy_workspace_item",
    "list_workspace_tree": "list_workspace_tree",
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
    "workspace_root_context",
    "is_safe_workspace_path",
    "list_directory",
    "create_directory",
    "upload_file",
    "upload_file_stream",
    "download_file",
    "download_items",
    "resolve_file_path",
    "rename_item",
    "move_item",
    "copy_item",
    "archive_items",
    "extract_archives",
    "delete_item",
    "restore_from_trash",
    "get_file_info",
    "get_preview",
    "get_directory_tree",
    "walk_workspace_tree",
    "search_workspace_entries",
    "resolve_workspace_path",
    "resolve_upload_target",
    # Editor functions
    "save_file",
    "get_full_content",
    "search_files",
    # Folder thumbnail
    "set_folder_thumbnail",
    "clear_folder_thumbnail",
    # LLM Tools
    "create_workspace_directory",
    "upload_workspace_file",
    "delete_workspace_item",
    "move_workspace_item",
    "copy_workspace_item",
    "list_workspace_tree",
    "get_workspace_file_info",
]
