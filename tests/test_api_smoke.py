import asyncio
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Config
from src.api.server import create_web_interface


os.environ.setdefault("AOITALK_WEB_AUTH_SECRET", "test-secret")


def test_fastapi_app_instantiates():
    config = Config()

    async def build_app():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    app = asyncio.run(build_app())
    assert isinstance(app, FastAPI)


def test_frontend_catchall_is_registered_after_project_api_routes():
    config = Config()

    async def build_app():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    app = asyncio.run(build_app())
    paths = [getattr(route, "path", "") for route in app.routes]

    assert "/api/projects" in paths
    assert "/{frontend_path:path}" in paths
    assert paths.index("/api/projects") < paths.index("/{frontend_path:path}")


def test_task_recurrence_route_is_registered_before_frontend_catchall():
    config = Config()

    async def build_app():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    app = asyncio.run(build_app())
    paths = [getattr(route, "path", "") for route in app.routes]

    assert "/api/tasks/{task_id}/recurrence" in paths
    assert "/{frontend_path:path}" in paths
    assert paths.index("/api/tasks/{task_id}/recurrence") < paths.index(
        "/{frontend_path:path}"
    )


def test_runtime_features_endpoint_reports_webui_always_on():
    config = Config()

    async def build_app():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    app = asyncio.run(build_app())
    client = TestClient(app)

    response = client.get("/api/runtime/features")

    assert response.status_code == 200
    data = response.json()
    assert data["web_ui_always_on"] is True
    assert data["features"]["web_ui"] is True
    assert data["discord_bot_service"]["state"] in {
        "stopped",
        "starting",
        "running",
        "stopping",
        "failed",
    }
    assert "web_text" in data["input_adapters"]
