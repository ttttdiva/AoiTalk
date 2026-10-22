"""Shared read contract; business records remain the sources of truth."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkReference(BaseModel):
    kind: Literal["task", "opportunity", "content_item"]
    id: str


class WorkMoneyEvidence(BaseModel):
    category: Literal["actual", "estimate", "reservation", "unmeasured"]
    amount: str | None
    currency: str | None
    source: str
    usage_id: str | None = Field(
        default=None, description="Exact TokenUsage row identity, when recorded."
    )
    run_id: str | None = Field(
        default=None, description="Recorded AgentRun link; never inferred."
    )


class WorkRunRead(BaseModel):
    id: str
    work_item_id: str | None
    parent_run_id: str | None
    status: str = Field(
        description="Last recorded run status; does not assert runtime liveness."
    )
    created_at: str | None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    agent_display_name: str | None = None
    acting_subagent_id: str | None = None
    work_item_attempt: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_event_at: str | None = None
    last_recorded_phase: str | None = None
    last_recorded_phase_at: str | None = None


class WorkAttemptRead(BaseModel):
    id: str
    parent_work_item_id: str | None
    state: str
    current_run_id: str | None
    current_run_status: str
    linkage: Literal["recorded", "missing", "partial"]
    assigned_agent_id: str | None = None
    agent_revision_id: str | None = None
    assigned_agent_display_name: str | None = None
    deadline: str | None = None
    not_before: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    current_phase: str | None = Field(
        default=None,
        description="Last phase of the exact current Run, when its state is known.",
    )
    current_phase_at: str | None = None


class WorkArtifactRead(BaseModel):
    kind: Literal["artifact_version", "content_variant_revision"]
    id: str
    availability: Literal["available", "unavailable", "unknown"]
    content_href: str | None


class WorkDecisionRead(BaseModel):
    kind: Literal["external_action", "deliverable_review"]
    id: str
    version: int | None


class WorkActionRead(BaseModel):
    id: str
    status: str
    version: int


class WorkReadItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: WorkReference
    project_id: str | None
    space_id: str | None
    title: str | None
    business_status: str
    created_at: str | None
    task_due_at: str | None = Field(
        default=None, description="Canonical Task.end_at only."
    )
    content_scheduled_at: str | None = Field(
        default=None, description="Canonical ContentItem.scheduled_at only."
    )
    last_recorded_phase: str | None = None
    last_recorded_phase_at: str | None = None
    last_recorded_phase_run_id: str | None = Field(
        default=None,
        description="Exact owned Run that supplied the last recorded phase.",
    )
    attempts: list[WorkAttemptRead]
    runs: list[WorkRunRead]
    artifacts: list[WorkArtifactRead]
    actions: list[WorkActionRead]
    decisions: list[WorkDecisionRead]
    costs: list[WorkMoneyEvidence]
    gaps: list[str]
    execution_availability: Literal["not_advertised"]


class WorkReadPage(BaseModel):
    limit: int
    fetched: int
    available: int
    has_more: bool
    next_cursor: str | None


class WorkReadProjectionResponse(BaseModel):
    schema_version: Literal["work-read-projection-v1"]
    observed_at: str
    items: list[WorkReadItem]
    page: WorkReadPage
