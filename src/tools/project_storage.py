"""Server-mediated Project file publication tools for Enterprise harness work."""

from __future__ import annotations

import inspect
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from ..memory.database import get_database_manager
from ..services.app_storage import get_workspaces_root
from ..services.project_context import get_runtime_project_context
from ..services.project_storage_publisher import (
    ProjectStorageCapability,
    ProjectStorageDiff,
    ProjectStoragePublisher,
    StagedFile,
)
from ..services.turn_context import get_turn_context
from .core import ToolDefinition, ToolParam


def _reject_link_or_reparse(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & flag
    ):
        raise PermissionError("Project publication staging cannot be a link/reparse point")


def _trusted_identity(context: Mapping[str, Any] | None) -> tuple[UUID, UUID]:
    turn = get_turn_context()
    runtime = get_runtime_project_context()
    effective = dict(runtime) if isinstance(runtime, Mapping) else dict(context or {})
    turn_user = str(getattr(turn, "user_id", "") or "").strip()
    turn_project = str(getattr(turn, "project_id", "") or "").strip()
    context_user = str(effective.get("user_id") or "").strip()
    context_project = str(
        effective.get("project_id") or effective.get("id") or ""
    ).strip()
    if not turn_user or not turn_project:
        raise PermissionError("authenticated Project context is required")
    if context_user and context_user != turn_user:
        raise PermissionError("Project publisher user context mismatch")
    if context_project and context_project != turn_project:
        raise PermissionError("Project publisher Project context mismatch")
    return UUID(turn_user), UUID(turn_project)


def build_project_storage_tool_definitions(
    context: Mapping[str, Any] | None = None,
    *,
    workspace_root: str | None = None,
) -> list[ToolDefinition]:
    runtime_context = dict(context or {})
    root = get_workspaces_root(workspace_root)

    async def _publish(
        *,
        writes: Mapping[str, Any] | None = None,
        deletes: Sequence[str] = (),
        operation_id: str = "",
        allow_delete: bool,
    ) -> dict[str, Any]:
        user_id, project_id = _trusted_identity(runtime_context)
        if writes is not None and not isinstance(writes, Mapping):
            raise ValueError("writes must be an object of Project-relative text files")
        if isinstance(deletes, (str, bytes)) or not isinstance(deletes, Sequence):
            raise ValueError("deletes must be an array of Project-relative paths")
        normalized_writes: dict[str, StagedFile] = {}
        for relative, value in (writes or {}).items():
            if not isinstance(value, str):
                raise ValueError("Project publisher write values must be text")
            normalized_writes[str(relative)] = StagedFile(content=value)
        effective_operation_id = operation_id.strip()
        if not effective_operation_id:
            from ..services.agent_run_service import get_current_agent_run_id

            run_id = str(get_current_agent_run_id() or "turn").strip() or "turn"
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "writes": dict(writes or {}),
                        "deletes": [str(item) for item in deletes],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            effective_operation_id = f"agent-{run_id[:180]}-{digest}"

        stage_parent = root / ".project-publish-stage"
        _reject_link_or_reparse(stage_parent)
        stage_parent.mkdir(parents=True, exist_ok=True)
        _reject_link_or_reparse(stage_parent)
        stage = Path(tempfile.mkdtemp(prefix="tool-", dir=stage_parent))
        try:
            capability = ProjectStorageCapability.issue(
                principal_id=user_id,
                project_id=project_id,
                staged_root=stage,
                allow_write=bool(normalized_writes),
                allow_delete=allow_delete,
            )
            session = await get_database_manager().get_session()
            try:
                result = await ProjectStoragePublisher(
                    workspace_root=root,
                ).publish(
                    session,
                    user_id,
                    project_id,
                    ProjectStorageDiff(
                        writes=normalized_writes,
                        deletes=tuple(str(item) for item in deletes),
                        operation_id=effective_operation_id,
                    ),
                    capability=capability,
                )
                return result.to_dict()
            except BaseException:
                await session.rollback()
                raise
            finally:
                await session.close()
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def publish_project_files(
        writes: dict[str, str],
        operation_id: str = "",
    ) -> dict[str, Any]:
        return await _publish(
            writes=writes,
            operation_id=operation_id,
            allow_delete=False,
        )

    async def delete_project_files(
        paths: list[str],
        operation_id: str = "",
    ) -> dict[str, Any]:
        return await _publish(
            deletes=paths,
            operation_id=operation_id,
            allow_delete=True,
        )

    def _tool(
        name: str,
        description: str,
        function: Any,
        parameters: list[ToolParam],
        *,
        risk: str,
        side_effect: str,
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            function=function,
            parameters=parameters,
            is_async=inspect.iscoroutinefunction(function),
            owner="project_storage",
            risk=risk,
            side_effect=side_effect,
            requires_approval=risk == "high",
            supports_parallel=False,
        )

    return [
        _tool(
            "publish_project_files",
            "Publish text files through Project ACL, lock, managed-path and quota checks.",
            publish_project_files,
            [
                ToolParam("writes", "object"),
                ToolParam("operation_id", "string", required=False, default=""),
            ],
            risk="medium",
            side_effect="filesystem,database",
        ),
        _tool(
            "delete_project_files",
            "Delete Project paths through the separate delete ACL and mediated publisher.",
            delete_project_files,
            [
                ToolParam("paths", "array"),
                ToolParam("operation_id", "string", required=False, default=""),
            ],
            risk="high",
            side_effect="filesystem,database,delete",
        ),
    ]


__all__ = ["build_project_storage_tool_definitions"]
