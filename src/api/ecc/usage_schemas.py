"""Read-only public metadata for one metered provider call."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


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
