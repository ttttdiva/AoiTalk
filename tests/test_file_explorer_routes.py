from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.tools.absolute_filer_paths as absolute_filer_paths
from src.api.routes.file_explorer_routes import register_file_explorer_routes


class _DummyServer:
    def _enforce_cookie_auth(self, request: Request) -> None:
        return None

    async def _is_admin_user(self, request: Request) -> bool:
        return request.headers.get("x-test-admin") == "1"


def _build_client() -> TestClient:
    app = FastAPI()
    register_file_explorer_routes(app, _DummyServer())  # type: ignore[arg-type]
    return TestClient(app)


def test_filer_absolute_routes_require_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("FILER_ROOT_PATH", str(tmp_path))
    absolute_filer_paths._config_cache.clear()
    target = tmp_path / "sample.txt"
    target.write_text("secret", encoding="utf-8")

    client = _build_client()

    requests = [
        ("/api/filer/config", {}),
        ("/api/filer/browse", {"path": str(tmp_path)}),
        ("/api/filer/file", {"path": str(target)}),
        ("/api/filer/image-thumbnail", {"path": str(target)}),
        ("/api/filer/video-thumbnail", {"path": str(target)}),
    ]

    for path, params in requests:
        response = client.get(path, params=params)
        assert response.status_code == 403


def test_admin_can_browse_filer_absolute_route(tmp_path, monkeypatch):
    monkeypatch.setenv("FILER_ROOT_PATH", str(tmp_path))
    absolute_filer_paths._config_cache.clear()
    (tmp_path / "media").mkdir()

    client = _build_client()

    response = client.get(
        "/api/filer/browse",
        params={"path": str(tmp_path)},
        headers={"x-test-admin": "1"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
