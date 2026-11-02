"""UTC-day usage parsing and spend estimates for OpenAI Realtime calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Mapping

from sqlalchemy import func, or_, select

logger = logging.getLogger(__name__)

LIVE_VOICE_PROVIDER = "openai_realtime"
LIVE_VOICE_RESPONSE_EVENT = "live_voice.response.done"
DEFAULT_DAILY_SPEND_CAP_USD = Decimal("1.00")
MAX_DAILY_SPEND_CAP_USD = Decimal("1000.00")


@dataclass(frozen=True)
class RealtimeUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    input_audio_tokens: int | None
    input_text_tokens: int | None
    output_audio_tokens: int | None
    output_text_tokens: int | None


def utc_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime, date]:
    """Return naive UTC [start, end) bounds and their UTC date."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    day = current.date()
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1), day


def daily_spend_cap(config: Any) -> Decimal:
    """Read the per-day Realtime cap; malformed values fall back conservatively."""
    value: Any = None
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter("live_voice.openai_realtime.daily_spend_cap_usd", None)
        except Exception:
            value = None
    if value is None and isinstance(config, Mapping):
        section = config.get("live_voice")
        if isinstance(section, Mapping):
            realtime = section.get("openai_realtime")
            if isinstance(realtime, Mapping):
                value = realtime.get("daily_spend_cap_usd")
    try:
        cap = Decimal(str(value)) if value is not None else DEFAULT_DAILY_SPEND_CAP_USD
    except (InvalidOperation, TypeError, ValueError):
        return DEFAULT_DAILY_SPEND_CAP_USD
    if not cap.is_finite() or cap < 0 or cap > MAX_DAILY_SPEND_CAP_USD:
        return DEFAULT_DAILY_SPEND_CAP_USD
    return cap


def _token_count(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 1_000_000_000:
        return value
    return None


def _detail_count(details: Any, key: str) -> int | None:
    if not isinstance(details, Mapping) or key not in details:
        return None
    return _token_count(details.get(key))


def extract_realtime_usage(event: Mapping[str, Any]) -> RealtimeUsage | None:
    """Extract response.done token usage without retaining arbitrary payload data."""
    response = event.get("response")
    response = response if isinstance(response, Mapping) else {}
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return None

    input_details = usage.get("input_token_details")
    if not isinstance(input_details, Mapping):
        input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_token_details")
    if not isinstance(output_details, Mapping):
        output_details = usage.get("output_tokens_details")

    input_tokens = _token_count(usage.get("input_tokens"))
    if input_tokens is None:
        parts = [
            _detail_count(input_details, name)
            for name in ("text_tokens", "audio_tokens", "image_tokens")
        ]
        if not any(part is not None for part in parts):
            return None
        input_tokens = sum(part or 0 for part in parts)

    output_tokens = _token_count(usage.get("output_tokens"))
    if output_tokens is None:
        parts = [
            _detail_count(output_details, name)
            for name in ("text_tokens", "audio_tokens")
        ]
        if not any(part is not None for part in parts):
            return None
        output_tokens = sum(part or 0 for part in parts)

    reported_total = usage.get("total_tokens")
    if reported_total is not None:
        parsed_total = _token_count(reported_total)
        if parsed_total is None or parsed_total != input_tokens + output_tokens:
            return None

    cached_value = (
        input_details.get("cached_tokens", usage.get("cached_tokens", 0))
        if isinstance(input_details, Mapping)
        else usage.get("cached_tokens", 0)
    )
    cached_tokens = _token_count(cached_value)
    if cached_tokens is None:
        return None
    if cached_tokens > input_tokens:
        return None
    reasoning_value = (
        output_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0))
        if isinstance(output_details, Mapping)
        else usage.get("reasoning_tokens", 0)
    )
    reasoning_tokens = _token_count(reasoning_value)
    if reasoning_tokens is None or reasoning_tokens > output_tokens:
        return None

    return RealtimeUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        input_audio_tokens=_detail_count(input_details, "audio_tokens"),
        input_text_tokens=_detail_count(input_details, "text_tokens"),
        output_audio_tokens=_detail_count(output_details, "audio_tokens"),
        output_text_tokens=_detail_count(output_details, "text_tokens"),
    )


