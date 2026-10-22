"""Closed, read-only Operations execution contract shared by Web and Mobile."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationsExecutionSummary(_Projection):
    phase: str | None = None
    tool: str | None = None
    last_activity_at: str | None = None
    elapsed_seconds: float | None = None
    attempt_count: int = 0
    max_attempts: int = 1
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    lease_health: Literal["none", "active", "expired"] = "none"
    blocker_code: str | None = None
    safe_error_code: str | None = None
    result_summary: str | None = None
    attention_reasons: list[str] = Field(default_factory=list)
    observation_status: Literal["available", "unavailable", "partial"] = "available"


class OperationsCanonicalSourceRef(_Projection):
    """Typed navigation identity; never carries a URL or filesystem path."""

    kind: Literal["conversation", "agent", "work"]
    id: str


class OperationsExecutionWork(_Projection):
    id: str
    parent_work_item_id: str | None = None
    root_work_item_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    assigned_agent_id: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    domain: str | None = None
    state: str
    created_at: str | None = None
    updated_at: str | None = None
    execution_summary: OperationsExecutionSummary
    source_href: str | None = None
    canonical_ref: OperationsCanonicalSourceRef | None = None
    source_ref: OperationsCanonicalSourceRef | None = None


class OperationsExecutionRun(_Projection):
    id: str
    work_item_id: str | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    acting_subagent_id: str | None = None
    work_item_attempt: int | None = None
    status: str
    phase: str | None = None
    tool: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_event_at: str | None = None
    elapsed_seconds: float | None = None
    error_code: str | None = None


class OperationsExecutionEvent(_Projection):
    id: str
    run_id: str
    sequence: int
    event_type: str
    status: str | None = None
    phase: str | None = None
    created_at: str | None = None
    error_code: str | None = None


class OperationsExecutionTool(_Projection):
    id: str
    run_id: str
    tool_name: str
    status: Literal["running", "succeeded", "failed", "unknown"]
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None


class OperationsExecutionPage(_Projection):
    next_cursor: str | None = None
    has_more: bool = False
    truncated: bool = False
    limit: int


class OperationsWorkExecutionResponse(_Projection):
    schema_version: Literal["operations-work-execution-v1"] = "operations-work-execution-v1"
    observed_at: str
    work: OperationsExecutionWork
    runs: list[OperationsExecutionRun] = Field(default_factory=list)
    events: list[OperationsExecutionEvent] = Field(default_factory=list)
    tool_calls: list[OperationsExecutionTool] = Field(default_factory=list)
    runs_page: OperationsExecutionPage
    events_page: OperationsExecutionPage
    tools_page: OperationsExecutionPage
