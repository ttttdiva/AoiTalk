"""コマンド実行ツール"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from ..config_loader import get_protected_paths, is_path_protected

_DESTRUCTIVE_COMMANDS = [
    'del', 'erase', 'rm', 'remove',
    'rmdir', 'rd',
    'move', 'mv', 'ren', 'rename',
    'echo', 'type', 'copy', 'cp',
]

_DESTRUCTIVE_POWERSHELL_CMDLETS = [
    'remove-item', 'ri', 'rm', 'rmdir', 'del', 'erase', 'rd',
    'move-item', 'mi', 'mv', 'move',
    'rename-item', 'rni', 'ren',
    'copy-item', 'ci', 'cp', 'copy',
    'set-content', 'sc',
    'out-file',
    'add-content', 'ac',
    'clear-content', 'clc',
    'new-item', 'ni',
]


def _extract_paths_from_command(command: str) -> List[str]:
    paths = []
    quoted_pattern = r'["\']([A-Za-z]:\\[^"\']+|/[^"\']+)["\']'
    paths.extend(re.findall(quoted_pattern, command))
    win_path_pattern = r'(?<!["\'])([A-Za-z]:\\[^\s"\'<>|]+)'
    paths.extend(re.findall(win_path_pattern, command))
    return paths


def _check_command_protection(command: str, working_directory: Optional[str]) -> Optional[dict]:
    protected_paths = get_protected_paths()
    if not protected_paths:
        return None

    command_lower = command.lower()
    parts = command.strip().split()
    if not parts:
        return None

    base_cmd = parts[0].lower().replace('.exe', '')
    is_powershell = base_cmd in ['powershell', 'pwsh']

    if is_powershell:
        detected_cmdlet = None
        for cmdlet in _DESTRUCTIVE_POWERSHELL_CMDLETS:
            if cmdlet in command_lower:
                if re.search(r'\b' + re.escape(cmdlet) + r'\b', command_lower):
                    detected_cmdlet = cmdlet
                    break
        if detected_cmdlet:
            paths = _extract_paths_from_command(command)
            for check_path in paths:
                is_prot, matched_path = is_path_protected(check_path)
                if is_prot:
                    return {
                        "success": False,
                        "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。"
                    }
        return None

    is_destructive = any(base_cmd == dc for dc in _DESTRUCTIVE_COMMANDS)
    if not is_destructive:
        return None

    for arg in parts[1:]:
        if arg.startswith('-') or arg.startswith('/'):
            continue
        arg = arg.strip('"').strip("'")
        try:
            if os.path.isabs(arg):
                check_path = arg
            elif working_directory:
                check_path = os.path.join(working_directory, arg)
            else:
                check_path = arg
            is_prot, matched_path = is_path_protected(check_path)
            if is_prot:
                return {
                    "success": False,
                    "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。"
                }
        except Exception:
            continue

    return None


def register(mcp: FastMCP):
    """コマンド実行ツールを MCP サーバーに登録する。"""

    from src.tools.os_operations.command_executor import get_command_executor

    @mcp.tool()
    async def execute_command(command: str, working_directory: Optional[str] = None) -> str:
        """シェルコマンドを実行する

        Args:
            command: 実行するコマンド（例：「dir」「ls -la」「python script.py」）
            working_directory: コマンドを実行するディレクトリ（省略時はカレントディレクトリ）
        """
        protection_error = _check_command_protection(command, working_directory)
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        executor = get_command_executor()
        result = executor.execute(command, cwd=working_directory)

        if result.success:
            return json.dumps({
                "success": True,
                "output": result.stdout,
                "stderr": result.stderr if result.stderr else None,
                "return_code": result.return_code
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "success": False,
                "error": result.error_message or result.stderr,
                "output": result.stdout if result.stdout else None,
                "timed_out": result.timed_out
            }, ensure_ascii=False)
