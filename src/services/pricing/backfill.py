"""既存 `token_usage` 行の料金再計算（バックフィル）。

- `created_at` 時点で有効なルールを使う。
- `provider_reported_cost IS NOT NULL` の行は絶対に上書きしない。
- `dry_run=True` では一切 UPDATE しない。
- 自動では走らせない。明示呼び出しのみ。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .engine import PricingEngine, PricingStatus, UsageInput
from .providers import canonical_provider

logger = logging.getLogger(__name__)

__all__ = ["BackfillFilter", "backfill_token_usage_costs"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class BackfillFilter:
    """バックフィル対象の絞り込み条件。"""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    only_zero_cost: bool = True


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


async def backfill_token_usage_costs(
    filt: BackfillFilter,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
) -> dict:
    """既存の使用量行に対して料金を再計算する。"""
    from sqlalchemy import select

    from ...memory.database import get_db_session
    from ...models.ecc_models import TokenUsage
    from .engine import get_pricing_engine

    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "scanned": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_provider_reported": 0,
        "unknown_model_count": 0,
        "unknown_models": [],
        "total_cost_before": "0",
        "total_cost_after": "0",
        "list_total_before": "0",
        "list_total_after": "0",
    }

    engine: PricingEngine = get_pricing_engine()
    await engine.ensure_loaded()
    if engine.rule_count == 0:
        engine = PricingEngine.from_catalog_file()

    total_before = ZERO
    total_after = ZERO
    list_before = ZERO
    list_after = ZERO
    unknown_models: Dict[str, int] = {}

    async with await get_db_session() as session:
        stmt = select(TokenUsage)
        if filt.start is not None:
            stmt = stmt.where(TokenUsage.created_at >= filt.start)
        if filt.end is not None:
            stmt = stmt.where(TokenUsage.created_at < filt.end)
        if filt.provider:
            stmt = stmt.where(TokenUsage.provider == filt.provider)
        if filt.model:
            stmt = stmt.where(TokenUsage.model == filt.model)
        stmt = stmt.order_by(TokenUsage.created_at.asc())
        if limit is not None:
            stmt = stmt.limit(int(limit))

        rows = (await session.execute(stmt)).scalars().all()

        for row in rows:
            if filt.only_zero_cost and _as_decimal(row.total_cost) != ZERO:
                continue
            result["scanned"] += 1

            total_before += _as_decimal(row.total_cost)
            list_before += _as_decimal(getattr(row, "list_total_cost", None))

            if getattr(row, "provider_reported_cost", None) is not None:
                # プロバイダ申告実費は正本。絶対に上書きしない。
                result["skipped_provider_reported"] += 1
                total_after += _as_decimal(row.total_cost)
                list_after += _as_decimal(getattr(row, "list_total_cost", None))
                continue

            requested = getattr(row, "requested_model", None) or row.model
            usage = UsageInput(
                provider=row.provider,
                requested_model=requested or "",
                resolved_model=getattr(row, "resolved_model", None),
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
                cached_tokens=int(row.cached_tokens or 0),
                cache_write_tokens=int(row.cache_write_tokens or 0),
                tool_invocations=getattr(row, "tool_invocations", None) or None,
                occurred_at=row.created_at,
            )
            breakdown = engine.calculate(usage)

            if breakdown.is_unknown:
                key = f"{canonical_provider(row.provider)}:{requested}"
                unknown_models[key] = unknown_models.get(key, 0) + 1

            total_after += breakdown.list_total_cost
            list_after += breakdown.list_total_cost

            # 実際に値が変わる行だけを「更新」として数える。こうすると
            # dry-run でも変更件数が分かり、適用済みの再実行は 0 件になる。
            stored_total = _as_decimal(getattr(row, "list_total_cost", None))
            stored_status = getattr(row, "pricing_status", None)
            changes = (
                stored_total != breakdown.list_total_cost
                or stored_status != breakdown.pricing_status
                or getattr(row, "pricing_rule_id", None) != breakdown.rule_id
            )
            if not changes:
                result["unchanged"] += 1
                continue

            result["updated"] += 1
            if dry_run:
                continue

            row.requested_model = requested
            row.pricing_status = breakdown.pricing_status
            row.pricing_catalog_version = breakdown.catalog_version
            row.pricing_rule_id = breakdown.rule_id
            row.free_incentive_group = breakdown.free_incentive_group
            row.applied_input_rate = breakdown.applied_input_rate
            row.applied_cached_input_rate = breakdown.applied_cached_input_rate
            row.applied_cache_write_rate = breakdown.applied_cache_write_rate
            row.applied_output_rate = breakdown.applied_output_rate
            row.list_input_cost = breakdown.list_input_cost
            row.list_output_cost = breakdown.list_output_cost
            row.list_tool_cost = breakdown.list_tool_cost
            row.list_total_cost = breakdown.list_total_cost
            if row.billing_scope_id is None:
                row.billing_scope_id = "default"
            # 後方互換の概算値も同じ値で更新する
            row.input_cost = float(breakdown.list_input_cost)
            row.output_cost = float(breakdown.list_output_cost)
            row.total_cost = float(breakdown.list_total_cost)

        if not dry_run and result["updated"]:
            await session.commit()

    result["unknown_model_count"] = sum(unknown_models.values())
    result["unknown_models"] = sorted(unknown_models.keys())
    result["total_cost_before"] = str(total_before)
    result["total_cost_after"] = str(total_after)
    result["list_total_before"] = str(list_before)
    result["list_total_after"] = str(list_after)
    return result