@lru_cache(maxsize=1)
def _realtime_pricing_engine():
    from .pricing.engine import PricingEngine

    return PricingEngine.from_catalog_file()


def estimate_realtime_cost_usd(model: str, usage: RealtimeUsage) -> Decimal | None:
    """Use the local conservative Realtime list-rate rules for a response.

    TokenUsage has one input/output rate pair rather than modality subcolumns.
    The Realtime catalog entries therefore apply the highest supported audio
    rate to text, image, and cached tokens too; this intentionally overestimates
    mixed-modality usage for the daily cap.
    """
    from .pricing.engine import UsageInput

    breakdown = _realtime_pricing_engine().calculate(
        UsageInput(
            provider=LIVE_VOICE_PROVIDER,
            requested_model=model,
            resolved_model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    if breakdown.pricing_status != "priced" or breakdown.list_total_cost < 0:
        return None
    if not breakdown.list_total_cost.is_finite():
        return None
    return breakdown.list_total_cost


async def read_daily_realtime_spend(
    db_manager: Any,
    *,
    start: datetime,
    end: datetime,
) -> Decimal | None:
    """Read today's Realtime ledger; return None when any cost is unaccounted.

    The database must already have completed normal startup initialization. This
    function never initializes/migrates the database from a request path.
    """
    initialized = getattr(db_manager, "is_initialized", None)
    if not callable(initialized) or not initialized():
        return None
    try:
        from ..memory.database import get_db_session
        from ..memory.models.agent_runs import AgentRun, AgentRunEvent
        from ..models.ecc_models import TokenUsage

        async with await get_db_session() as session:
            unknown_count = (
                await session.execute(
                    select(func.count(TokenUsage.id)).where(
                        TokenUsage.provider == LIVE_VOICE_PROVIDER,
                        TokenUsage.created_at >= start,
                        TokenUsage.created_at < end,
                        or_(
                            TokenUsage.pricing_status.is_(None),
                            TokenUsage.pricing_status != "priced",
                            TokenUsage.list_total_cost.is_(None),
                        ),
                    )
                )
            ).scalar_one()
            if int(unknown_count or 0) > 0:
                return None
            stored_cost = (
                await session.execute(
                    select(func.coalesce(func.sum(TokenUsage.list_total_cost), 0)).where(
                        TokenUsage.provider == LIVE_VOICE_PROVIDER,
                        TokenUsage.created_at >= start,
                        TokenUsage.created_at < end,
                    )
                )
            ).scalar_one()
            event_status = AgentRunEvent.payload["usage_accounting_status"].as_string()
            fallback_events = (
                await session.execute(
                    select(AgentRunEvent.payload)
                    .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
                    .where(
                        AgentRun.run_type == "live_voice_session",
                        AgentRunEvent.event_type == LIVE_VOICE_RESPONSE_EVENT,
                        AgentRunEvent.created_at >= start,
                        AgentRunEvent.created_at < end,
                        event_status.in_(("token_usage_unpersisted", "unknown")),
                    )
                )
            ).scalars().all()
        total = Decimal(str(stored_cost or 0))
        for payload in fallback_events:
            if not isinstance(payload, Mapping):
                return None
            status = payload.get("usage_accounting_status")
            if status == "unknown":
                return None
            try:
                estimate = Decimal(str(payload.get("estimated_cost_usd")))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if not estimate.is_finite() or estimate < 0:
                return None
            # Conservative if a TokenUsage commit returned an ambiguous result.
            total += estimate
        return total if total.is_finite() and total >= 0 else None
    except Exception as exc:
        logger.warning(
            "Realtime daily spend ledger read failed: exception_type=%s",
            type(exc).__name__,
        )
        return None

