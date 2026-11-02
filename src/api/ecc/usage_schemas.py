"""Read-only public metadata for one metered provider call."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TokenUsageDetail(BaseModel):
    id: UUID
    user_id: str | None
    project_id: UUID | None
    agent_run_id: UUID | None
    provider: str
    requested_model: str
    resolved_model: str | None
    agent_name: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    pricing_status: str | None
    list_total_cost: str | None
    provider_reported_cost: str | None
    created_at: datetime | None


class ProviderKeyHealth(BaseModel):
    consumer_id: str
    last_success_at: str | None
    last_failure_at: str | None
    last_failure_class: str | None
    success_count: int
    failure_count: int
    attribution: Literal["confirmed", "unknown", "conflict"]
    legacy_still_in_use: bool


class ProviderKeyAttributionEntry(BaseModel):
    masked_alias: str
    local_digest: str
    kind: Literal["legacy", "project", "unknown"]
    state: Literal["active", "disabled", "unknown"]
    state_source: str
    provider_key_id_digest: str | None
    health: list[ProviderKeyHealth]


class ProviderKeyAttributionResponse(BaseModel):
    keys: list[ProviderKeyAttributionEntry]


class ProviderKeyAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    local_digest: str = Field(pattern=r"^[0-9a-f]{24}$")
    provider_key_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["active", "disabled", "unknown"]
