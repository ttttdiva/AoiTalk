"""
File Explorer Tools - LLM function tools for workspace file management.

Provides CRUD operations for managing files through LLM function calling.
"""

import base64
import os
from pathlib import Path
from typing import Any, Dict

from ..core import tool

from .file_explorer_service import (
    copy_item,
    create_directory,
    upload_file,
    delete_item,
    move_item,
    get_file_info,
    walk_workspace_tree,
)

try:
    from ...security.agent_run_scope import RunScopeViolation, get_current_run_scope
except ImportError:  # pragma: no cover - defensive for stripped builds
    RunScopeViolation = PermissionError  # type: ignore[assignment]

    def get_current_run_scope():  # type: ignore[no-redef]
        return None


def _run_scope_error(exc: Exception) -> Dict[str, Any]:
    """Return a machine-readable denial without leaking into the service layer."""

    return {
        "success": False,
        "error": str(exc),
        "error_code": "run_scope_violation",
        "retryable": False,
    }


def _scope_path(
    path: str,
    *,
    access: str,
    operation: str,
) -> tuple[str | None, Dict[str, Any] | None]:
    """Resolve one path through the active run contract, if one is bound.

    An unbound call deliberately returns ``(None, None)`` so the existing
    user/project ACL resolver remains authoritative for ordinary application
    callers.  A bound coding run must never fall back to that process-global
    workspace resolver.
    """

    scope = get_current_run_scope()
    if scope is None:
        return None, None
    try:
        if access == "read":
            resolved = scope.assert_read_allowed(path)
        elif access == "delete":
            resolved = scope.assert_delete_allowed(path)
        else:
            resolved = scope.assert_mutation_allowed(path, operation)
        return str(resolved), None
    except (RunScopeViolation, OSError, ValueError, TypeError) as exc:
        return None, _run_scope_error(exc)


def _scope_child_path(
    parent: str,
    name: str,
    *,
    operation: str,
) -> Dict[str, Any] | None:
    """Validate a service-created child before sanitisation can hide escape.

    The service intentionally strips separators from browser/LLM names.  That
    is useful for normal workspace calls but cannot be the run-scope boundary:
    validating the raw child first rejects ``../outside`` and absolute names
    rather than silently turning them into a different filename.
    """

    scope = get_current_run_scope()
    if scope is None:
        return None
    # The service treats both separators as path separators on all supported
    # platforms, so mirror that wire-format behaviour during preflight.
    candidate = Path(parent) / str(name or "").replace("\\", "/")
    try:
        scope.assert_mutation_allowed(candidate, operation)
    except (RunScopeViolation, OSError, ValueError, TypeError) as exc:
        return _run_scope_error(exc)
    return None


def _scope_move_paths(
    src: str,
    dest: str,
    *,
    operation: str,
) -> tuple[str | None, str | None, Dict[str, Any] | None]:
    """Validate both sides of a move/copy and the eventual child path."""

    scope = get_current_run_scope()
    if scope is None:
        return None, None, None
    try:
        if operation == "copy":
            source_path, destination_path = scope.assert_copy_allowed(src, dest)
        else:
            source_path, destination_path = scope.assert_move_allowed(src, dest)
        # move_item/copy_item place the source basename below ``dest``.  Check
        # that final path too; validating only the destination directory would
        # leave a name/reparse race at the service boundary.
        scope.assert_mutation_allowed(
            destination_path / source_path.name,
            operation,
        )
        return str(source_path), str(destination_path), None
    except (RunScopeViolation, OSError, ValueError, TypeError) as exc:
        return None, None, _run_scope_error(exc)


def _scope_trash_destination() -> Dict[str, Any] | None:
    """Ensure scoped deletes cannot fall back to a workspace-level trash."""

    scope = get_current_run_scope()
    if scope is None:
        return None
    try:
        from . import file_explorer_service

        trash_root = getattr(file_explorer_service, "_trash_root", None)
        if callable(trash_root):
            scope.assert_mutation_allowed(str(trash_root()), "delete-trash")
    except (RunScopeViolation, OSError, ValueError, TypeError) as exc:
        return _run_scope_error(exc)
    return None


