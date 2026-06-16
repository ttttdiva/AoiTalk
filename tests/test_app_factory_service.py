from __future__ import annotations

import zipfile

from src.services.app_factory_service import (
    create_app_factory_artifact,
    delete_app_factory_artifact,
    list_app_factory_artifacts,
    load_artifact_manifest,
)


def test_app_factory_creates_downloadable_batch_package(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}

    artifact = create_app_factory_artifact(
        kind="bat_macro",
        title="Daily Report",
        description="Prepare a daily report folder.",
        requirements="Create output files under output/.",
        batch_script="@echo off\r\necho ready\r\n",
        config=config,
    )

    assert artifact.zip_path.exists()
    assert artifact.root_dir.is_relative_to(tmp_path)
    assert artifact.download_url.endswith(f"/{artifact.artifact_id}/download")
    assert artifact.preview_url is None

    manifest = load_artifact_manifest(artifact.artifact_id, config=config)
    assert manifest["kind"] == "bat_macro"
    assert manifest["download_filename"] == "daily-report.zip"

    with zipfile.ZipFile(artifact.zip_path) as archive:
        names = set(archive.namelist())
        assert "run.bat" in names
        assert "scripts/macro.bat" in names
        assert "manifest.json" in names
        assert "input/README.txt" in names
        assert "output/README.txt" in names


def test_app_factory_creates_hosted_web_preview_package(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}

    artifact = create_app_factory_artifact(
        kind="aoitalk_webui",
        title="Inspection Form",
        description="Simple inspection form.",
        app_html="<!doctype html><title>Inspection</title><main>Ready</main>",
        config=config,
    )

    assert artifact.kind == "hosted_web"
    assert artifact.root_dir.is_relative_to(tmp_path)
    assert artifact.preview_url is not None
    assert (artifact.package_dir / "app" / "index.html").read_text(
        encoding="utf-8"
    ).startswith("<!doctype html>")


def test_app_factory_accepts_multi_file_web_package(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}

    artifact = create_app_factory_artifact(
        kind="local_web",
        title="Multi File App",
        extra_files={
            "app/index.html": "<!doctype html><script src=\"main.js\"></script>",
            "app/main.js": "document.body.dataset.ready = '1';",
            "app/style.css": "body { font-family: sans-serif; }",
            "docs/notes.md": "# Notes\n",
        },
        config=config,
    )

    assert "app/main.js" in artifact.files
    assert "app/style.css" in artifact.files
    assert (artifact.package_dir / "app" / "main.js").read_text(
        encoding="utf-8"
    ).startswith("document.body")
    with zipfile.ZipFile(artifact.zip_path) as archive:
        assert "app/main.js" in archive.namelist()


def test_app_factory_rejects_unsafe_extra_file_path(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}

    try:
        create_app_factory_artifact(
            kind="local_web",
            title="Bad App",
            extra_files={"../outside.txt": "bad"},
            config=config,
        )
    except ValueError as exc:
        assert "Unsafe package path" in str(exc)
    else:
        raise AssertionError("unsafe extra path should be rejected")


def test_app_factory_lists_and_deletes_artifacts(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}

    first = create_app_factory_artifact(
        kind="bat_macro",
        title="First Macro",
        config=config,
    )
    second = create_app_factory_artifact(
        kind="local_web",
        title="Second App",
        config=config,
    )

    listed = list_app_factory_artifacts(config=config)
    assert [item["artifact_id"] for item in listed] == [
        second.artifact_id,
        first.artifact_id,
    ]
    assert listed[0]["download_available"] is True
    assert listed[0]["download_size_bytes"] > 0

    deleted = delete_app_factory_artifact(first.artifact_id, config=config)
    assert deleted["artifact_id"] == first.artifact_id
    assert not first.root_dir.exists()
    assert [item["artifact_id"] for item in list_app_factory_artifacts(config=config)] == [
        second.artifact_id
    ]
