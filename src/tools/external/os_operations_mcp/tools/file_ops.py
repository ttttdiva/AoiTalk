"""ファイル操作ツール"""

from __future__ import annotations

import json
import os
import shutil
import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from ..config_loader import resolve_path_for_user, check_path_protection

logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    """ファイル操作ツールを MCP サーバーに登録する。"""

    from src.tools.os_operations.file_editor import get_file_editor, FileEditorError

    @mcp.tool()
    async def view_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        """ファイルの内容を表示する（行番号付き）

        Args:
            path: 表示するファイルのパス
            start_line: 開始行（1から始まる。省略時は最初から）
            end_line: 終了行（省略時は最後まで。-1も最後まで）
        """
        path = resolve_path_for_user(path)
        try:
            if start_line is not None:
                start_line = int(start_line)
            if end_line is not None:
                end_line = int(end_line)
                if end_line == -1:
                    end_line = None

            editor = get_file_editor()
            content = editor.view(path, start_line=start_line, end_line=end_line)
            return json.dumps({"success": True, "content": content}, ensure_ascii=False)
        except FileEditorError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def create_file(path: str, content: str) -> str:
        """新しいファイルを作成する

        Args:
            path: 作成するファイルのパス
            content: ファイルの内容
        """
        path = resolve_path_for_user(path)
        protection_error = check_path_protection(path, "作成")
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        try:
            editor = get_file_editor()
            result = editor.create(path, content)
            return json.dumps({"success": True, "message": result}, ensure_ascii=False)
        except FileEditorError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def delete_file(path: str) -> str:
        """ファイルまたはディレクトリを削除する

        Args:
            path: 削除するファイルまたはディレクトリのパス
        """
        path = resolve_path_for_user(path)
        protection_error = check_path_protection(path, "削除")
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        try:
            if not os.path.exists(path):
                return json.dumps({"success": False, "error": f"File or directory not found: {path}"}, ensure_ascii=False)
            if os.path.isdir(path):
                shutil.rmtree(path)
                return json.dumps({"success": True, "message": f"Directory deleted: {path}"}, ensure_ascii=False)
            else:
                os.remove(path)
                return json.dumps({"success": True, "message": f"File deleted: {path}"}, ensure_ascii=False)
        except PermissionError:
            return json.dumps({"success": False, "error": f"Permission denied: {path}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def append_to_file(path: str, content: str) -> str:
        """ファイルの末尾に内容を追記する

        Args:
            path: 追記するファイルのパス
            content: 追記する内容
        """
        path = resolve_path_for_user(path)
        protection_error = check_path_protection(path, "編集")
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        try:
            if not os.path.exists(path):
                return json.dumps({"success": False, "error": f"File not found: {path}"}, ensure_ascii=False)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(content)
            return json.dumps({"success": True, "message": f"Content appended to: {path}"}, ensure_ascii=False)
        except PermissionError:
            return json.dumps({"success": False, "error": f"Permission denied: {path}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def edit_file(path: str, old_str: str, new_str: str) -> str:
        """ファイル内の文字列を置換して編集する

        Args:
            path: 編集するファイルのパス
            old_str: 置換する元の文字列（ファイル内で一意である必要あり）
            new_str: 置換後の文字列
        """
        path = resolve_path_for_user(path)
        protection_error = check_path_protection(path, "編集")
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        try:
            editor = get_file_editor()
            result = editor.str_replace(path, old_str, new_str)
            return json.dumps({"success": True, "message": result}, ensure_ascii=False)
        except FileEditorError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def insert_to_file(path: str, line_number: int, content: str) -> str:
        """ファイルの指定行に内容を挿入する

        Args:
            path: 編集するファイルのパス
            line_number: 挿入する行番号（0=ファイルの先頭、n=n行目の後）
            content: 挿入する内容
        """
        path = resolve_path_for_user(path)
        protection_error = check_path_protection(path, "編集")
        if protection_error:
            return json.dumps(protection_error, ensure_ascii=False)

        try:
            editor = get_file_editor()
            result = editor.insert(path, line_number, content)
            return json.dumps({"success": True, "message": result}, ensure_ascii=False)
        except FileEditorError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def undo_edit(path: str) -> str:
        """ファイルの直前の編集を取り消す

        Args:
            path: 取り消し対象のファイルパス
        """
        path = resolve_path_for_user(path)
        try:
            editor = get_file_editor()
            result = editor.undo(path)
            return json.dumps({"success": True, "message": result}, ensure_ascii=False)
        except FileEditorError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)