def _check_permission_sync(tool_name: str, args: Dict[str, Any]) -> bool:
    from ..external_llm_permission import check_permission_sync

    return check_permission_sync(tool_name, args)


def _authorized_workspace_path(path: str, operation: str) -> tuple[str | None, Dict[str, Any] | None]:
    """LLM向けlegacy操作を、現在ユーザーが所有する保存領域へ解決する。"""

    # A coding run has an immutable repository boundary which must take
    # precedence over the interactive user-files resolver.  Without this
    # branch, a relative path could silently resolve into another user's
    # workspace before AgentRunScope sees it.
    scope_access = "read"
    if operation in {"削除", "移動"}:
        scope_access = "delete"
    elif operation not in {"読み取り"}:
        scope_access = "mutation"
    scoped_path, scope_error = _scope_path(
        path,
        access=scope_access,
        operation=operation,
    )
    if get_current_run_scope() is not None:
        return scoped_path, scope_error

    from ..os_operations.tools import (
        _check_user_permission,
        _get_user_files_root,
    )

    raw = str(path or "").strip()
    root = _get_user_files_root()
    if os.path.isabs(raw):
        target = Path(raw).resolve()
    else:
        target = (root / raw).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        return None, {"success": False, "error": "ワークスペース外のパスは指定できません。"}

    permission_error = _check_user_permission(str(target), operation)
    if permission_error:
        return None, permission_error
    return str(target), None


def _is_storage_scope_root(path: str) -> bool:
    """`_users/user_*` / `_projects/project_*` 自体への破壊操作を識別する。"""
    from ..os_operations.tools import _get_user_files_root

    try:
        parts = Path(path).resolve().relative_to(_get_user_files_root()).parts
    except ValueError:
        return False
    return (
        len(parts) == 2
        and parts[0] in {"_users", "_projects"}
        and parts[1].startswith(("user_", "project_"))
    )


def _enterprise_project_write_error(*paths: str) -> Dict[str, Any] | None:
    """Keep generic agent writes out of project storage in Enterprise.

    Project API writers perform the row-lock, strict usage scan, quota check,
    atomic write, and counter update as one protocol.  These legacy tools do
    not have a database transaction context, so accepting project paths here
    would silently bypass that protocol.
    """
    try:
        from ...features import Features

        if not Features.is_enterprise():
            return None
        from ..os_operations.tools import _get_user_files_root

        project_root = Path(os.path.abspath(_get_user_files_root())) / "_projects"
        for raw_path in paths:
            candidate = Path(os.path.abspath(str(raw_path or "")))
            try:
                candidate.relative_to(project_root)
            except ValueError:
                continue
            return {
                "success": False,
                "error": (
                    "Enterpriseではプロジェクト保存領域への汎用agent書き込みを"
                    "無効化しています。プロジェクトのファイルAPIを使用してください。"
                ),
            }
    except Exception:
        # A profile-detection failure must not turn this compatibility layer
        # into a broad write bypass.  Fail closed even if this helper is used
        # outside the normal registry path.
        return {
            "success": False,
            "error": (
                "Enterprise状態を安全に確認できないため、汎用agent書き込みを"
                "拒否しました。"
            ),
        }
    return None


@tool
def create_workspace_directory(path: str, name: str) -> Dict[str, Any]:
    """ワークスペースに新しいフォルダを作成する
    
    Args:
        path: 親ディレクトリのパス（空文字でルート）
        name: 作成するフォルダ名
        
    Returns:
        Dict[str, Any]: 作成結果
    """
    print(f"[Tool] create_workspace_directory が呼び出されました: path={path}, name={name}")
    if not _check_permission_sync(
        "create_workspace_directory",
        {"path": path, "name": name},
    ):
        return {"success": False, "error": "ユーザーによってフォルダ作成がキャンセルされました。"}
    if get_current_run_scope() is not None:
        resolved, error = _scope_path(
            path,
            access="mutation",
            operation="create_directory",
        )
        if not error and resolved:
            error = _scope_child_path(
                resolved,
                name,
                operation="create_directory",
            )
    else:
        resolved, error = _authorized_workspace_path(path, "作成")
    if error or not resolved:
        return error or {"success": False, "error": "パスを解決できませんでした。"}
    project_write_error = _enterprise_project_write_error(resolved)
    if project_write_error:
        return project_write_error
    return create_directory(resolved, name, is_admin=True)


