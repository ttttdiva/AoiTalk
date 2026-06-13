import asyncio
import os

from src.config import Config
from src.api.server import create_web_interface


os.environ.setdefault("AOITALK_WEB_AUTH_SECRET", "test-secret")


def _build_app():
    config = Config()

    async def build():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    return asyncio.run(build())


def test_long_lived_token_routes_registered_before_catchall():
    app = _build_app()
    paths = [getattr(route, "path", "") for route in app.routes]

    assert "/api/auth/long-lived-tokens" in paths
    assert "/api/auth/long-lived-tokens/{token_id}" in paths
    assert "/{frontend_path:path}" in paths
    assert paths.index("/api/auth/long-lived-tokens") < paths.index(
        "/{frontend_path:path}"
    )


def test_long_lived_token_routes_have_expected_methods():
    app = _build_app()
    methods_by_path: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/auth/long-lived-tokens"):
            methods_by_path.setdefault(path, set()).update(
                getattr(route, "methods", set()) or set()
            )

    assert "GET" in methods_by_path["/api/auth/long-lived-tokens"]
    assert "POST" in methods_by_path["/api/auth/long-lived-tokens"]
    assert "DELETE" in methods_by_path["/api/auth/long-lived-tokens/{token_id}"]
