"""OpenAI データ共有インセンティブ（無料枠）の割当。

無料枠は「UTC 日界」「請求スコープ単位」「1M / 10M グループ別」に管理する。
上限を少しでも超えるリクエストは**リクエスト全体**が有料になり、
無料化しなかったリクエストのトークンは used に加算しない（枠を食い潰さない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "FREE_TIER_LIMITS",
    "FREE_TIER_GROUPS",
    "DEFAULT_BILLING_SCOPE_ID",
    "FreeTierConfig",
    "AllocatableRecord",
    "FreeTierAllocation",
    "allocate_free_tier",
    "free_tier_usage",
    "free_tier_limits_for",
]

DEFAULT_BILLING_SCOPE_ID = "default"
FREE_TIER_GROUPS = ("1m", "10m")

FREE_TIER_LIMITS: Dict[str, Dict[str, int]] = {
    "tier_1_2": {"1m": 250_000, "10m": 2_500_000},
    "tier_3_plus": {"1m": 1_000_000, "10m": 10_000_000},
}


@dataclass(frozen=True)
class FreeTierConfig:
    """無料枠の設定。"""

    enabled: bool = False
    tier: str = "tier_1_2"
    billing_scope_id: Optional[str] = None


@dataclass(frozen=True)
class AllocatableRecord:
    """無料枠割当の入力となる1リクエスト分の記録。"""

    row_id: str
    created_at: datetime
    provider: str
    billing_scope_id: Optional[str]
    free_incentive_group: Optional[str]
    input_tokens: int
    output_tokens: int
    list_token_cost: Decimal
    list_tool_cost: Decimal
    list_total_cost: Decimal
    pricing_status: str


@dataclass(frozen=True)
class FreeTierAllocation:
    """1リクエストに対する割当結果。"""

    row_id: str
    is_free: bool
    billed_cost: Decimal
    group: Optional[str]
    utc_day: date


def free_tier_limits_for(tier: Optional[str]) -> Dict[str, int]:
    """tier 名から上限表を引く。未知の tier は tier_1_2 扱い。"""
    return FREE_TIER_LIMITS.get(tier or "tier_1_2", FREE_TIER_LIMITS["tier_1_2"])


def _is_eligible(record: AllocatableRecord) -> bool:
    """無料枠の対象になり得るレコードか。"""
    return (
        record.provider == "openai"
        and record.free_incentive_group is not None
        and record.pricing_status == "priced"
    )


def allocate_free_tier(
    records: Iterable[AllocatableRecord],
    config: FreeTierConfig,
) -> Dict[str, FreeTierAllocation]:
    """レコード群へ無料枠を決定的に割り当てる。"""
    items: List[AllocatableRecord] = list(records)
    result: Dict[str, FreeTierAllocation] = {}

    if not config.enabled:
        for record in items:
            result[record.row_id] = FreeTierAllocation(
                row_id=record.row_id,
                is_free=False,
                billed_cost=record.list_total_cost,
                group=record.free_incentive_group,
                utc_day=record.created_at.date(),
            )
        return result

    limits = free_tier_limits_for(config.tier)
    scope_filter = config.billing_scope_id

    eligible: List[AllocatableRecord] = []
    for record in items:
        record_scope = record.billing_scope_id or DEFAULT_BILLING_SCOPE_ID
        if scope_filter is not None and record_scope != scope_filter:
            eligible_now = False
        else:
            eligible_now = _is_eligible(record)
        if eligible_now:
            eligible.append(record)
        else:
            result[record.row_id] = FreeTierAllocation(
                row_id=record.row_id,
                is_free=False,
                billed_cost=record.list_total_cost,
                group=record.free_incentive_group,
                utc_day=record.created_at.date(),
            )

    # (請求スコープ, UTC日, グループ) 単位。ユーザーをまたいで枠を共有する。
    buckets: Dict[Tuple[str, date, str], List[AllocatableRecord]] = {}
    for record in eligible:
        key = (
            record.billing_scope_id or DEFAULT_BILLING_SCOPE_ID,
            record.created_at.date(),
            str(record.free_incentive_group),
        )
        buckets.setdefault(key, []).append(record)

    for (_scope, utc_day, group), bucket in buckets.items():
        limit = limits.get(group, 0)
        used = 0
        # 決定的な順序で処理する
        bucket.sort(key=lambda r: (r.created_at, r.row_id))
        for record in bucket:
            # cached_tokens は input_tokens の内数なので加算しない
            request_tokens = max(int(record.input_tokens or 0), 0) + max(
                int(record.output_tokens or 0), 0
            )
            if used + request_tokens <= limit:
                used += request_tokens
                result[record.row_id] = FreeTierAllocation(
                    row_id=record.row_id,
                    is_free=True,
                    billed_cost=record.list_tool_cost,
                    group=group,
                    utc_day=utc_day,
                )
            else:
                # 上限を少しでも超えるリクエストはリクエスト全体が有料。used も増やさない。
                result[record.row_id] = FreeTierAllocation(
                    row_id=record.row_id,
                    is_free=False,
                    billed_cost=record.list_total_cost,
                    group=group,
                    utc_day=utc_day,
                )

    return result


def free_tier_usage(
    records: Iterable[AllocatableRecord],
    config: FreeTierConfig,
) -> Dict[Tuple[date, str], int]:
    """(UTC日, group) → 無料化に使ったトークン数。"""
    items: List[AllocatableRecord] = list(records)
    allocations = allocate_free_tier(items, config)
    by_id = {record.row_id: record for record in items}
    usage: Dict[Tuple[date, str], int] = {}
    for row_id, allocation in allocations.items():
        if not allocation.is_free or allocation.group is None:
            continue
        record = by_id.get(row_id)
        if record is None:
            continue
        key = (allocation.utc_day, allocation.group)
        usage[key] = usage.get(key, 0) + max(int(record.input_tokens or 0), 0) + max(
            int(record.output_tokens or 0), 0
        )
    return usage
