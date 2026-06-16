"""Tools for creating chat-downloadable instant app packages."""

from __future__ import annotations

import json
from typing import Any

from .core import tool
from ..services.app_factory_service import create_app_factory_artifact


_runtime_config: Any | None = None


def set_app_factory_tool_config(config: Any | None) -> None:
    global _runtime_config
    _runtime_config = config


@tool
def create_instant_app_package(
    kind: str,
    title: str,
    description: str = "",
    requirements: str = "",
    html: str = "",
    batch_script: str = "",
    files_json: str = "",
) -> str:
    """Create a downloadable instant WebUI app or .bat macro package.

    Args:
        kind: local_web, hosted_web, or bat_macro. Aliases such as webui_app,
            aoitalk_webui, macro, and bat are accepted.
        title: Short user-facing package title.
        description: What the package is for.
        requirements: Functional notes or acceptance criteria to include.
        html: Optional complete HTML for WebUI app packages.
        batch_script: Optional Windows batch script for bat_macro packages.
        files_json: Optional JSON object mapping relative package paths to file
            contents, or a JSON array of {"path": "...", "content": "..."}
            objects. Use this for multi-file apps/macros.
    """
    try:
        extra_files = _parse_files_json(files_json)
        artifact = create_app_factory_artifact(
            kind=kind,
            title=title,
            description=description,
            requirements=requirements,
            app_html=html,
            batch_script=batch_script,
            extra_files=extra_files,
            config=_runtime_config,
        )
    except Exception as exc:
        return f"Failed to create instant app package: {exc}"

    lines = [
        "Instant app package created.",
        f"- Package ID: `{artifact.artifact_id}`",
        f"- Type: `{artifact.kind}`",
        f"- Files: {len(artifact.files)}",
        f"- Download: [{artifact.zip_filename}]({artifact.download_url})",
    ]
    if artifact.preview_url:
        lines.append(f"- Preview: [Open in AoiTalk]({artifact.preview_url})")
    if artifact.warnings:
        lines.append("- Safety warnings:")
        lines.extend(f"  - {warning}" for warning in artifact.warnings)
    return "\n".join(lines)


def _parse_files_json(files_json: str) -> dict[str, str] | None:
    raw = (files_json or "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return {str(path): "" if content is None else str(content) for path, content in parsed.items()}
    if isinstance(parsed, list):
        files: dict[str, str] = {}
        for item in parsed:
            if not isinstance(item, dict) or "path" not in item:
                raise ValueError("files_json array items must contain path and content.")
            files[str(item["path"])] = "" if item.get("content") is None else str(item.get("content"))
        return files
    raise ValueError("files_json must be a JSON object or array.")
