"""ワークスペースファイル操作ツール"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP, service_module):
    """ワークスペースツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def list_workspace_files(path: str = "") -> str:
        """ワークスペース内のファイルとフォルダを一覧表示する

        Args:
            path: 表示するディレクトリのパス（空文字でルート）
        """
        result = service_module.list_directory(path)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def create_workspace_directory(path: str, name: str) -> str:
        """ワークスペースに新しいフォルダを作成する

        Args:
            path: 親ディレクトリのパス（空文字でルート）
            name: 作成するフォルダ名
        """
        result = service_module.create_directory(path, name)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def upload_workspace_file(path: str, filename: str, content_base64: str) -> str:
        """ワークスペースにファイルをアップロードする（Base64エンコード）

        Args:
            path: アップロード先ディレクトリのパス（空文字でルート）
            filename: ファイル名
            content_base64: Base64エンコードされたファイル内容
        """
        try:
            content = base64.b64decode(content_base64)
        except Exception:
            return json.dumps({"success": False, "error": "Base64デコードに失敗しました"}, ensure_ascii=False)

        result = service_module.upload_file(path, filename, content)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def read_workspace_file(path: str) -> str:
        """ワークスペースのファイル内容を読み取る

        Args:
            path: ファイルのパス
        """
        result = service_module.get_preview(path)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def delete_workspace_item(path: str) -> str:
        """ワークスペースのファイルまたはフォルダを削除する

        Args:
            path: 削除対象のパス
        """
        result = service_module.delete_item(path)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def move_workspace_item(src: str, dest: str) -> str:
        """ワークスペース内でファイルまたはフォルダを移動する

        Args:
            src: 移動元のパス
            dest: 移動先ディレクトリのパス
        """
        result = service_module.move_item(src, dest)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def get_workspace_file_info(path: str) -> str:
        """ワークスペースのファイル情報を取得する

        Args:
            path: ファイルまたはフォルダのパス
        """
        result = service_module.get_file_info(path)
        return json.dumps(result, ensure_ascii=False, default=str)
