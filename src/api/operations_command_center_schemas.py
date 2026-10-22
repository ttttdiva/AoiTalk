"""Additive wire contract for the read-only Operations command center."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OperationsCommandCenterPage(BaseModel):
    limit: int
    fetched: int
    available: int
    has_more: bool
    truncated: bool
    next_cursor: str | None


class OperationsCommandCenterResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str
    organization: dict[str, Any]
    summary: dict[str, Any]
    current_work: list[dict[str, Any]]
    work_page: OperationsCommandCenterPage
    agents: list[dict[str, Any]]
    agents_page: OperationsCommandCenterPage
    attention: list[dict[str, Any]]
    attention_page: dict[str, Any]
    attention_has_more: bool
    projects: list[dict[str, Any]]
    spaces: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    activity: list[dict[str, Any]]
    runtime_usage: dict[str, Any]
    filters: dict[str, Any] = Field(default_factory=dict)
