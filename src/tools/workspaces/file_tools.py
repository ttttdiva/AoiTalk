"""
User Files tools for AoiTalk - LLM function tools for file management.

Provides CRUD operations for managing user files through LLM function calling.
"""

from typing import Any, Dict

from agents import function_tool

from ..external_llm_permission import check_permission_sync

from .file_service import (
    upload_file_impl,
    download_file_impl,
    list_files_impl,
    delete_file_impl,
    get_file_info_impl,
)


@function_tool
def upload_user_file(filename: str, content_base64: str) -> Dict[str, Any]:
    """ユーザーファイルをアップロードする
    
    Base64エンコードされたファイル内容をアップロードします。
    任意のファイル形式に対応（一部危険な拡張子は除く）。
    
    Args:
        filename: ファイル名（例：「report.pdf」「image.png」）
        content_base64: Base64エンコードされたファイル内容
        
    Returns:
        Dict[str, Any]: アップロード結果
    """
    print(f"[Tool] upload_user_file が呼び出されました: {filename}")
    if not check_permission_sync("upload_user_file", {"filename": filename}):
        return {"success": False, "error": "ユーザーによってファイル保存がキャンセルされました。"}
    return upload_file_impl(filename, content_base64)


@function_tool
def download_user_file(filename: str) -> Dict[str, Any]:
    """ユーザーファイルをダウンロードする
    
    指定されたファイルの内容をBase64エンコードで取得します。
    
    Args:
        filename: ダウンロードするファイル名
        
    Returns:
        Dict[str, Any]: ファイル内容（Base64）とメタデータ
    """
    print(f"[Tool] download_user_file が呼び出されました: {filename}")
    return download_file_impl(filename)


@function_tool
def list_user_files() -> Dict[str, Any]:
    """保存されているユーザーファイルの一覧を取得する
    
    Returns:
        Dict[str, Any]: ファイル一覧
    """
    print("[Tool] list_user_files が呼び出されました")
    return list_files_impl()


@function_tool
def delete_user_file(filename: str) -> Dict[str, Any]:
    """ユーザーファイルを削除する
    
    Args:
        filename: 削除するファイル名
        
    Returns:
        Dict[str, Any]: 削除結果
    """
    print(f"[Tool] delete_user_file が呼び出されました: {filename}")
    if not check_permission_sync("delete_user_file", {"filename": filename}):
        return {"success": False, "error": "ユーザーによってファイル削除がキャンセルされました。"}
    return delete_file_impl(filename)


@function_tool
def get_user_file_info(filename: str) -> Dict[str, Any]:
    """ユーザーファイルのメタデータを取得する
    
    ファイルサイズ、作成日時、更新日時などの情報を取得します。
    
    Args:
        filename: 情報を取得するファイル名
        
    Returns:
        Dict[str, Any]: ファイルメタデータ
    """
    print(f"[Tool] get_user_file_info が呼び出されました: {filename}")
    return get_file_info_impl(filename)
