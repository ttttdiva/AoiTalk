from __future__ import annotations

import json
import zipfile

from src.services.app_factory_service import get_app_factory_root
from src.tools.app_factory import create_instant_app_package, set_app_factory_tool_config


def test_create_instant_app_package_accepts_files_json(tmp_path):
    config = {"app_factory": {"artifact_dir": str(tmp_path)}}
    set_app_factory_tool_config(config)

    result = create_instant_app_package.function(
        kind="webui_app",
        title="Tool Generated",
        files_json=json.dumps(
            {
                "app/index.html": "<!doctype html><title>Tool Generated</title>",
                "app/main.js": "console.log('ready');",
            }
        ),
    )

    assert "Instant app package created." in result
    artifact_id = result.split("`")[1]
    zip_path = get_app_factory_root(config) / artifact_id / "tool-generated.zip"
    with zipfile.ZipFile(zip_path) as archive:
        assert "app/main.js" in archive.namelist()
