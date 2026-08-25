"""
OS Operations package for AoiTalk

Provides self-contained OS operation tools including:
- Command execution (shell commands)
- File editing (view, create, edit, undo)
- File system operations (list, search)

Self-contained implementation for local file and system operations.
"""

from .background_jobs import (
    BackgroundJob,
    BackgroundJobRegistry,
    compute_scope_fingerprint,
)
from .command_executor import CommandExecutor, CommandResult
from .file_editor import FileEditor
from .file_system import FileSystem
from .tools import (
    execute_command,
    read_command_output,
    write_command_input,
    stop_command,
    list_commands,
    close_scoped_jobs,
    preflight_scoped_jobs,
    assert_no_active_scoped_jobs,
    read_file,
    create_file,
    delete_file,
    append_to_file,
    edit_file,
    insert_to_file,
    undo_edit,
    list_directory,
    search_files,
)

__all__ = [
    # Core classes
    'BackgroundJob',
    'BackgroundJobRegistry',
    'compute_scope_fingerprint',
    'CommandExecutor',
    'CommandResult',
    'FileEditor',
    'FileSystem',
    # Function tools
    'execute_command',
    'read_command_output',
    'write_command_input',
    'stop_command',
    'list_commands',
    'close_scoped_jobs',
    'preflight_scoped_jobs',
    'assert_no_active_scoped_jobs',
    'read_file',
    'create_file',
    'delete_file',
    'append_to_file',
    'edit_file',
    'insert_to_file',
    'undo_edit',
    'list_directory',
    'search_files',
]
