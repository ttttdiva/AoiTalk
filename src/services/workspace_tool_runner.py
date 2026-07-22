"""Discovery and subprocess execution for project workspace tools."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..tools.core import ToolDefinition, ToolParam

logger = logging.getLogger(__name__)
MAX_OUTPUT_BYTES = 256 * 1024


@dataclass(frozen=True)
class WorkspaceToolManifest:
    name: str
    description: str
    entrypoint: Path
    parameters: dict[str, Any]
    timeout_seconds: int = 60


def load_workspace_tool_manifests(workspace: Path) -> list[WorkspaceToolManifest]:
    manifests: list[WorkspaceToolManifest] = []
    seen: set[str] = set()
    tools_root = workspace / "tools"
    try:
        tools_root.resolve().relative_to(workspace.resolve())
    except ValueError:
        logger.warning("workspace 外を指す tools directory を拒否しました")
        return []
    for path in sorted(tools_root.glob("*/manifest.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            name = str(data["name"]).strip()
            description = str(data["description"]).strip()
            entrypoint_value = str(data["entrypoint"]).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) or not description or not entrypoint_value or name in seen:
                raise ValueError("name/description/entrypoint は必須で name は一意です")
            parameters = data.get("parameters") or {"type": "object", "properties": {}}
            if not isinstance(parameters, dict) or parameters.get("type", "object") != "object":
                raise ValueError("parameters は object JSON Schema である必要があります")
            if not isinstance(parameters.get("properties", {}), dict) or not isinstance(parameters.get("required", []), list):
                raise ValueError("parameters.properties/required が不正です")
            timeout = max(1, min(int(data.get("timeout_seconds", 60)), 300))
            entrypoint = (path.parent / entrypoint_value).resolve()
            entrypoint.relative_to(path.parent.resolve())
            path.parent.resolve().relative_to(workspace.resolve())
            if not entrypoint.is_file():
                raise ValueError("entrypoint が存在しません")
            seen.add(name)
            manifests.append(WorkspaceToolManifest(name, description, entrypoint, parameters, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace tool manifest をスキップしました (%s): %s", path, exc)
    return manifests


def _params_from_schema(schema: dict[str, Any]) -> list[ToolParam]:
    required = set(schema.get("required") or [])
    return [
        ToolParam(
            name=name,
            type=str(spec.get("type", "string")),
            description=str(spec.get("description", "")),
            required=name in required,
            default=spec.get("default"),
            enum=list(spec["enum"]) if isinstance(spec.get("enum"), list) else None,
            schema=dict(spec),
        )
        for name, spec in (schema.get("properties") or {}).items() if isinstance(spec, dict)
    ]


def manifest_to_tool(manifest: WorkspaceToolManifest) -> ToolDefinition:
    def execute(**kwargs: Any) -> Any:
        payload = json.dumps(kwargs, ensure_ascii=False).encode("utf-8")
        # Spool subprocess streams to disk so an untrusted tool cannot force an
        # unbounded in-memory capture before the 256KB response cap is applied.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                [sys.executable, str(manifest.entrypoint)], cwd=manifest.entrypoint.parent,
                stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
            )
            try:
                process.communicate(input=payload, timeout=manifest.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            stdout_file.seek(0)
            raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
            stderr_file.seek(0)
            stderr_text = stderr_file.read(64 * 1024).decode("utf-8", errors="replace")
        if stderr_text:
            logger.info("workspace tool %s stderr: %s", manifest.name, stderr_text.rstrip())
        if process.returncode != 0:
            raise RuntimeError(f"workspace tool exited with code {process.returncode}")
        truncated = len(raw) > MAX_OUTPUT_BYTES
        if truncated:
            raw = raw[:MAX_OUTPUT_BYTES]
        text = raw.decode("utf-8", errors="ignore")
        if truncated:
            return {"truncated": True, "output": text, "message": "出力が256KBを超えたため切り詰めました"}
        return json.loads(text)

    return ToolDefinition(
        name=f"ws_{manifest.name}", description=manifest.description, function=execute,
        parameters=_params_from_schema(manifest.parameters), risk="high", side_effect="external",
        requires_approval=True, timeout_seconds=float(manifest.timeout_seconds), supports_parallel=False,
        owner="workspace",
    )
