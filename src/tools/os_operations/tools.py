"""
LLM Function Tools for OS Operations

Provides function tools that can be called by the LLM:
- execute_command: Run shell commands
- view_file: View file contents
- create_file: Create new files
- delete_file: Delete files
- append_to_file: Append content to files
- edit_file: Edit files via string replacement
- insert_to_file: Insert content at specific line
- undo_edit: Undo last file edit
- list_directory: List directory contents
- search_files: Search for files
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import tool as function_tool

from .command_executor import get_command_executor
from .file_editor import get_file_editor, FileEditorError
from .file_system import get_file_system, FileSystemError

logger = logging.getLogger(__name__)

# --- Path Protection Utilities ---

_protected_paths_cache: Optional[List[str]] = None
_allowed_workspace_dirs_cache: Optional[List[str]] = None

# User context for permission checks (set before agent execution)
_current_user_context: Dict[str, Any] = {
    "user_id": None,
    "is_admin": True,  # Default to admin for backward compatibility
    "project_ids": []
}


def set_current_user_context(user_id: Optional[str], is_admin: bool, project_ids: Optional[List[str]] = None):
    """
    Set current user context for path permission checks.
    
    This should be called before agent execution to set the user context.
    
    Args:
        user_id: User UUID as string (None for anonymous/system)
        is_admin: Whether user is admin
        project_ids: List of project UUIDs the user participates in
    """
    global _current_user_context
    _current_user_context = {
        "user_id": user_id,
        "is_admin": is_admin,
        "project_ids": project_ids or []
    }
    logger.debug(f"Set user context: user_id={user_id}, is_admin={is_admin}, projects={len(project_ids or [])}")


def get_current_user_context() -> Dict[str, Any]:
    """Get current user context for permission checks."""
    return _current_user_context.copy()


def clear_user_context():
    """Clear user context (reset to default admin for backward compat)."""
    global _current_user_context
    _current_user_context = {
        "user_id": None,
        "is_admin": True,
        "project_ids": []
    }


def _get_protected_paths() -> List[str]:
    """Get protected paths from config. Cached for performance."""
    global _protected_paths_cache
    if _protected_paths_cache is not None:
        return _protected_paths_cache
    
    try:
        from ...config import Config
        config = Config()
        os_ops_config = config.get('os_operations', {})
        paths = os_ops_config.get('protected_paths', [])
        _protected_paths_cache = paths if paths else []
    except Exception as e:
        logger.warning(f"Failed to load protected paths from config: {e}")
        _protected_paths_cache = []
    
    return _protected_paths_cache


def _get_allowed_workspace_dirs() -> List[str]:
    """Get allowed workspace directory prefixes from config. Cached for performance."""
    global _allowed_workspace_dirs_cache
    if _allowed_workspace_dirs_cache is not None:
        return _allowed_workspace_dirs_cache
    
    try:
        from ...config import Config
        config = Config()
        os_ops_config = config.get('os_operations', {})
        dirs = os_ops_config.get('allowed_workspace_dirs', ['_users', '_projects'])
        _allowed_workspace_dirs_cache = dirs if dirs else []
    except Exception as e:
        logger.warning(f"Failed to load allowed workspace dirs from config: {e}")
        _allowed_workspace_dirs_cache = ['_users', '_projects']
    
    return _allowed_workspace_dirs_cache


def _get_user_files_root() -> Path:
    """Get the user_files root directory."""
    import os
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    return Path(files_dir).resolve()


def _resolve_path_for_user(path: str) -> str:
    """
    Resolve a file path based on user context.
    
    For relative paths:
    - If user context is set: Resolve to user's personal workspace
      (user_files/_users/user_{uuid}/)
    - If no user context: Resolve to user_files root (fallback)
    
    Absolute paths are returned as-is.
    
    Args:
        path: File path to resolve (relative or absolute)
        
    Returns:
        Resolved absolute path string
    """
    # If already absolute, return as-is
    if os.path.isabs(path):
        return path
    
    context = get_current_user_context()
    user_id = context.get("user_id")
    
    user_files_root = _get_user_files_root()
    
    if user_id:
        # User is logged in: resolve to their personal workspace
        # e.g., "日記フォルダ/2026-01-18.md" -> "user_files/_users/user_{id}/日記フォルダ/2026-01-18.md"
        user_workspace = user_files_root / "_users" / f"user_{user_id}"
        user_workspace.mkdir(parents=True, exist_ok=True)
        resolved = user_workspace / path
        logger.debug(f"Resolved path '{path}' to user workspace: {resolved}")
        return str(resolved)
    else:
        # No user context: resolve to user_files root (fallback for system/anonymous)
        resolved = user_files_root / path
        logger.debug(f"Resolved path '{path}' to user_files root: {resolved}")
        return str(resolved)


def _is_path_in_user_workspace(path: str, user_id: Optional[str], project_ids: List[str]) -> bool:
    """
    Check if a path is within the user's allowed workspace directories.
    
    Args:
        path: Path to check
        user_id: User UUID as string
        project_ids: List of project UUIDs the user participates in
        
    Returns:
        True if path is in user's personal directory or a participating project directory
    """
    if not user_id:
        return False
    
    try:
        target_path = Path(path).resolve()
        user_files_root = _get_user_files_root()
        
        # Check if path is under user_files at all
        try:
            target_path.relative_to(user_files_root)
        except ValueError:
            return False
        
        # Check if path is in user's personal directory
        user_dir = user_files_root / "_users" / f"user_{user_id}"
        try:
            target_path.relative_to(user_dir)
            return True
        except ValueError:
            pass
        
        # Check if path is in any participating project directory
        for project_id in project_ids:
            project_dir = user_files_root / "_projects" / f"project_{project_id}"
            try:
                target_path.relative_to(project_dir)
                return True
            except ValueError:
                continue
        
        return False
        
    except Exception as e:
        logger.warning(f"Error checking user workspace path: {e}")
        return False


def _check_user_permission(path: str, operation: str) -> Optional[Dict[str, Any]]:
    """
    Check if current user has permission to perform operation on path.
    
    For admin users: Uses protected_paths check (current behavior)
    For non-admin users: Only allows access to own directory and participating projects
    
    Args:
        path: Path to check
        operation: Operation name for error message
        
    Returns:
        Error dict if not permitted, None if allowed
    """
    context = get_current_user_context()
    
    # Admin users: use existing protected_paths logic
    if context["is_admin"]:
        return None  # Admins bypass this check, will use _check_path_protection
    
    # Non-admin users: must be in their workspace
    user_id = context["user_id"]
    project_ids = context["project_ids"]
    
    if not user_id:
        return {
            "success": False,
            "error": f"操作拒否: ユーザーコンテキストが設定されていません。"
        }
    
    if not _is_path_in_user_workspace(path, user_id, project_ids):
        return {
            "success": False,
            "error": f"操作拒否: パス '{path}' へのアクセス権限がありません。\n"
                     f"自分のディレクトリまたは参加しているプロジェクトのディレクトリのみ{operation}できます。"
        }
    
    return None


def _is_path_protected(path: str) -> tuple[bool, Optional[str]]:
    """
    Check if a path is protected from modification.
    
    Args:
        path: Path to check
        
    Returns:
        Tuple of (is_protected, matched_protected_path)
    """
    protected_paths = _get_protected_paths()
    if not protected_paths:
        return False, None
    
    try:
        # Normalize the path
        target_path = Path(path).resolve()
        
        for protected in protected_paths:
            protected_path = Path(protected).resolve()
            # Check if target is equal to or under the protected path
            try:
                target_path.relative_to(protected_path)
                return True, str(protected_path)
            except ValueError:
                # Not under this protected path
                continue
    except Exception as e:
        logger.warning(f"Error checking path protection for {path}: {e}")
    
    return False, None


def _check_path_protection(path: str, operation: str) -> Optional[Dict[str, Any]]:
    """
    Check if path is protected and return error dict if so.
    
    For admins: Blocks access to protected paths except user_files workspace.
    For non-admins: Uses _check_user_permission instead (called separately).
    
    Args:
        path: Path to check
        operation: Operation name for error message
        
    Returns:
        Error dict if protected, None if allowed
    """
    # First check if path is within user_files (always allowed for all users)
    try:
        target_path = Path(path).resolve()
        user_files_root = _get_user_files_root()
        try:
            target_path.relative_to(user_files_root)
            # Path is within user_files - skip protected paths check
            # Individual user permission check is done separately
            return None
        except ValueError:
            pass  # Not in user_files, continue with protection check
    except Exception:
        pass
    
    is_protected, matched_path = _is_path_protected(path)
    if is_protected:
        return {
            "success": False,
            "error": f"操作拒否: パス '{path}' は保護されています。\n"
                     f"理由: '{matched_path}' 以下のファイルは {operation} できません。\n"
                     f"（config.yaml の os_operations.protected_paths で設定されています）"
        }
    return None




# Destructive command patterns that should be blocked on protected paths
_DESTRUCTIVE_COMMANDS = [
    'del', 'erase', 'rm', 'remove',  # File deletion
    'rmdir', 'rd',  # Directory deletion
    'move', 'mv', 'ren', 'rename',  # Move/rename
    'echo', 'type', 'copy', 'cp',  # These can overwrite files
]

# PowerShell destructive cmdlets (case-insensitive)
_DESTRUCTIVE_POWERSHELL_CMDLETS = [
    'remove-item', 'ri', 'rm', 'rmdir', 'del', 'erase', 'rd',  # Deletion
    'move-item', 'mi', 'mv', 'move',  # Move
    'rename-item', 'rni', 'ren',  # Rename
    'copy-item', 'ci', 'cp', 'copy',  # Copy (can overwrite)
    'set-content', 'sc',  # Overwrite file content
    'out-file',  # Write to file
    'add-content', 'ac',  # Append to file
    'clear-content', 'clc',  # Clear file content
    'new-item', 'ni',  # Create new item
]


def _extract_paths_from_command(command: str) -> List[str]:
    """Extract potential file paths from a command string."""
    import re
    paths = []
    
    # Match paths in various formats:
    # - Quoted paths: "D:\path\to\file" or 'D:\path\to\file'
    # - Unquoted absolute paths: D:\path\to\file or /path/to/file
    
    # Quoted paths
    quoted_pattern = r'["\']([A-Za-z]:\\[^"\']+|/[^"\']+)["\']'
    paths.extend(re.findall(quoted_pattern, command))
    
    # Unquoted Windows absolute paths (D:\something)
    win_path_pattern = r'(?<!["\'])([A-Za-z]:\\[^\s"\'<>|]+)'
    paths.extend(re.findall(win_path_pattern, command))
    
    return paths


def _check_command_protection(command: str, working_directory: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Check if a command targets protected paths with destructive operations.
    
    Args:
        command: Command string to check
        working_directory: Working directory for the command
        
    Returns:
        Error dict if protected path is targeted, None if allowed
    """
    protected_paths = _get_protected_paths()
    if not protected_paths:
        return None
    
    command_lower = command.lower()
    
    # Parse the command to extract the base command
    parts = command.strip().split()
    if not parts:
        return None
    
    base_cmd = parts[0].lower().replace('.exe', '')
    
    # Check for PowerShell commands
    is_powershell = base_cmd in ['powershell', 'pwsh']
    
    if is_powershell:
        # Check for destructive PowerShell cmdlets in the command
        detected_cmdlet = None
        for cmdlet in _DESTRUCTIVE_POWERSHELL_CMDLETS:
            # Check for cmdlet (case-insensitive, word boundary)
            if cmdlet in command_lower:
                # Verify it's a word boundary match
                import re
                if re.search(r'\b' + re.escape(cmdlet) + r'\b', command_lower):
                    detected_cmdlet = cmdlet
                    break
        
        if detected_cmdlet:
            # Extract paths from the PowerShell command
            paths = _extract_paths_from_command(command)
            for check_path in paths:
                is_protected, matched_path = _is_path_protected(check_path)
                if is_protected:
                    return {
                        "success": False,
                        "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。\n"
                                 f"理由: '{matched_path}' 以下は保護されており、PowerShellの '{detected_cmdlet}' を実行できません。\n"
                                 f"（config.yaml の os_operations.protected_paths で設定されています）"
                    }
        return None
    
    # Check if it's a destructive command (non-PowerShell)
    is_destructive = any(base_cmd == dc for dc in _DESTRUCTIVE_COMMANDS)
    if not is_destructive:
        return None
    
    # Extract potential paths from the command arguments
    for arg in parts[1:]:
        # Skip flags
        if arg.startswith('-') or arg.startswith('/'):
            continue
        
        # Remove quotes
        arg = arg.strip('"').strip("'")
        
        # Try to resolve the path
        try:
            if os.path.isabs(arg):
                check_path = arg
            elif working_directory:
                check_path = os.path.join(working_directory, arg)
            else:
                check_path = arg
            
            # Check if this path is protected
            is_protected, matched_path = _is_path_protected(check_path)
            if is_protected:
                return {
                    "success": False,
                    "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。\n"
                             f"理由: '{matched_path}' 以下は保護されており、'{base_cmd}' コマンドを実行できません。\n"
                             f"（config.yaml の os_operations.protected_paths で設定されています）"
                }
        except Exception:
            continue
    
    return None


