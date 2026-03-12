"""OS操作用の設定読み込み"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_protected_paths_cache: Optional[List[str]] = None
_allowed_workspace_dirs_cache: Optional[List[str]] = None


def _get_config() -> dict:
    """config.yaml からos_operations設定を読み込む。"""
    try:
        from src.config import Config
        config = Config()
        return config.get('os_operations', {})
    except Exception as e:
        logger.warning(f"Failed to load config: {e}")
        return {}


def get_protected_paths() -> List[str]:
    global _protected_paths_cache
    if _protected_paths_cache is not None:
        return _protected_paths_cache

    os_ops_config = _get_config()
    paths = os_ops_config.get('protected_paths', [])
    _protected_paths_cache = paths if paths else []
    return _protected_paths_cache


def get_allowed_workspace_dirs() -> List[str]:
    global _allowed_workspace_dirs_cache
    if _allowed_workspace_dirs_cache is not None:
        return _allowed_workspace_dirs_cache

    os_ops_config = _get_config()
    dirs = os_ops_config.get('allowed_workspace_dirs', ['_users', '_projects'])
    _allowed_workspace_dirs_cache = dirs if dirs else []
    return _allowed_workspace_dirs_cache


def get_user_files_root() -> Path:
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    return Path(files_dir).resolve()


def resolve_path_for_user(path: str, user_id: Optional[str] = None) -> str:
    """相対パスをユーザーワークスペースに解決する。"""
    if os.path.isabs(path):
        return path

    user_files_root = get_user_files_root()

    if user_id:
        user_workspace = user_files_root / "_users" / f"user_{user_id}"
        user_workspace.mkdir(parents=True, exist_ok=True)
        return str(user_workspace / path)
    else:
        return str(user_files_root / path)


def is_path_protected(path: str) -> tuple:
    """パスが保護されているか確認する。"""
    protected_paths = get_protected_paths()
    if not protected_paths:
        return False, None

    try:
        target_path = Path(path).resolve()
        for protected in protected_paths:
            protected_path = Path(protected).resolve()
            try:
                target_path.relative_to(protected_path)
                return True, str(protected_path)
            except ValueError:
                continue
    except Exception as e:
        logger.warning(f"Error checking path protection for {path}: {e}")

    return False, None


def check_path_protection(path: str, operation: str) -> Optional[Dict[str, Any]]:
    """パス保護チェック。エラーdictまたはNoneを返す。"""
    try:
        target_path = Path(path).resolve()
        user_files_root = get_user_files_root()
        try:
            target_path.relative_to(user_files_root)
            return None
        except ValueError:
            pass
    except Exception:
        pass

    is_protected, matched_path = is_path_protected(path)
    if is_protected:
        return {
            "success": False,
            "error": f"操作拒否: パス '{path}' は保護されています。\n"
                     f"理由: '{matched_path}' 以下のファイルは {operation} できません。"
        }
    return None
