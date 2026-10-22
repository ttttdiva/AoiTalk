"""Generated-client contract for the bounded, read-only memory display."""

from typing import Literal

from pydantic import BaseModel, Field


MemoryStage = Literal["scope_selection", "relevance_decision", "context_budget"]
MemoryStatus = Literal["running", "completed", "disabled", "skipped", "blocked", "failed", "cancelled"]


class MemorySelectionStage(BaseModel):
    schema_version: Literal[1]
    pipeline: Literal["automatic_context_memory"]
    stage: MemoryStage
    status: MemoryStatus
    engine: Literal["host", "jev", "eg2"]
    reason: str | None = Field(default=None, max_length=96)
    candidate_count: int | None = Field(default=None, ge=0, le=10000)
    selected_count: int | None = Field(default=None, ge=0, le=10000)
    excluded_count: int | None = Field(default=None, ge=0, le=10000)


class MemorySelectionItem(BaseModel):
    memory_id: str
    version: int = Field(ge=1)
    candidate_rank: int = Field(ge=1, le=10000)
    selected: bool
    selection_reason: Literal["pinned", "keyword_and_scope_match", "semantic_match", "scope_priority", "context_budget_exceeded", "jev_low_relevance", "jev_not_evaluated"] | None
    weighted_score: float | None = Field(default=None, ge=0, le=1)
    jev_relevance: float | None = Field(default=None, ge=0, le=1)
    semantic_score: float | None = Field(default=None, ge=-1, le=1)
    title: str = Field(max_length=180)
    excerpt: str = Field(max_length=240)
    scope: Literal["global", "user", "project", "task", "session"]
    updated_at: str | None
    source: Literal["scoped_memory"]


class MemorySelectionProjection(BaseModel):
    schema_version: Literal[1]
    pipeline: Literal["automatic_context_memory"]
    stages: dict[MemoryStage, MemorySelectionStage]
    history: list[MemorySelectionStage] = Field(max_length=8)
    items: list[MemorySelectionItem] = Field(max_length=8)


class MemorySelectionBinding(BaseModel):
    session_id: str
    message_id: str | None
    agent_run_id: str | None
    active_branch: bool


class MemoryToolCallObservation(BaseModel):
    call_id: str = Field(max_length=160)
    tool: Literal["search_past_chats", "semantic_memory_search"]
    mode: Literal["semantic", "text"]
    status: Literal["completed", "partial", "failed", "unavailable"]


class MemoryToolReference(BaseModel):
    ref_key: str
    call_id: str
    kind: Literal["scoped_memory", "conversation"]
    source_id: str
    version: int | None = None
    title: str = Field(max_length=180)
    excerpt: str = Field(max_length=240)
    href: str | None
    score: float | None
    score_kind: Literal["memory_relevance", "semantic_relevance", "text_match"]


class MemoryToolProjection(BaseModel):
    pipeline: Literal["explicit_memory_search"]
    calls: list[MemoryToolCallObservation] = Field(max_length=4)
    items: list[MemoryToolReference] = Field(max_length=8)


class MemorySelectionResponse(BaseModel):
    success: Literal[True]
    status: Literal["available", "unavailable"]
    binding: MemorySelectionBinding
    observation: MemorySelectionProjection | None
    tool_recall: MemoryToolProjection | None = None
