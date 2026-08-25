"""
FastAPI WebSocket API module

`create_web_interface` は遅延 import する。
このパッケージを eager import すると `web_interface` 経由で voice / Discord など
optional 依存を含むサーバー全体（約3500モジュール）が読み込まれ、
`from src.api.apps_routes import ...` のようなサブモジュール import だけでも
optional 依存が未インストールの環境では失敗してしまうため。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 型チェック時のみ実体を参照する
    from .web_interface import create_web_interface

__all__ = ['create_web_interface']


def __getattr__(name: str) -> Any:
    if name == 'create_web_interface':
        from .web_interface import create_web_interface

        return create_web_interface
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
