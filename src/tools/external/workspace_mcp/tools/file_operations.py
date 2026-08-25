"""ワークスペースファイル操作ツール"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _scope_path(path: str, *, access: str, operation: str) -> tuple[str | None, dict | None]:
    """Return a canonical run-scoped path, or preserve legacy MCP resolution."""

    try:
        from src.security.agent_run_scope import RunScopeViolation, get_current_run_scope
    except ImportError:  # pragma: no cover - defensive for stripped builds
        return path, None

    scope = get_current_run_scope()
    if scope is None:
        return path, None
    try:
        if access == "read":
            resolved = scope.assert_read_allowed(path)
        elif access == "delete":
            resolved = scope.assert_delete_allowed(path)
        else:
            resolved = scope.assert_mutation_allowed(path, operation)
        return str(resolved), None
    except RunScopeViolation as exc:
        return None, {"success": False, "error": str(exc)}


def _run_scope_active() -> bool:
    """Whether this MCP call is running under an explicit repository scope."""

    try:
        from src.security.agent_run_scope import get_current_run_scope
    except ImportError:  # pragma: no cover - defensive for stripped builds
        return False
    return get_current_run_scope() is not None


def _service_call(
    service_module: Any,
    method: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a workspace service while preserving legacy mock/signature calls."""

    function = getattr(service_module, method)
    if _run_scope_active():
        return function(*args, is_admin=True, **kwargs)
    return function(*args, **kwargs)


def _assert_delete_destination_scope(service_module: Any) -> dict | None:
    """Validate the workspace service's trash destination for scoped deletes."""

    if not _run_scope_active():
        return None
    trash_root = getattr(service_module, "_trash_root", None)
    if not callable(trash_root):
        # Test doubles and alternate service implementations may physically
        # delete instead of trashing; the source path check remains authoritative.
        return None
    try:
        from src.security.agent_run_scope import RunScopeViolation, get_current_run_scope

        scope = get_current_run_scope()
        if scope is None:
            return None
        # Deletion in File Explorer normally moves into `.trash`, which is a
        # second mutation destination.  Validate that destination too; a
        # selected repository nested under a larger workspace must not leak a
        # deleted file into the workspace-level trash outside the run root.
        scope.assert_mutation_allowed(str(trash_root()), "delete-trash")
    except RunScopeViolation as exc:
        return {"success": False, "error": str(exc)}
    return None


def register(mcp: FastMCP, service_module):
    """ワークスペースツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def list_directory(path: str = "") -> str:
        """ワークスペース内のファイルとフォルダを一覧表示する

        Args:
            path: 表示するディレクトリのパス（空文字でルート）
        """
        path, error = _scope_path(path, access="read", operation="read")
        if error:
            return json.dumps(error, ensure_ascii=False)
        result = _service_call(service_module, "list_directory", path)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def create_workspace_directory(path: str, name: str) -> str:
        """ワークスペースに新しいフォルダを作成する

        Args:
            path: 親ディレクトリのパス（空文字でルート）
            name: 作成するフォルダ名
        """
        scoped_path, error = _scope_path(path, access="mutation", operation="create_directory")
        if error:
            return json.dumps(error, ensure_ascii=False)
        try:
            # Validate the final child as well as its parent; otherwise a
            # ``name=../outside`` payload could escape after a safe parent
            # preflight.
            from src.security.agent_run_scope import get_current_run_scope

            scope = get_current_run_scope()
            if scope is not None:
                scope.assert_mutation_allowed(
                    Path(scoped_path) / name,
                    "create_directory",
                )
        except Exception as exc:
            if isinstance(exc, PermissionError):
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            raise
        result = _service_call(service_module, "create_directory", scoped_path, name)
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

        scoped_path, error = _scope_path(path, access="mutation", operation="upload")
        if error:
            return json.dumps(error, ensure_ascii=False)
        try:
            from src.security.agent_run_scope import get_current_run_scope

            scope = get_current_run_scope()
            if scope is not None:
                scope.assert_mutation_allowed(
                    Path(scoped_path) / filename,
                    "upload",
                )
        except Exception as exc:
            if isinstance(exc, PermissionError):
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            raise
        result = _service_call(
            service_module,
            "upload_file",
            scoped_path,
            filename,
            content,
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def read_file(path: str) -> str:
        """ワークスペースのファイル内容を読み取る

        Args:
            path: ファイルのパス
        """
        path, error = _scope_path(path, access="read", operation="read")
        if error:
            return json.dumps(error, ensure_ascii=False)
        result = _service_call(service_module, "get_preview", path)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def delete_workspace_item(path: str) -> str:
        """ワークスペースのファイルまたはフォルダを削除する

        Args:
            path: 削除対象のパス
        """
        path, error = _scope_path(path, access="delete", operation="delete")
        if error:
            return json.dumps(error, ensure_ascii=False)
        trash_error = _assert_delete_destination_scope(service_module)
        if trash_error:
            return json.dumps(trash_error, ensure_ascii=False)
        # This is a first-party workspace mutation.  Never make a failed trash
        # move irreversible; the service still keeps its default physical
        # delete behavior for explicit external/admin absolute paths.
        result = _service_call(
            service_module,
            "delete_item",
            path,
            require_trash=True,
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def move_workspace_item(src: str, dest: str) -> str:
        """ワークスペース内でファイルまたはフォルダを移動する

        Args:
            src: 移動元のパス
            dest: 移動先ディレクトリのパス
        """
        scoped_src, error = _scope_path(src, access="delete", operation="move")
        if error:
            return json.dumps(error, ensure_ascii=False)
        scoped_dest, error = _scope_path(dest, access="mutation", operation="move")
        if error:
            return json.dumps(error, ensure_ascii=False)
        result = _service_call(service_module, "move_item", scoped_src, scoped_dest)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.tool()
    async def get_workspace_file_info(path: str) -> str:
        """ワークスペースのファイル情報を取得する

        Args:
            path: ファイルまたはフォルダのパス
        """
        path, error = _scope_path(path, access="read", operation="read")
        if error:
            return json.dumps(error, ensure_ascii=False)
        result = _service_call(service_module, "get_file_info", path)
        return json.dumps(result, ensure_ascii=False, default=str)
