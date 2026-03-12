"""
File Explorer Tools - LLM function tools for workspace file management.

Provides CRUD operations for managing files through LLM function calling.
"""

import base64
from typing import Any, Dict

from ..core import tool as function_tool

from .file_explorer_service import (
    list_directory,
    create_directory,
    upload_file,
    download_file,
    delete_item,
    move_item,
    get_file_info,
    get_preview,
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
def create_workspace_directory(path: str, name: str) -> Dict[str, Any]:
    """ワークスペースに新しいフォルダを作成する
    
    Args:
        path: 親ディレクトリのパス（空文字でルート）
        name: 作成するフォルダ名
        
    Returns:
        Dict[str, Any]: 作成結果
    """
    print(f"[Tool] create_workspace_directory が呼び出されました: path={path}, name={name}")
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
