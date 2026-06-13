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


def test_remote_proxy_routes_registered_before_catchall():
    app = _build_app()
    paths = [getattr(route, "path", "") for route in app.routes]

    expected = [
        "/api/remote-servers/{profile_id}/tasks",
        "/api/remote-servers/{profile_id}/tasks/{task_id}",
        "/api/remote-servers/{profile_id}/task-occurrences",
        "/api/remote-servers/{profile_id}/spaces",
        "/api/remote-servers/{profile_id}/tasks/{task_id}/comments",
    ]
    for path in expected:
        assert path in paths, path
        assert paths.index(path) < paths.index("/{frontend_path:path}")


def test_remote_proxy_routes_have_expected_methods():
    app = _build_app()
    methods_by_path: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/remote-servers/{profile_id}/"):
            methods_by_path.setdefault(path, set()).update(
                getattr(route, "methods", set()) or set()
            )

    assert "GET" in methods_by_path["/api/remote-servers/{profile_id}/tasks"]
    assert "GET" in methods_by_path[
        "/api/remote-servers/{profile_id}/task-occurrences"
    ]
    assert "PATCH" in methods_by_path[
        "/api/remote-servers/{profile_id}/tasks/{task_id}"
    ]
    assert "POST" in methods_by_path[
        "/api/remote-servers/{profile_id}/tasks/{task_id}/comments"
    ]
