"""ECC (Extended Command Center) API routes.

後方互換のための薄いファサード。実装は `src/api/ecc/` パッケージへ分割済み。
既存の `from .ecc_routes import create_ecc_router` を壊さないために再エクスポートする。
"""

from __future__ import annotations

from .ecc import create_ecc_router

__all__ = ["create_ecc_router"]
