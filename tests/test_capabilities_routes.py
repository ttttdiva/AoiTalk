import asyncio
import os

from fastapi.testclient import TestClient

from src.config import Config
from src.api.server import create_web_interface


os.environ.setdefault("AOITALK_WEB_AUTH_SECRET", "test-secret")


def _build_app():
    config = Config()

    async def build():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    return asyncio.run(build())


def test_capabilities_route_registered():
    app = _build_app()
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/api/capabilities" in paths


def test_capabilities_requires_auth():
    app = _build_app()
    client = TestClient(app)

    # 認証なしでは保護される（接続テスト時のトークン検証を兼ねる）
    response = client.get("/api/capabilities")
    assert response.status_code == 401