@function_tool
def execute_command(
    command: str,
    working_directory: Optional[str] = None
) -> Dict[str, Any]:
    """シェルコマンドを実行する
    
    Args:
        command: 実行するコマンド（例：「dir」「ls -la」「python script.py」）
        working_directory: コマンドを実行するディレクトリ（省略時はカレントディレクトリ）
    
    Returns:
        Dict[str, Any]: 実行結果（success, stdout, stderr, return_code）
    
    Examples:
        >>> execute_command("dir")
        >>> execute_command("ls -la", "/home/user")
        >>> execute_command("python --version")
    """
    print(f"[Tool] execute_command が呼び出されました: {command[:50]}...")
    
    # Check for destructive commands on protected paths
    protection_error = _check_command_protection(command, working_directory)
    if protection_error:
        return protection_error
    
    executor = get_command_executor()
    result = executor.execute(command, cwd=working_directory)
    
    if result.success:
        return {
            "success": True,
            "output": result.stdout,
            "stderr": result.stderr if result.stderr else None,
            "return_code": result.return_code
        }
    else:
        error_msg = result.error_message or result.stderr
        return {
            "success": False,
            "error": error_msg,
            "output": result.stdout if result.stdout else None,
            "timed_out": result.timed_out
        }



@function_tool
def view_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None
) -> Dict[str, Any]:
    """ファイルの内容を表示する（行番号付き）
    
    Args:
        path: 表示するファイルのパス
        start_line: 開始行（1から始まる。省略時は最初から）
        end_line: 終了行（省略時または-1で最後まで）
    
    Returns:
        Dict[str, Any]: ファイル内容と結果
    
    Examples:
        >>> view_file("src/main.py")
        >>> view_file("config.yaml", start_line=1, end_line=20)
    """
    print(f"[Tool] view_file が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    try:
        # Convert float to int if Gemini API passes floats (e.g., 1.0 instead of 1)
        if start_line is not None:
            start_line = int(start_line)
        if end_line is not None:
            end_line = int(end_line)
            # Handle -1 as "to end of file"
            if end_line == -1:
                end_line = None
        
        editor = get_file_editor()
        content = editor.view(path, start_line=start_line, end_line=end_line)
        return {
            "success": True,
            "content": content
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in view_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def create_file(
    path: str,
    content: str
) -> Dict[str, Any]:
    """新しいファイルを作成する
    
    Args:
        path: 作成するファイルのパス
        content: ファイルの内容
    
    Returns:
        Dict[str, Any]: 作成結果
    
    Examples:
        >>> create_file("hello.txt", "Hello, World!")
        >>> create_file("src/utils.py", "def helper():\\n    pass")
    """
    print(f"[Tool] create_file が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "作成")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "作成")
    if protection_error:
        return protection_error
    
    try:
        editor = get_file_editor()
        result = editor.create(path, content)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in create_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def delete_file(path: str) -> Dict[str, Any]:
    """ファイルまたはディレクトリを削除する
    
    ファイルの場合は単体削除、ディレクトリの場合は中身ごと再帰的に削除します。
    削除操作には注意が必要です。
    
    Args:
        path: 削除するファイルまたはディレクトリのパス
    
    Returns:
        Dict[str, Any]: 削除結果
    
    Examples:
        >>> delete_file("temp.txt")
        >>> delete_file("D:\\Download\\old_folder")
        >>> delete_file("/tmp/cache")
    """
    import os
    import shutil
    print(f"[Tool] delete_file が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "削除")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "削除")
    if protection_error:
        return protection_error
    
    try:
        if not os.path.exists(path):
            return {
                "success": False,
                "error": f"File or directory not found: {path}"
            }
        
        if os.path.isdir(path):
            # Delete directory recursively
            shutil.rmtree(path)
            return {
                "success": True,
                "message": f"Directory deleted successfully (including all contents): {path}"
            }
        else:
            # Delete single file
            os.remove(path)
            return {
                "success": True,
                "message": f"File deleted successfully: {path}"
            }
    except PermissionError:
        return {
            "success": False,
            "error": f"Permission denied: {path}"
        }
    except Exception as e:
        logger.error(f"Error in delete_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def append_to_file(path: str, content: str) -> Dict[str, Any]:
    """ファイルの末尾に内容を追記する
    
    Args:
        path: 追記するファイルのパス
        content: 追記する内容
    
    Returns:
        Dict[str, Any]: 追記結果
    
    Examples:
        >>> append_to_file("log.txt", "新しいログエントリ")
        >>> append_to_file("data.txt", "\\n追加データ")
    """
    import os
    print(f"[Tool] append_to_file が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error
    
    try:
        if not os.path.exists(path):
            return {
                "success": False,
                "error": f"File not found: {path}. Use create_file for new files."
            }
        
        with open(path, 'a', encoding='utf-8') as f:
            f.write(content)
        
        return {
            "success": True,
            "message": f"Content appended to file: {path}"
        }
    except PermissionError:
        return {
            "success": False,
            "error": f"Permission denied: {path}"
        }
    except Exception as e:
        logger.error(f"Error in append_to_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def edit_file(
    path: str,
    old_str: str,
    new_str: str
) -> Dict[str, Any]:
    """ファイル内の文字列を置換して編集する
    
    old_strはファイル内で一意である必要があります（複数箇所にある場合はエラー）。
    編集は取り消し可能です（undo_editを使用）。
    
    Args:
        path: 編集するファイルのパス
        old_str: 置換する元の文字列（ファイル内で一意である必要あり）
        new_str: 置換後の文字列
    
    Returns:
        Dict[str, Any]: 編集結果
    
    Examples:
        >>> edit_file("config.py", "DEBUG = False", "DEBUG = True")
        >>> edit_file("main.py", "def old_func():", "def new_func():")
    """
    print(f"[Tool] edit_file が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error
    
    try:
        editor = get_file_editor()
        result = editor.str_replace(path, old_str, new_str)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in edit_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def insert_to_file(
    path: str,
    line_number: int,
    content: str
) -> Dict[str, Any]:
    """ファイルの指定行に内容を挿入する
    
    Args:
        path: 編集するファイルのパス
        line_number: 挿入する行番号（0=ファイルの先頭、n=n行目の後）
        content: 挿入する内容
    
    Returns:
        Dict[str, Any]: 挿入結果
    
    Examples:
        >>> insert_to_file("main.py", 0, "# -*- coding: utf-8 -*-")
        >>> insert_to_file("config.yaml", 10, "new_setting: value")
    """
    print(f"[Tool] insert_to_file が呼び出されました: {path} at line {line_number}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error
    
    try:
        editor = get_file_editor()
        result = editor.insert(path, line_number, content)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in insert_to_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def undo_edit(path: str) -> Dict[str, Any]:
    """ファイルの直前の編集を取り消す
    
    edit_fileまたはinsert_to_fileで行った変更を元に戻します。
    
    Args:
        path: 取り消し対象のファイルパス
    
    Returns:
        Dict[str, Any]: 取り消し結果
    
    Examples:
        >>> undo_edit("config.py")
    """
    print(f"[Tool] undo_edit が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    try:
        editor = get_file_editor()
        result = editor.undo(path)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in undo_edit: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def list_directory(
    path: str,
    max_depth: int = 2,
    pattern: Optional[str] = None
) -> Dict[str, Any]:
    """ディレクトリの内容を一覧表示する
    
    Args:
        path: 一覧表示するディレクトリのパス
        max_depth: 再帰的に表示する深さ（デフォルト: 2）
        pattern: フィルタするパターン（例: "*.py"）
    
    Returns:
        Dict[str, Any]: ディレクトリ内容
    
    Examples:
        >>> list_directory(".")
        >>> list_directory("src", max_depth=3)
        >>> list_directory(".", pattern="*.py")
    """
    print(f"[Tool] list_directory が呼び出されました: {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    try:
        fs = get_file_system()
        result = fs.list_directory(path, max_depth=max_depth, pattern=pattern)
        return {
            "success": True,
            "content": result
        }
    except FileSystemError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in list_directory: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@function_tool
def search_files(
    query: str,
    path: str,
    extensions: Optional[List[str]] = None,
    search_content: bool = False
) -> Dict[str, Any]:
    """ファイルを検索する
    
    ファイル名またはファイル内容で検索できます。
    
    Args:
        query: 検索クエリ（ファイル名検索時はglob、内容検索時は正規表現）
        path: 検索対象のディレクトリパス
        extensions: フィルタする拡張子のリスト（例: [".py", ".js"]）
        search_content: Trueの場合、ファイル内容を検索（デフォルト: False=ファイル名検索）
    
    Returns:
        Dict[str, Any]: 検索結果
    
    Examples:
        >>> search_files("*.py", "src")
        >>> search_files("config", ".", extensions=[".yaml", ".json"])
        >>> search_files("TODO", "src", search_content=True)
    """
    print(f"[Tool] search_files が呼び出されました: {query} in {path}")
    
    # Resolve relative paths to user's workspace
    path = _resolve_path_for_user(path)
    
    try:
        fs = get_file_system()
        result = fs.search_files(
            query, path, 
            extensions=extensions, 
            search_content=search_content
        )
        return {
            "success": True,
            "content": result
        }
    except FileSystemError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in search_files: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }
