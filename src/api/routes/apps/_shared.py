"""Apps ルーター分割で共有する payload と実行時コンテキスト。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel


class AppGrantPayload(BaseModel):
    user_id: str | None = None
    project_id: str | None = None
    permission: str = "viewer"


@dataclass
class AppRouterContext:
    """抽出済み Apps endpoint が必要とする依存と helper の最小セット。"""

    get_db_manager: Callable
    require_auth_dependency: Any
    current_user: Callable
    require_app: Callable
    uuid: Callable
    user_id: Callable
    error: Callable