@tool
def upload_workspace_file(path: str, filename: str, content_base64: str) -> Dict[str, Any]:
    """ワークスペースにファイルをアップロードする
    
    Base64エンコードされたファイル内容をアップロードします。
    
    Args:
        path: アップロード先ディレクトリのパス（空文字でルート）
        filename: ファイル名
        content_base64: Base64エンコードされたファイル内容
        
    Returns:
        Dict[str, Any]: アップロード結果
    """
    print(f"[Tool] upload_workspace_file が呼び出されました: path={path}, filename={filename}")
    if not _check_permission_sync(
        "upload_workspace_file",
        {"path": path, "filename": filename},
    ):
        return {"success": False, "error": "ユーザーによってファイル保存がキャンセルされました。"}
    try:
        content = base64.b64decode(content_base64)
    except Exception:
        return {"success": False, "error": "Base64デコードに失敗しました"}

    if get_current_run_scope() is not None:
        resolved, error = _scope_path(
            path,
            access="mutation",
            operation="upload",
        )
        if not error and resolved:
            error = _scope_child_path(resolved, filename, operation="upload")
    else:
        resolved, error = _authorized_workspace_path(path, "作成")
    if error or not resolved:
        return error or {"success": False, "error": "パスを解決できませんでした。"}
    project_write_error = _enterprise_project_write_error(resolved)
    if project_write_error:
        return project_write_error
    return upload_file(resolved, filename, content, is_admin=True)


@tool
def delete_workspace_item(path: str) -> Dict[str, Any]:
    """ワークスペースのファイルまたはフォルダを削除する
    
    Args:
        path: 削除対象のパス
        
    Returns:
        Dict[str, Any]: 削除結果
    """
    print(f"[Tool] delete_workspace_item が呼び出されました: path={path}")
    if not _check_permission_sync("delete_workspace_item", {"path": path}):
        return {"success": False, "error": "ユーザーによって削除がキャンセルされました。"}
    if get_current_run_scope() is not None:
        resolved, error = _scope_path(
            path,
            access="delete",
            operation="delete",
        )
        if not error:
            error = _scope_trash_destination()
    else:
        resolved, error = _authorized_workspace_path(path, "削除")
    if error or not resolved:
        return error or {"success": False, "error": "パスを解決できませんでした。"}
    project_write_error = _enterprise_project_write_error(resolved)
    if project_write_error:
        return project_write_error
    if not str(path or "").strip() or _is_storage_scope_root(resolved):
        return {
            "success": False,
            "error": "ユーザーまたはプロジェクトの保存領域ルートは削除できません。",
        }
    # Workspace files are user-owned content: a failed trash move must never
    # silently turn into an irreversible physical delete.  The service keeps
    # its default fail-soft mode for explicit external/admin absolute paths.
    return delete_item(resolved, is_admin=True, require_trash=True)


@tool
def move_workspace_item(src: str, dest: str) -> Dict[str, Any]:
    """ワークスペース内でファイルまたはフォルダを移動する
    
    Args:
        src: 移動元のパス
        dest: 移動先ディレクトリのパス
        
    Returns:
        Dict[str, Any]: 移動結果
    """
    print(f"[Tool] move_workspace_item が呼び出されました: src={src}, dest={dest}")
    if not _check_permission_sync("move_workspace_item", {"src": src, "dest": dest}):
        return {"success": False, "error": "ユーザーによって移動がキャンセルされました。"}
    if get_current_run_scope() is not None:
        resolved_src, resolved_dest, scope_error = _scope_move_paths(
            src,
            dest,
            operation="move",
        )
        if scope_error:
            return scope_error
        if not resolved_src or not resolved_dest:
            return {"success": False, "error": "移動元または移動先を解決できませんでした。"}
    else:
        resolved_src, src_error = _authorized_workspace_path(src, "移動")
        if src_error or not resolved_src:
            return src_error or {"success": False, "error": "移動元を解決できませんでした。"}
        resolved_dest, dest_error = _authorized_workspace_path(dest, "移動")
        if dest_error or not resolved_dest:
            return dest_error or {"success": False, "error": "移動先を解決できませんでした。"}
    if not str(src or "").strip() or _is_storage_scope_root(resolved_src):
        return {
            "success": False,
            "error": "ユーザーまたはプロジェクトの保存領域ルートは移動できません。",
        }
    project_write_error = _enterprise_project_write_error(resolved_src, resolved_dest)
    if project_write_error:
        return project_write_error
    return move_item(resolved_src, resolved_dest, is_admin=True)


