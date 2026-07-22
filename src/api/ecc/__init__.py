"""ECC (Extended Command Center) API ルートパッケージ。

`create_ecc_router()` は各サブルーターを組み立てて集約する。
実際のエンドポイント定義は機能別モジュールに分割されている。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..ecc_helpers import ecc_cookie_auth_dependency
from .characters_routes import build_characters_router
from .common import make_get_user_id
from .context_routes import build_context_router
from .mcp_routes import build_mcp_router
from .memory_routes import build_memory_router
from .quality_routes import build_quality_router
from .skill_routes import build_skill_router
from .usage_routes import build_usage_router
from .worldbook_routes import build_worldbook_router

__all__ = ["create_ecc_router"]


def create_ecc_router(app_instance: Any) -> APIRouter:
    """ECC 全機能の APIRouter を作成する。

    Args:
        app_instance: WebInterface インスタンス。
            認証・DB・MCP プラグイン等へのアクセスに使用する。

    Returns:
        全 ECC ルートを含む APIRouter。
    """

    # ── 認証依存関数・共有ヘルパー ──
    require_auth = ecc_cookie_auth_dependency(app_instance)
    get_user_id = make_get_user_id(app_instance)

    root_router = APIRouter()
    root_router.include_router(build_characters_router(require_auth))
    root_router.include_router(build_usage_router(app_instance, require_auth))
    root_router.include_router(build_skill_router(require_auth))
    root_router.include_router(build_mcp_router(app_instance, require_auth))
    root_router.include_router(build_quality_router(require_auth))
    root_router.include_router(build_memory_router(require_auth, get_user_id))
    root_router.include_router(build_context_router(require_auth, get_user_id))
    root_router.include_router(build_worldbook_router(require_auth))

    return root_router
