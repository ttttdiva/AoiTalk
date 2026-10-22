"""Pydantic wire contract for App Runtime, Actions and Action Runs.

The source Manifest remains YAML, but runtime clients receive this bounded
JSON projection.  In particular, an Action descriptor exposes an opaque
Release-bound ``action_id`` and never exposes a command or local filesystem
path that a client could replay or retarget.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppUiOption(_WireModel):
    label: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=255)


class AppUiColumn(_WireModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=255)


class AppUiField(_WireModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=255)
    kind: Literal["text", "number", "select", "switch", "file"]
    required: bool = False
    options: list[AppUiOption] | None = None


class AppUiNode(_WireModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: Literal[
        "stack",
        "card",
        "text",
        "button",
        "form",
        "select",
        "switch",
        "list",
        "table",
        "detail",
        "progress",
        "log",
        "result",
        "file",
    ]
    children: list["AppUiNode"] = Field(default_factory=list)
    fields: list[AppUiField] | None = None
    columns: list[AppUiColumn] | None = None
    label: str | None = Field(default=None, max_length=255)
    text: str | None = Field(default=None, max_length=16_384)
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    options: list[AppUiOption] | None = None
    data: Any | None = None
    value: Any | None = None
    value_path: str | None = Field(
        default=None,
        pattern=r"^run\.(?:result|status)(?:\.[a-zA-Z][a-zA-Z0-9_-]*)*$",
    )
    action_id: str | None = Field(default=None, min_length=1, max_length=255)
    target_key: str | None = Field(default=None, max_length=80)


class AppUiDocument(_WireModel):
    schema_version: Literal[1]
    root: AppUiNode
    target_key: str | None = Field(default=None, max_length=80)


class AppActionExecution(_WireModel):
    kind: Literal["target_command", "pc_saved_action"]
    mode: Literal["finite", "service", "desktop"]
    command_key: Literal["build", "test", "run", "package"] | None = None
    saved_action_id: str | None = Field(default=None, max_length=255)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    contract_hash: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class AppActionDescriptor(_WireModel):
    action_id: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    target_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution_target_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    execution: AppActionExecution
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    # Availability is server-owned.  A missing/unknown execution target must
    # never become an executable action through a model default.
    available: bool = False
    unavailable_reason: str | None = Field(default=None, max_length=255)


class AppRuntimeRelease(_WireModel):
    id: str
    version: str
    git_revision: str
    manifest_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    readme_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    status: Literal["published", "deprecated"]
    source_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    source_bundle_size_bytes: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None


class AppRuntimeAvailability(_WireModel):
    available: bool
    reason: str | None = Field(default=None, max_length=255)


class AppRuntimeActivation(_WireModel):
    app_id: str
    release_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    activated_by: str | None = None
    activated_at: datetime | None = None


class AppRuntimeTarget(_WireModel):
    id: str
    target_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str | None = Field(default=None, max_length=255)
    surface: Literal["embedded_web", "standalone_web", "desktop_gui", "headless", "office"]
    runtime: Literal["static_web", "node", "python", "powershell", "batch", "vba", "executable"]
    execution_host: Literal["aoitalk", "server", "client", "browser", "office", "download_only"]
    entrypoint: str
    runtime_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    runtime_bundle_size_bytes: int | None = Field(default=None, ge=0)


class AppRuntimeResponse(_WireModel):
    app_id: str
    release: AppRuntimeRelease
    target: AppRuntimeTarget
    ui: AppUiDocument | None = None
    actions: list[AppActionDescriptor] = Field(default_factory=list)
    availability: AppRuntimeAvailability
    latest_runs: list["AppRunView"]
    activation: AppRuntimeActivation | None


class AppActionRunRequest(_WireModel):
    """Canonical request body; release/target/command are intentionally absent.

    Idempotency is an HTTP transport concern and is carried by the required
    ``Idempotency-Key`` header.  Keeping it out of the JSON body prevents two
    competing wire sources for the replay fence.
    """

    input: dict[str, Any] = Field(default_factory=dict)


class AppRunView(_WireModel):
    id: str
    app_id: str
    action_id: str
    release_id: str
    target_id: str
    project_id: str | None = None
    status: Literal[
        "unknown",
        "queued",
        "dispatching",
        "accepted",
        "starting",
        "running",
        "stopping",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
    ]
    idempotency_key: str = Field(min_length=1, max_length=160)
    mode: Literal["finite", "service", "desktop"]
    readiness_status: Literal[
        "unknown", "not_required", "ready", "pending", "failed"
    ]
    process_status: Literal["unknown", "not_started", "running", "exited"]
    exit_code: int | None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any | None = None
    error: dict[str, str] | None = None
    links: dict[str, str] = Field(default_factory=dict)


class AppActionRunResponse(_WireModel):
    run: AppRunView


class AppRunListResponse(_WireModel):
    runs: list[AppRunView] = Field(default_factory=list)


class AppRunFileResponse(_WireModel):
    run_id: str
    artifact_id: str
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    download_url: str


class AppRunLogsResponse(_WireModel):
    run_id: str

    class LogEntry(_WireModel):
        sequence: int = Field(ge=0)
        stream: str = Field(min_length=1, max_length=32)
        line: str = Field(max_length=16_384)
        created_at: datetime | None = None

    class LogPage(_WireModel):
        cursor: int = Field(default=0, ge=0)
        next_cursor: int = Field(default=0, ge=0)
        latest_cursor: int = Field(default=0, ge=0)
        eof: bool | None = None
        entries: list[LogEntry] = Field(default_factory=list)
        # ``data`` is retained for the local server supervisor, which reads
        # byte-offset logs; entries remain the canonical typed projection.
        data: str | None = None
        status: str | None = Field(default=None, max_length=32)

    # Accept the historical string shape while all new runtime transports
    # return the typed LogPage object.  This keeps embedded/headless clients
    # compatible during a rolling API deployment.
    logs: LogPage | str


class AppRunFileUploadResponse(_WireModel):
    id: str
    artifact_id: str | None = None
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    direction: Literal["input", "output"] = "input"
    download_url: str | None = None


AppUiNode.model_rebuild()
AppRuntimeResponse.model_rebuild()


__all__ = [
    "AppActionDescriptor",
    "AppActionExecution",
    "AppActionRunRequest",
    "AppActionRunResponse",
    "AppRunFileUploadResponse",
    "AppRunFileResponse",
    "AppRunListResponse",
    "AppRunLogsResponse",
    "AppRunView",
    "AppRuntimeRelease",
    "AppRuntimeResponse",
    "AppRuntimeTarget",
    "AppRuntimeActivation",
    "AppRuntimeAvailability",
    "AppUiColumn",
    "AppUiDocument",
    "AppUiField",
    "AppUiNode",
    "AppUiOption",
]
