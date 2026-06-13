"""
File Explorer Tools - LLM function tools for workspace file management.

Provides CRUD operations for managing files through LLM function calling.
"""

import base64
from typing import Any, Dict

from ..core import tool as function_tool
from ..external_llm_permission import check_permission_sync

from .file_explorer_service import (
    list_directory,
    create_directory,
    upload_file,
    download_file,
    delete_item,
    find_workspace_items as service_find_workspace_items,
    move_item,
    get_file_info,
    get_preview,
    inspect_workspace_tree as service_inspect_workspace_tree,
)


@function_tool
def list_workspace_files(path: str = "") -> Dict[str, Any]:
    """ワークスペース内のファイルとフォルダを一覧表示する
    
    Args:
        path: 表示するディレクトリのパス（空文字でルート）
        
    Returns:
        Dict[str, Any]: ディレクトリ内容（フォルダ一覧、ファイル一覧）
    """
    print(f"[Tool] list_workspace_files が呼び出されました: path={path}")
    return list_directory(path)


@function_tool
def find_workspace_items(
    query: str,
    path: str = "",
    include_dirs: bool = True,
    include_files: bool = True,
    max_results: int = 50,
) -> Dict[str, Any]:
    """Search workspace files and folders by name."""
    print(
        "[Tool] find_workspace_items called: "
        f"query={query}, path={path}, include_dirs={include_dirs}, "
        f"include_files={include_files}, max_results={max_results}"
    )
    return service_find_workspace_items(
        query=query,
        path=path,
        include_dirs=include_dirs,
        include_files=include_files,
        max_results=max_results,
    )


@function_tool
def inspect_workspace_tree(
    path: str = "",
    max_depth: int = 3,
    include_files: bool = True,
    max_entries: int = 300,
) -> Dict[str, Any]:
    """Return a bounded recursive inventory of a workspace folder."""
    print(
        "[Tool] inspect_workspace_tree called: "
        f"path={path}, max_depth={max_depth}, include_files={include_files}, "
        f"max_entries={max_entries}"
    )
    return service_inspect_workspace_tree(
        path=path,
        max_depth=max_depth,
        include_files=include_files,
        max_entries=max_entries,
    )


@function_tool
def create_workspace_directory(path: str, name: str) -> Dict[str, Any]:
    """ワークスペースに新しいフォルダを作成する
    
    Args:
        path: 親ディレクトリのパス（空文字でルート）
        name: 作成するフォルダ名
        
    Returns:
        Dict[str, Any]: 作成結果
    """
    print(f"[Tool] create_workspace_directory が呼び出されました: path={path}, name={name}")
    if not check_permission_sync(
        "create_workspace_directory",
        {"path": path, "name": name},
    ):
        return {"success": False, "error": "ユーザーによってフォルダ作成がキャンセルされました。"}
    return create_directory(path, name)


@function_tool
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
    if not check_permission_sync(
        "upload_workspace_file",
        {"path": path, "filename": filename},
    ):
        return {"success": False, "error": "ユーザーによってファイル保存がキャンセルされました。"}
    try:
        content = base64.b64decode(content_base64)
    except Exception:
        return {"success": False, "error": "Base64デコードに失敗しました"}
    
    return upload_file(path, filename, content)


@function_tool
def read_workspace_file(path: str) -> Dict[str, Any]:
    """ワークスペースのファイル内容を読み取る
    
    テキストファイルは内容を、画像はBase64データを、
    Officeファイルは変換後のテキストを返します。
    
    Args:
        path: ファイルのパス
        
    Returns:
        Dict[str, Any]: ファイル内容とメタデータ
    """
    print(f"[Tool] read_workspace_file が呼び出されました: path={path}")
    return get_preview(path)


@function_tool
def delete_workspace_item(path: str) -> Dict[str, Any]:
    """ワークスペースのファイルまたはフォルダを削除する
    
    Args:
        path: 削除対象のパス
        
    Returns:
        Dict[str, Any]: 削除結果
    """
    print(f"[Tool] delete_workspace_item が呼び出されました: path={path}")
    if not check_permission_sync("delete_workspace_item", {"path": path}):
        return {"success": False, "error": "ユーザーによって削除がキャンセルされました。"}
    return delete_item(path)


@function_tool
def move_workspace_item(src: str, dest: str) -> Dict[str, Any]:
    """ワークスペース内でファイルまたはフォルダを移動する
    
    Args:
        src: 移動元のパス
        dest: 移動先ディレクトリのパス
        
    Returns:
        Dict[str, Any]: 移動結果
    """
    print(f"[Tool] move_workspace_item が呼び出されました: src={src}, dest={dest}")
    if not check_permission_sync("move_workspace_item", {"src": src, "dest": dest}):
        return {"success": False, "error": "ユーザーによって移動がキャンセルされました。"}
    return move_item(src, dest)


@function_tool
def get_workspace_file_info(path: str) -> Dict[str, Any]:
    """ワークスペースのファイル情報を取得する
    
    ファイルサイズ、作成日時、更新日時などの詳細情報を取得します。
    
    Args:
        path: ファイルまたはフォルダのパス
        
    Returns:
        Dict[str, Any]: ファイル/フォルダの詳細情報
    """
    print(f"[Tool] get_workspace_file_info が呼び出されました: path={path}")
    return get_file_info(path)
