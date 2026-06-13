from typing import Callable

from fastapi import Request


def cookie_auth_dependency(enforce_cookie_auth: Callable[[Request], None]):
    """Build a FastAPI dependency that delegates to the app cookie auth guard."""

    def require_auth(request: Request) -> None:
        enforce_cookie_auth(request)

    return require_auth
