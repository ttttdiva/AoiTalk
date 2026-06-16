from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.app_factory_routes import create_app_factory_router
from src.services.app_factory_service import create_app_factory_artifact


def _build_client(config: dict) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_app_factory_router(
            require_auth_dependency=lambda: None,
            config=config,
        )
    )
    return TestClient(app)


def test_app_factory_download_route_returns_zip_attachment(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}
    artifact = create_app_factory_artifact(
        kind="local_web",
        title="Quick Tool",
        description="A local web tool.",
        config=config,
    )
    client = _build_client(config)

    response = client.get(
        f"/api/app-factory/artifacts/{artifact.artifact_id}/download"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"PK")


def test_app_factory_preview_route_returns_html(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}
    artifact = create_app_factory_artifact(
        kind="hosted_web",
        title="Hosted Tool",
        app_html="<!doctype html><title>Hosted Tool</title><main>Ready</main>",
        config=config,
    )
    client = _build_client(config)

    response = client.get(f"/api/app-factory/artifacts/{artifact.artifact_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Hosted Tool" in response.text


def test_app_factory_list_and_delete_routes(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}
    artifact = create_app_factory_artifact(
        kind="bat_macro",
        title="Disposable Macro",
        config=config,
    )
    client = _build_client(config)

    list_response = client.get("/api/app-factory/artifacts")
    assert list_response.status_code == 200
    artifacts = list_response.json()["artifacts"]
    assert [item["artifact_id"] for item in artifacts] == [artifact.artifact_id]
    assert artifacts[0]["download_available"] is True

    delete_response = client.delete(
        f"/api/app-factory/artifacts/{artifact.artifact_id}"
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["artifact"]["artifact_id"] == artifact.artifact_id

    download_response = client.get(
        f"/api/app-factory/artifacts/{artifact.artifact_id}/download"
    )
    assert download_response.status_code == 404
