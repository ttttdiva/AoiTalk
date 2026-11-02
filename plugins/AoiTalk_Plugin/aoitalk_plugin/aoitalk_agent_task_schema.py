"""MCP output schemas for the generic AoiTalk Agent task bridge."""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any, Callable

from mcp.types import CallToolResult, TextContent
from typing_extensions import Required, TypedDict


class FreeTeamTaskUsage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    requests: int
    quota_actual: dict[str, int]


class FreeTeamTaskSettlement(TypedDict, total=False):
    status: str
    actual_usage: FreeTeamTaskUsage


class FreeTeamReservationsByStatus(TypedDict, total=False):
    committed: int
    uncertain: int
    released: int
    reserved: int


class FreeTeamReservationsTotal(TypedDict, total=False):
    count: int
    by_status: FreeTeamReservationsByStatus
    actual_usage: FreeTeamTaskUsage
    uncertain_estimated_hold: dict[str, int]
    truncated: bool


class AgentTaskFreeTeam(TypedDict, total=False):
    billing_mode: str
    candidate: str
    provider: str
    model: str
    quota_pool_ids: list[str]
    reservation_id: str
    settlement: FreeTeamTaskSettlement
    wait: dict[str, Any]
    reservations_total: FreeTeamReservationsTotal
    non_free_reservation_detected: bool


class AgentTaskFields(TypedDict, total=False):
    task_id: str
    status: str
    work_item_id: str
    agent_run_id: str
    agent_id: str
    agent_revision_id: str
    cancellation_requested: bool
    is_terminal: bool
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str
    current_step: str
    summary_text: str
    failure_reason: str
    failure_detail: str
    progress: Any
    result: Any
    failure: dict[str, Any] | None
    tool_failures: dict[str, Any] | None
    free_team: AgentTaskFreeTeam


class BusinessReply(TypedDict, total=False):
    ok: Required[bool]
    error: str
    message: str
    http_status: int
    retry_after_seconds: int
    local_cooldown_seconds: int
    error_source: str
    server_error_code: str
    server_detail: str
    output_truncated: bool
    next_step: str


class TaskReply(AgentTaskFields, BusinessReply, total=False):
    pass


class StartReply(TaskReply, total=False):
    created: bool
    idempotency_key: str
    submission_uncertain: bool


class TailReply(BusinessReply, total=False):
    task_id: str
    status: str
    events: list[dict[str, Any]]
    truncated: bool
    event_count: int | None


class ResultReply(BusinessReply, total=False):
    task_id: str
    status: str
    content: str
    content_sha256: str
    total_chars: int
    total_bytes: int
    offset: int
    next_offset: int | None
    free_team: AgentTaskFreeTeam


class FreeTeamQuota(TypedDict, total=False):
    id: str
    configured_daily_cap: int | float
    effective_daily_cap: int | float
    used: int | float
    reserved: int | float
    external_delta: int | float
    remaining: int | float
    window_end: str | None
    status: str
    stop_reason: str


class FreeTeamStatusReply(BusinessReply, total=False):
    quota_pools: list[FreeTeamQuota]
    verification: dict[str, str]
    openrouter_provider_observed: dict[str, str]
    provider_blocked: bool
    provider_block_reason_code: str
    provider_blocked_until: str | None
    provider_half_open: bool
    provider_block_persistence: str
    next_probe_at: str | None
    probe_interval_minutes: int
    openai_usage_actual: dict[str, object]


REPLIES = {
    "aoitalk_agent_task_start": StartReply,
    "aoitalk_agent_task_status": TaskReply,
    "aoitalk_free_team_status": FreeTeamStatusReply,
    "aoitalk_agent_task_tail": TailReply,
    "aoitalk_agent_task_result": ResultReply,
    "aoitalk_agent_task_cancel": TaskReply,
}


def structured_agent_task_tool(target: Callable[..., Any]) -> Callable[..., Any]:
    """Publish a typed output schema while preserving the returned JSON values."""

    @functools.wraps(target)
    async def invoke(**kwargs):
        result = await target(**kwargs)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))],
            structuredContent=result,
        )

    annotation = Annotated[CallToolResult, REPLIES[target.__name__]]
    invoke.__signature__ = inspect.signature(target, eval_str=True).replace(return_annotation=annotation)
    invoke.__annotations__ = {**inspect.get_annotations(target, eval_str=True), "return": annotation}
    return invoke