def _copy_workspace_item_impl(src: str, dest: str) -> Dict[str, Any]:
    """Copy after the outer tool permission has already been decided."""
    if get_current_run_scope() is not None:
        resolved_src, resolved_dest, scope_error = _scope_move_paths(
            src,
            dest,
            operation="copy",
        )
        if scope_error:
            return scope_error
        if not resolved_src or not resolved_dest:
            return {"success": False, "error": "コピー元またはコピー先を解決できませんでした。"}
    else:
        resolved_src, src_error = _authorized_workspace_path(src, "読み取り")
        if src_error or not resolved_src:
            return src_error or {"success": False, "error": "コピー元を解決できませんでした。"}
        resolved_dest, dest_error = _authorized_workspace_path(dest, "作成")
        if dest_error or not resolved_dest:
            return dest_error or {"success": False, "error": "コピー先を解決できませんでした。"}
    if not str(src or "").strip() or _is_storage_scope_root(resolved_src):
        return {"success": False, "error": "保存領域ルートはコピーできません。"}
    project_write_error = _enterprise_project_write_error(resolved_dest)
    if project_write_error:
        return project_write_error
    return copy_item(
        resolved_src,
        resolved_dest,
        is_admin=True,
        conflict_strategy="reuse_identical",
    )


@tool
def copy_workspace_item(src: str, dest: str) -> Dict[str, Any]:
    """ワークスペース内のファイルを冪等にコピーする

    同名・同内容のファイルが既にあれば成功として再利用し、内容が異なる
    同名ファイルは上書きも自動改名もしません。

    Args:
        src: コピー元ファイルのパス
        dest: コピー先ディレクトリのパス
    """
    if not _check_permission_sync("copy_workspace_item", {"src": src, "dest": dest}):
        return {"success": False, "error": "ユーザーによってコピーがキャンセルされました。"}
    return _copy_workspace_item_impl(src, dest)


@tool
def list_workspace_tree(
    path: str = "",
    max_depth: int = 3,
    include_files: bool = True,
    max_entries: int = 300,
) -> Dict[str, Any]:
    """ワークスペースのフォルダ構成を1回で上限付き再帰取得する

    Args:
        path: 調査するワークスペース内ディレクトリ
        max_depth: 再帰する深さ（1〜8）
        include_files: ファイルも含めるか
        max_entries: 最大返却件数（1〜1000）
    """
    resolved, error = _authorized_workspace_path(path, "読み取り")
    if error or not resolved:
        return error or {"success": False, "error": "パスを解決できませんでした。"}
    return walk_workspace_tree(
        resolved,
        max_depth=max_depth,
        include_files=include_files,
        max_entries=max_entries,
        is_admin=True,
    )


@tool
def get_workspace_file_info(path: str) -> Dict[str, Any]:
    """ワークスペースのファイル情報を取得する
    
    ファイルサイズ、作成日時、更新日時などの詳細情報を取得します。
    
    Args:
        path: ファイルまたはフォルダのパス
        
    Returns:
        Dict[str, Any]: ファイル/フォルダの詳細情報
    """
    print(f"[Tool] get_workspace_file_info が呼び出されました: path={path}")
    resolved, error = _authorized_workspace_path(path, "読み取り")
    if error or not resolved:
        return error or {"success": False, "error": "パスを解決できませんでした。"}
    return get_file_info(resolved, is_admin=True)
