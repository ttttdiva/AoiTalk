"""
トークン使用量・コスト追跡サービス

LLM API呼び出しごとのトークン消費量とコストを記録・集計する。
シングルトンとして動作し、ダッシュボード向けサマリーを提供する。

コストは ``src.services.pricing`` の versioned catalog / PricingEngine で算出する。
記録時には割引前の list cost を確定して保存し、OpenAIデータ共有無料枠を反映した
estimated billed cost は集計時に請求スコープ単位・UTC日界で決定的に割り当てる。
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, TypeVar, Union

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.database import get_db_session
from ..models.ecc_models import TokenUsage
from .pricing import (
    AllocatableRecord,
    CostBreakdown,
    FreeTierConfig,
    FreeTierAllocation,
    PricingStatus,
    UsageInput,
    allocate_free_tier,
    canonical_provider,
    get_pricing_engine,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

ZERO = Decimal("0")

#: 1回の集計でメモリへ読み込む token_usage 行数の上限。
#: 無料枠の割り当てが請求スコープ全体の履歴順序に依存するため、集計はSQLの
#: SUMではなくPython側で行う。暴走を避けるための安全弁。
MAX_AGGREGATION_ROWS = 500_000

# ────────────────────────────────────────────
# 日付ヘルパー
# ────────────────────────────────────────────

def _to_datetime(d: Union[date, datetime, str, None]) -> Optional[datetime]:
    """画面の日付はJST日界、日時はUTC naiveへ正規化する。"""
    if d is None:
        return None
    if isinstance(d, str):
        value = datetime.fromisoformat(d)
        if len(d) == 10:
            return value.replace(tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(
                ZoneInfo("UTC")
            ).replace(tzinfo=None)
        if value.tzinfo is not None:
            return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        return value
    if isinstance(d, date) and not isinstance(d, datetime):
        return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(
            ZoneInfo("UTC")
        ).replace(tzinfo=None)
    if isinstance(d, datetime) and d.tzinfo is not None:
        return d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return d


def _to_exclusive_end(d: Union[date, datetime, str, None]) -> Optional[datetime]:
    """画面の日付指定は当日を含め、日時指定はその値を排他的上限にする。"""
    value = _to_datetime(d)
    if value is None:
        return None
    if (isinstance(d, str) and len(d) == 10) or (
        isinstance(d, date) and not isinstance(d, datetime)
    ):
        return value + timedelta(days=1)
    return value


def _date_filter(column, start: Optional[datetime], end: Optional[datetime]):
    """SQLAlchemy の日付範囲フィルターリストを返す。"""
    filters = []
    if start is not None:
        filters.append(column >= start)
    if end is not None:
        filters.append(column < end)
    return filters


def _usage_filters(
    start: Optional[datetime], end: Optional[datetime], user_id: Optional[str]
) -> list[Any]:
    filters = _date_filter(TokenUsage.created_at, start, end)
    if user_id:
        filters.append(TokenUsage.user_id == str(user_id))
    return filters


def _utc_day_start(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _dec(value: Any) -> Decimal:
    """DBから読んだ値を安全にDecimalへ変換する。floatは文字列経由で通す。"""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _dec_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return _dec(value)


def _f(value: Optional[Decimal], digits: int = 10) -> float:
    """API返却用。Decimalをfloatへ落とす（表示専用。計算には使わない）。"""
    if value is None:
        return 0.0
    return round(float(value), digits)


# ────────────────────────────────────────────
# 集計用の行スナップショット
# ────────────────────────────────────────────


@dataclass(frozen=True)
class _UsageRow:
    row_id: str
    created_at: datetime
    user_id: Optional[str]
    project_id: Optional[str]
    agent_name: Optional[str]
    provider: str
    model: str
    requested_model: Optional[str]
    resolved_model: Optional[str]
    billing_scope_id: Optional[str]
    pricing_status: str
    free_incentive_group: Optional[str]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    prompt_eval_tokens: int
    list_input_cost: Decimal
    list_output_cost: Decimal
    list_tool_cost: Decimal
    list_total_cost: Decimal
    provider_reported_cost: Optional[Decimal]
    latency_ms: int

    @property
    def is_unpriced(self) -> bool:
        return self.pricing_status == PricingStatus.UNKNOWN

    @property
    def is_non_metered(self) -> bool:
        """サブスク/ローカル。従量課金の対象外なのでカバー率の母数から除く。"""
        return self.pricing_status in (PricingStatus.SUBSCRIPTION, PricingStatus.LOCAL)


def _row_from_orm(row: TokenUsage) -> _UsageRow:
    """ORM行を集計用スナップショットへ。未backfillの旧行は legacy total_cost を list として扱う。"""
    list_total = _dec_or_none(getattr(row, "list_total_cost", None))
    if list_total is None:
        # 新列が未設定の旧レコード。互換のため legacy total_cost を暫定の list cost とする。
        list_total = _dec(getattr(row, "total_cost", None))
        list_input = _dec(getattr(row, "input_cost", None))
        list_output = _dec(getattr(row, "output_cost", None))
    else:
        list_input = _dec(getattr(row, "list_input_cost", None))
        list_output = _dec(getattr(row, "list_output_cost", None))

    status = getattr(row, "pricing_status", None) or PricingStatus.UNKNOWN
    return _UsageRow(
        row_id=str(row.id),
        created_at=row.created_at or datetime.utcnow(),
        user_id=row.user_id,
        project_id=str(row.project_id) if row.project_id else None,
        agent_name=row.agent_name,
        provider=canonical_provider(row.provider),
        model=row.model,
        requested_model=getattr(row, "requested_model", None) or row.model,
        resolved_model=getattr(row, "resolved_model", None),
        billing_scope_id=getattr(row, "billing_scope_id", None) or "default",
        pricing_status=status,
        free_incentive_group=getattr(row, "free_incentive_group", None),
        input_tokens=int(row.input_tokens or 0),
        output_tokens=int(row.output_tokens or 0),
        total_tokens=int(row.total_tokens or 0),
        cached_tokens=int(row.cached_tokens or 0),
        cache_read_tokens=int(row.cache_read_tokens or 0),
        cache_write_tokens=int(row.cache_write_tokens or 0),
        reasoning_tokens=int(row.reasoning_tokens or 0),
        prompt_eval_tokens=int(row.prompt_eval_tokens or 0),
        list_input_cost=list_input,
        list_output_cost=list_output,
        list_tool_cost=_dec(getattr(row, "list_tool_cost", None)),
        list_total_cost=list_total,
        provider_reported_cost=_dec_or_none(getattr(row, "provider_reported_cost", None)),
        latency_ms=int(row.latency_ms or 0),
    )


# ────────────────────────────────────────────
# 集計アキュムレータ
# ────────────────────────────────────────────


class _Bucket:
    """1グループ分のトークン・コスト集計。"""

    def __init__(self) -> None:
        self.total_input = 0
        self.total_output = 0
        self.total_tokens = 0
        self.total_cached = 0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.total_reasoning = 0
        self.total_prompt_eval = 0
        self.request_count = 0
        self.latency_sum = 0
        self.list_cost = ZERO
        self.billed_cost = ZERO
        self.provider_reported_cost: Optional[Decimal] = None
        self.unpriced_request_count = 0
        self.unpriced_tokens = 0
        self.metered_request_count = 0
        self.free_request_count = 0
        self.statuses: set[str] = set()

    def add(self, row: _UsageRow, billed: Decimal, is_free: bool) -> None:
        self.total_input += row.input_tokens
        self.total_output += row.output_tokens
        self.total_tokens += row.total_tokens
        self.total_cached += row.cached_tokens
        self.total_cache_read += row.cache_read_tokens
        self.total_cache_write += row.cache_write_tokens
        self.total_reasoning += row.reasoning_tokens
        self.total_prompt_eval += row.prompt_eval_tokens
        self.request_count += 1
        self.latency_sum += row.latency_ms
        self.list_cost += row.list_total_cost
        self.billed_cost += billed
        if row.provider_reported_cost is not None:
            self.provider_reported_cost = (
                self.provider_reported_cost or ZERO
            ) + row.provider_reported_cost
        if row.is_unpriced:
            self.unpriced_request_count += 1
            self.unpriced_tokens += row.total_tokens
        if not row.is_non_metered:
            self.metered_request_count += 1
        if is_free:
            self.free_request_count += 1
            self.statuses.add(PricingStatus.FREE_INCENTIVE)
        else:
            self.statuses.add(row.pricing_status)

    @property
    def pricing_status(self) -> str:
        if not self.statuses:
            return PricingStatus.UNKNOWN
        if len(self.statuses) == 1:
            return next(iter(self.statuses))
        return "mixed"

    @property
    def coverage_percent(self) -> float:
        if self.metered_request_count <= 0:
            return 100.0
        priced = self.metered_request_count - self.unpriced_request_count
        return round(priced * 100.0 / self.metered_request_count, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total_tokens": self.total_tokens,
            "total_cached": self.total_cached,
            "total_cache_read": self.total_cache_read,
            "total_cache_write": self.total_cache_write,
            "total_reasoning": self.total_reasoning,
            "total_prompt_eval": self.total_prompt_eval,
            "request_count": self.request_count,
            "avg_latency_ms": (
                round(self.latency_sum / self.request_count, 1)
                if self.request_count
                else 0.0
            ),
            "list_cost": _f(self.list_cost),
            "estimated_billed_cost": _f(self.billed_cost),
            "savings": _f(self.list_cost - self.billed_cost),
            "provider_reported_cost": (
                _f(self.provider_reported_cost)
                if self.provider_reported_cost is not None
                else None
            ),
            # 互換: 既存クライアントは total_cost を参照する。定価換算を返す。
            "total_cost": _f(self.list_cost, 6),
            "unpriced_request_count": self.unpriced_request_count,
            "unpriced_tokens": self.unpriced_tokens,
            "pricing_coverage_percent": self.coverage_percent,
            "is_partial": self.unpriced_request_count > 0,
            "free_incentive_request_count": self.free_request_count,
            "pricing_status": self.pricing_status,
        }


def _empty_bucket_dict() -> Dict[str, Any]:
    return _Bucket().to_dict()


# ────────────────────────────────────────────
# サービス本体
# ────────────────────────────────────────────


class TokenTrackingService:
    """トークン使用量・コスト追跡のシングルトンサービス。"""

    _instance: Optional["TokenTrackingService"] = None

    def __new__(cls) -> "TokenTrackingService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ──────── 料金カタログ ────────

    async def ensure_pricing_catalog(self, *, refresh_openrouter: bool = True) -> Dict[str, Any]:
        """起動時に料金カタログをDBへ同期し、エンジンのキャッシュを温める。

        `config/pricing_catalog.json` を idempotent に upsert し、必要なら
        OpenRouter の Models API を TTL 付きで取り込む。失敗しても例外は投げず、
        last-known-good の料金表をそのまま使い続ける。
        """
        result: Dict[str, Any] = {"catalog": None, "openrouter": None}
        try:
            from .pricing.catalog import sync_catalog_to_db

            result["catalog"] = await sync_catalog_to_db()
        except Exception as exc:
            logger.exception("料金カタログのDB同期に失敗しました")
            result["catalog"] = {"status": "error", "error": str(exc)}

        if refresh_openrouter:
            try:
                from .pricing.updater import refresh_openrouter_catalog

                result["openrouter"] = await refresh_openrouter_catalog()
            except Exception as exc:
                logger.warning("OpenRouter料金表の更新に失敗しました: %s", exc)
                result["openrouter"] = {"status": "error", "error": str(exc)}

        try:
            await get_pricing_engine().ensure_loaded(force=True)
        except Exception:
            logger.exception("料金キャッシュの読み込みに失敗しました")
        return result

    def _free_tier_config(self) -> FreeTierConfig:
        """アプリ設定からOpenAIデータ共有無料枠の設定を読む。"""
        try:
            from ..app_config_store import load_app_config_sync

            cfg = load_app_config_sync() or {}
            openai_cfg = cfg.get("openai") or {}
            if not isinstance(openai_cfg, Mapping):
                openai_cfg = {}
            tier = str(openai_cfg.get("usage_tier") or "tier_1_2")
            if tier not in ("tier_1_2", "tier_3_plus"):
                tier = "tier_1_2"
            scope = str(openai_cfg.get("billing_scope_id") or "").strip() or None
            return FreeTierConfig(
                enabled=bool(openai_cfg.get("data_sharing_incentive_enabled", False)),
                tier=tier,
                billing_scope_id=scope,
            )
        except Exception:
            logger.debug("無料枠設定の読み込みに失敗しました。無効として扱います", exc_info=True)
            return FreeTierConfig()

    # ──────── 記録 ────────

    async def record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        requested_model: Optional[str] = None,
        resolved_model: Optional[str] = None,
        billing_scope_id: Optional[str] = None,
        provider_reported_cost: Any = None,
        provider_reported_cost_details: Optional[Mapping[str, Any]] = None,
        tool_invocations: Optional[Mapping[str, int]] = None,
        cached_tokens: int = 0,
        cache_read_tokens: Optional[int] = None,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        prompt_eval_tokens: int = 0,
        prompt_eval_ms: int = 0,
        cache_hit_rate: Optional[float] = None,
        cache_evictions: int = 0,
        cache_provider: Optional[str] = None,
        cache_mode: Optional[str] = None,
        cache_key: Optional[str] = None,
        cache_supported: Optional[bool] = None,
        cache_active: Optional[bool] = None,
        metrics_source: Optional[str] = None,
        session_id: Optional[uuid.UUID] = None,
        user_id: Optional[str] = None,
        project_id: Optional[uuid.UUID] = None,
        agent_name: Optional[str] = None,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """API呼び出し1回分のトークン使用量とlist costを記録する。

        list cost（割引前の定価換算）は**記録時に確定**する。無料枠を反映した
        推定請求額は履歴順序に依存するため保存せず、集計時に計算する。

        Returns:
            記録した行の辞書表現。失敗時は None。
        """
        try:
            cache_read_tokens = (
                int(cached_tokens or 0)
                if cache_read_tokens is None
                else int(cache_read_tokens or 0)
            )
            cached_tokens = max(int(cached_tokens or 0), 0)
            cache_read_tokens = max(int(cache_read_tokens or 0), 0)
            cache_write_tokens = max(int(cache_write_tokens or 0), 0)
            input_tokens = max(int(input_tokens or 0), 0)
            output_tokens = max(int(output_tokens or 0), 0)

            requested = str(requested_model or model or "")
            scope = (
                str(billing_scope_id).strip()
                if billing_scope_id
                else (self._free_tier_config().billing_scope_id or "default")
            )

            breakdown = await self._calculate_breakdown(
                UsageInput(
                    provider=provider,
                    requested_model=requested,
                    resolved_model=resolved_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    tool_invocations=dict(tool_invocations) if tool_invocations else None,
                    provider_reported_cost=_dec_or_none(provider_reported_cost),
                    provider_reported_cost_details=(
                        dict(provider_reported_cost_details)
                        if provider_reported_cost_details
                        else None
                    ),
                    occurred_at=datetime.utcnow(),
                )
            )

            row = TokenUsage(
                id=uuid.uuid4(),
                session_id=session_id,
                user_id=user_id,
                project_id=project_id,
                provider=provider,
                model=model,
                requested_model=requested,
                resolved_model=resolved_model,
                billing_scope_id=scope,
                agent_name=agent_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cached_tokens=cache_read_tokens,
                reasoning_tokens=max(int(reasoning_tokens or 0), 0),
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                prompt_eval_tokens=max(int(prompt_eval_tokens or 0), 0),
                prompt_eval_ms=max(int(prompt_eval_ms or 0), 0),
                cache_hit_rate=cache_hit_rate,
                cache_evictions=max(int(cache_evictions or 0), 0),
                cache_provider=cache_provider,
                cache_mode=cache_mode,
                cache_key=cache_key,
                cache_supported=cache_supported,
                cache_active=cache_active,
                metrics_source=metrics_source,
                pricing_status=breakdown.pricing_status,
                pricing_catalog_version=breakdown.catalog_version,
                pricing_rule_id=breakdown.rule_id,
                free_incentive_group=breakdown.free_incentive_group,
                applied_input_rate=breakdown.applied_input_rate,
                applied_cached_input_rate=breakdown.applied_cached_input_rate,
                applied_cache_write_rate=breakdown.applied_cache_write_rate,
                applied_output_rate=breakdown.applied_output_rate,
                list_input_cost=breakdown.list_input_cost,
                list_output_cost=breakdown.list_output_cost,
                list_tool_cost=breakdown.list_tool_cost,
                list_total_cost=breakdown.list_total_cost,
                provider_reported_cost=breakdown.provider_reported_cost,
                provider_reported_cost_details=breakdown.provider_reported_cost_details,
                tool_invocations=dict(tool_invocations) if tool_invocations else None,
                # 互換列。正本は list_* / provider_reported_cost。
                input_cost=float(breakdown.list_input_cost),
                output_cost=float(breakdown.list_output_cost),
                total_cost=float(breakdown.list_total_cost),
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )

            async with await get_db_session() as session:
                session.add(row)
                await session.commit()

            logger.debug(
                "トークン使用量を記録: %s/%s (resolved=%s) in=%d out=%d status=%s list=$%s",
                provider, model, resolved_model, input_tokens, output_tokens,
                breakdown.pricing_status, breakdown.list_total_cost,
            )
            return row.to_dict()

        except Exception:
            logger.exception("トークン使用量の記録に失敗しました")
            return None

    async def _calculate_breakdown(self, usage: UsageInput) -> CostBreakdown:
        """料金エンジンでコストを算出する。エンジン障害時も記録は続行する。"""
        engine = get_pricing_engine()
        try:
            await engine.ensure_loaded()
        except Exception:
            logger.exception("料金カタログの読み込みに失敗しました")
        return engine.calculate(usage)

    # ──────── 行の読み込みと無料枠割り当て ────────

    async def _load_rows(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
        user_id: Optional[str] = None,
    ) -> List[_UsageRow]:
        async with await get_db_session() as session:
            stmt = (
                select(TokenUsage)
                .where(and_(*_usage_filters(start, end, user_id)))
                .order_by(TokenUsage.created_at, TokenUsage.id)
                .limit(MAX_AGGREGATION_ROWS)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
        if len(rows) >= MAX_AGGREGATION_ROWS:
            logger.warning(
                "集計対象が上限 %d 行に達しました。結果は部分集計です", MAX_AGGREGATION_ROWS
            )
        return [_row_from_orm(r) for r in rows]

    async def _allocations(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
        config: Optional[FreeTierConfig] = None,
    ) -> Dict[str, FreeTierAllocation]:
        allocations, _ = await self._allocations_with_status(start, end, config)
        return allocations

    async def _allocations_with_status(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
        config: Optional[FreeTierConfig] = None,
    ) -> tuple[Dict[str, FreeTierAllocation], bool]:
        """指定期間に重なるUTC日について、請求スコープ全体で無料枠を割り当てる。

        ユーザー別画面でも先にスコープ全体で割り当てる必要があるため、
        user_id では絞り込まずに読み込む。戻り値の2番目は、履歴行数の
        上限に達して割当が部分的になったかどうかを示す。
        """
        cfg = config if config is not None else self._free_tier_config()
        if not cfg.enabled:
            return {}, False

        # JSTの1日が複数のUTC無料枠日にまたがるため、範囲をUTC日界へ拡張する。
        scope_start = _utc_day_start(start) if start else None
        scope_end = end
        if scope_end is not None:
            day_start = _utc_day_start(scope_end)
            scope_end = day_start + timedelta(days=1)

        rows = await self._load_rows(scope_start, scope_end, user_id=None)
        is_partial = len(rows) >= MAX_AGGREGATION_ROWS
        records = [
            AllocatableRecord(
                row_id=r.row_id,
                created_at=r.created_at,
                provider=r.provider,
                billing_scope_id=r.billing_scope_id,
                free_incentive_group=r.free_incentive_group,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                list_token_cost=r.list_input_cost + r.list_output_cost,
                list_tool_cost=r.list_tool_cost,
                list_total_cost=r.list_total_cost,
                pricing_status=r.pricing_status,
            )
            for r in rows
        ]
        return allocate_free_tier(records, cfg), is_partial

    def _billed_cost(
        self,
        row: _UsageRow,
        allocations: Mapping[str, FreeTierAllocation],
        include_free_incentive: bool,
    ) -> tuple[Decimal, bool]:
        """(推定請求額, 無料枠が適用されたか) を返す。"""
        if row.provider_reported_cost is not None:
            # プロバイダ報告額が実請求額。無料枠の推定は適用しない。
            return row.provider_reported_cost, False
        if include_free_incentive:
            allocation = allocations.get(row.row_id)
            if allocation is not None and allocation.is_free:
                return allocation.billed_cost, True
        return row.list_total_cost, False

    async def _aggregate(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
        user_id: Optional[str],
        key_fn: Callable[[_UsageRow], Any],
        *,
        include_free_incentive: bool = True,
        row_filter: Optional[Callable[[_UsageRow], bool]] = None,
    ) -> tuple[Dict[Any, _Bucket], List[_UsageRow]]:
        allocations = (
            await self._allocations(start, end) if include_free_incentive else {}
        )
        rows = await self._load_rows(start, end, user_id)
        return self._aggregate_loaded_rows(
            rows,
            allocations,
            key_fn,
            include_free_incentive=include_free_incentive,
            row_filter=row_filter,
        )

    def _aggregate_loaded_rows(
        self,
        rows: Sequence[_UsageRow],
        allocations: Mapping[str, FreeTierAllocation],
        key_fn: Callable[[_UsageRow], Any],
        *,
        include_free_incentive: bool = True,
        row_filter: Optional[Callable[[_UsageRow], bool]] = None,
    ) -> tuple[Dict[Any, _Bucket], List[_UsageRow]]:
        """既に読み込んだ行を、指定キーで集計する。

        ダッシュボードは同じ期間の行から複数の切り口を作るため、DBからの
        再読込を避けてこのヘルパーを使う。無料枠の割当結果も呼び出し側で
        一度だけ作り、各切り口で共有する。
        """
        buckets: Dict[Any, _Bucket] = defaultdict(_Bucket)
        kept: List[_UsageRow] = []
        for row in rows:
            if row_filter is not None and not row_filter(row):
                continue
            kept.append(row)
            billed, is_free = self._billed_cost(
                row, allocations, include_free_incentive
            )
            buckets[key_fn(row)].add(row, billed, is_free)
        return buckets, kept

    # ──────── 集計クエリ ────────

    async def get_daily_summary(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """日別（JST日界）のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        jst = ZoneInfo("Asia/Tokyo")

        def _key(row: _UsageRow) -> str:
            return (
                row.created_at.replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(jst)
                .strftime("%Y-%m-%d")
            )

        try:
            buckets, _ = await self._aggregate(
                start, end, user_id, _key, include_free_incentive=include_free_incentive
            )
            return [
                {"date": day, **buckets[day].to_dict()}
                for day in sorted(buckets)
            ]
        except Exception:
            logger.exception("日別サマリーの取得に失敗しました")
            return []

    async def get_daily_summary_by_model(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """日別・モデル別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        jst = ZoneInfo("Asia/Tokyo")

        def _key(row: _UsageRow) -> tuple[str, str, str, str, str]:
            day = (
                row.created_at.replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(jst)
                .strftime("%Y-%m-%d")
            )
            return (
                day,
                row.provider,
                row.model,
                row.resolved_model or "",
                row.free_incentive_group or "",
            )

        try:
            buckets, _ = await self._aggregate(
                start,
                end,
                user_id,
                _key,
                include_free_incentive=include_free_incentive,
            )
            items = [
                {
                    "date": day,
                    "provider": provider,
                    "model": model,
                    "resolved_model": resolved or None,
                    "free_incentive_group": free_group or None,
                    **bucket.to_dict(),
                }
                for (day, provider, model, resolved, free_group), bucket in buckets.items()
            ]
            items.sort(
                key=lambda item: (
                    item["date"],
                    item["provider"],
                    item["model"],
                    item.get("resolved_model") or "",
                )
            )
            return items
        except Exception:
            logger.exception("日別モデル別サマリーの取得に失敗しました")
            return []

    async def get_summary_by_model(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """モデル別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        try:
            buckets, _ = await self._aggregate(
                start,
                end,
                user_id,
                lambda r: (r.provider, r.model, r.resolved_model or ""),
                include_free_incentive=include_free_incentive,
            )
            items = [
                {
                    "provider": provider,
                    "model": model,
                    "resolved_model": resolved or None,
                    **bucket.to_dict(),
                }
                for (provider, model, resolved), bucket in buckets.items()
            ]
            items.sort(key=lambda x: x["list_cost"], reverse=True)
            return items
        except Exception:
            logger.exception("モデル別サマリーの取得に失敗しました")
            return []

    async def get_summary_by_project(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """プロジェクト別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        try:
            buckets, _ = await self._aggregate(
                start,
                end,
                user_id,
                lambda r: r.project_id,
                include_free_incentive=include_free_incentive,
            )
            project_labels = await self._project_labels(list(buckets.keys()))
            items = [
                {
                    "project_id": str(project_id) if project_id else None,
                    "project_name": (
                        project_labels.get(str(project_id))
                        if project_id
                        else "未設定"
                    )
                    or (str(project_id) if project_id else "未設定"),
                    **bucket.to_dict(),
                }
                for project_id, bucket in buckets.items()
            ]
            items.sort(key=lambda x: x["list_cost"], reverse=True)
            return items
        except Exception:
            logger.exception("プロジェクト別サマリーの取得に失敗しました")
            return []

    async def _project_labels(self, raw_ids: Sequence[Any]) -> Dict[str, str]:
        """プロジェクトIDからユーザー向けのプロジェクト名を解決する。"""
        project_ids: list[uuid.UUID] = []
        for raw_id in raw_ids:
            if not raw_id:
                continue
            try:
                project_ids.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError, AttributeError):
                continue
        if not project_ids:
            return {}

        try:
            from ..memory.models.projects import Project

            async with await get_db_session() as session:
                rows = (
                    await session.execute(
                        select(Project.id, Project.name).where(
                            Project.id.in_(project_ids)
                        )
                    )
                ).all()
            return {
                str(project_id): str(name or project_id)
                for project_id, name in rows
            }
        except Exception:
            logger.debug("プロジェクト名の解決に失敗しました", exc_info=True)
            return {}

    async def get_summary_by_agent(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """エージェント別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        try:
            buckets, _ = await self._aggregate(
                start,
                end,
                user_id,
                lambda r: r.agent_name,
                include_free_incentive=include_free_incentive,
                row_filter=lambda r: r.agent_name is not None,
            )
            agent_labels = await self._agent_labels(list(buckets.keys()))
            items = [
                {
                    "agent_id": str(agent_name),
                    "agent_name": agent_labels.get(str(agent_name))
                    or agent_labels.get(str(agent_name).casefold())
                    or str(agent_name),
                    **bucket.to_dict(),
                }
                for agent_name, bucket in buckets.items()
            ]
            items.sort(key=lambda x: x["list_cost"], reverse=True)
            return items
        except Exception:
            logger.exception("エージェント別サマリーの取得に失敗しました")
            return []

    async def _agent_labels(self, raw_names: Sequence[Any]) -> Dict[str, str]:
        """キャラクターのslug/名前からユーザー向け表示名を解決する。"""
        names = [str(raw).strip() for raw in raw_names if str(raw or "").strip()]
        if not names:
            return {}

        try:
            from ..models.ecc_models import Character

            async with await get_db_session() as session:
                rows = (
                    await session.execute(
                        select(Character.slug, Character.name).where(
                            or_(Character.slug.in_(names), Character.name.in_(names))
                        )
                    )
                ).all()
            labels: Dict[str, str] = {}
            for slug, name in rows:
                display_name = str(name or slug)
                for value in (slug, name):
                    if value:
                        text = str(value)
                        labels[text] = display_name
                        labels[text.casefold()] = display_name
            return labels
        except Exception:
            logger.debug("エージェント名の解決に失敗しました", exc_info=True)
            return {}

    async def get_summary_by_user(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        *,
        include_free_incentive: bool = True,
    ) -> List[Dict[str, Any]]:
        """管理者向けにユーザー別使用量を返す。

        無料枠は請求スコープ全体で先に割り当ててから、ユーザー単位へ集約する。
        """
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        try:
            buckets, _ = await self._aggregate(
                start,
                end,
                None,
                lambda r: r.user_id,
                include_free_incentive=include_free_incentive,
            )
            user_labels = await self._user_labels(list(buckets.keys()))
            items = [
                {
                    "user_id": user_id or "unknown",
                    "user_name": user_labels.get(str(user_id), user_id or "不明"),
                    **bucket.to_dict(),
                }
                for user_id, bucket in buckets.items()
            ]
            items.sort(key=lambda x: x["total_tokens"], reverse=True)
            return items
        except Exception:
            logger.exception("ユーザー別サマリーの取得に失敗しました")
            return []

    async def _user_labels(self, raw_ids: Sequence[Any]) -> Dict[str, str]:
        user_ids = []
        for value in raw_ids:
            try:
                user_ids.append(uuid.UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                continue
        if not user_ids:
            return {}
        try:
            from ..memory.models import User

            async with await get_db_session() as session:
                users = (
                    (await session.execute(select(User).where(User.id.in_(user_ids))))
                    .scalars()
                    .all()
                )
            return {str(u.id): u.display_name or u.username for u in users}
        except Exception:
            logger.debug("ユーザー名の解決に失敗しました", exc_info=True)
            return {}

    async def get_total_cost(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> Dict[str, Any]:
        """指定期間の合計コスト・トークン数を返す。"""
        start = _to_datetime(start_date)
        end = _to_exclusive_end(end_date)
        try:
            buckets, _ = await self._aggregate(
                start, end, user_id, lambda r: "all",
                include_free_incentive=include_free_incentive,
            )
            bucket = buckets.get("all", _Bucket())
            return {
                **bucket.to_dict(),
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
            }
        except Exception:
            logger.exception("合計コストの取得に失敗しました")
            return {
                **_empty_bucket_dict(),
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
            }

    # ──────── 無料枠の残量 ────────

    async def get_free_tier_status(self) -> Dict[str, Any]:
        """本日（UTC日界）の無料枠使用量・上限・残量を返す。"""
        from .pricing.free_tier import FREE_TIER_LIMITS

        config = self._free_tier_config()
        now_utc = datetime.utcnow()
        day_start = _utc_day_start(now_utc)
        day_end = day_start + timedelta(days=1)
        limits = FREE_TIER_LIMITS.get(config.tier, FREE_TIER_LIMITS["tier_1_2"])

        used: Dict[str, int] = {"1m": 0, "10m": 0}
        if config.enabled:
            try:
                allocations = await self._allocations(day_start, day_end, config)
                rows = {r.row_id: r for r in await self._load_rows(day_start, day_end)}
                for row_id, allocation in allocations.items():
                    if not allocation.is_free or allocation.group not in used:
                        continue
                    row = rows.get(row_id)
                    if row is None:
                        continue
                    used[allocation.group] += row.input_tokens + row.output_tokens
            except Exception:
                logger.exception("無料枠使用量の取得に失敗しました")

        return {
            "enabled": config.enabled,
            "tier": config.tier,
            "billing_scope_id": config.billing_scope_id or "default",
            "utc_date": day_start.strftime("%Y-%m-%d"),
            "groups": [
                {
                    "group": group,
                    "used_tokens": used[group],
                    "limit_tokens": limits[group],
                    "remaining_tokens": max(limits[group] - used[group], 0),
                }
                for group in ("1m", "10m")
            ],
        }

    # ──────── ダッシュボード向けサマリー ────────

    async def _get_dashboard_summary_legacy(
        self,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> Dict[str, Any]:
        """行数上限に達した場合の従来型ダッシュボード集計。

        広い共有期間を1回で読むと、個別集計ごとに存在していた
        ``MAX_AGGREGATION_ROWS`` の上限単位が変わるため、上限到達時だけ
        既存経路へ戻して部分集計の意味を維持する。
        """
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        today_jst = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = today_jst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        trend_start = today_start - timedelta(days=29)
        month_start_jst = today_jst.replace(day=1)
        month_start = month_start_jst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        month_ago = today_start - timedelta(days=30)
        tomorrow = today_start + timedelta(days=1)

        today_cost = await self.get_total_cost(
            today_start,
            tomorrow,
            user_id,
            include_free_incentive=include_free_incentive,
        )
        monthly_total = await self.get_total_cost(
            month_start,
            tomorrow,
            user_id,
            include_free_incentive=include_free_incentive,
        )
        trend_rows = await self.get_daily_summary(
            trend_start,
            tomorrow,
            user_id,
            include_free_incentive=include_free_incentive,
        )
        daily_model_trend = await self.get_daily_summary_by_model(
            trend_start,
            tomorrow,
            user_id,
            include_free_incentive=include_free_incentive,
        )
        trend_by_date = {str(row.get("date")): row for row in trend_rows}
        daily_trend = []
        for offset in range(30):
            day = trend_start + timedelta(days=offset)
            day_key = (
                day.replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(ZoneInfo("Asia/Tokyo"))
                .strftime("%Y-%m-%d")
            )
            daily_trend.append(
                {"date": day_key, **trend_by_date.get(day_key, _empty_bucket_dict())}
            )
        model_breakdown = await self.get_summary_by_model(
            month_ago,
            tomorrow,
            user_id,
            include_free_incentive=include_free_incentive,
        )
        free_tier = await self.get_free_tier_status()
        pricing = await self.get_pricing_status()
        return {
            "today": today_cost,
            "monthly_total": monthly_total,
            "daily_trend": daily_trend,
            "daily_model_trend": daily_model_trend,
            "weekly_trend": daily_trend[-7:],
            "model_breakdown": model_breakdown,
            "free_tier": free_tier,
            "pricing": pricing,
            "generated_at": now.isoformat(),
        }

    async def get_dashboard_summary(
        self,
        user_id: Optional[str] = None,
        *,
        include_free_incentive: bool = True,
    ) -> Dict[str, Any]:
        """ダッシュボード表示用のサマリーを一括で返す。

        - 今日のコスト
        - 過去30日の日別推移
        - 今月（JST）の合計コスト
        - モデル別内訳（過去30日）
        - 無料枠の残量と料金カタログの状態
        """
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        today_jst = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = today_jst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        trend_start = today_start - timedelta(days=29)
        month_start_jst = today_jst.replace(day=1)
        month_start = month_start_jst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        month_ago = today_start - timedelta(days=30)
        tomorrow = today_start + timedelta(days=1)

        # 今日/月間/30日推移/モデル別は、最も広い期間の行を一度だけ読み込んで
        # メモリ上で切り分ける。従来は各集計メソッドが同じ token_usage 行を
        # 個別に読み直していたため、ダッシュボードを開くだけで複数回の全走査が
        # 発生していた。
        aggregate_start = min(trend_start, month_start, month_ago)
        allocations_partial = False
        if include_free_incentive:
            try:
                allocations, allocations_partial = await self._allocations_with_status(
                    aggregate_start, tomorrow
                )
            except Exception:
                logger.exception("ダッシュボードの無料枠割当に失敗しました")
                allocations = {}
        else:
            allocations = {}

        try:
            rows = await self._load_rows(aggregate_start, tomorrow, user_id)
        except Exception:
            logger.exception("ダッシュボードの使用量行取得に失敗しました")
            rows = []

        if allocations_partial or len(rows) >= MAX_AGGREGATION_ROWS:
            logger.warning(
                "共有ダッシュボード集計が上限 %d 行に達したため、従来経路へフォールバックします",
                MAX_AGGREGATION_ROWS,
            )
            return await self._get_dashboard_summary_legacy(
                user_id,
                include_free_incentive=include_free_incentive,
            )

        def _in_range(row: _UsageRow, start: datetime, end: datetime) -> bool:
            return start <= row.created_at < end

        def _jst_day(row: _UsageRow) -> str:
            return (
                row.created_at.replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(ZoneInfo("Asia/Tokyo"))
                .strftime("%Y-%m-%d")
            )

        def _range_bucket(start: datetime, end: datetime) -> Dict[str, Any]:
            buckets, _ = self._aggregate_loaded_rows(
                rows,
                allocations,
                lambda _row: "all",
                include_free_incentive=include_free_incentive,
                row_filter=lambda row: _in_range(row, start, end),
            )
            return buckets.get("all", _Bucket()).to_dict()

        today_cost = {
            **_range_bucket(today_start, tomorrow),
            "start_date": today_start.isoformat(),
            "end_date": tomorrow.isoformat(),
        }
        monthly_total = {
            **_range_bucket(month_start, tomorrow),
            "start_date": month_start.isoformat(),
            "end_date": tomorrow.isoformat(),
        }

        trend_buckets, _ = self._aggregate_loaded_rows(
            rows,
            allocations,
            _jst_day,
            include_free_incentive=include_free_incentive,
            row_filter=lambda row: _in_range(row, trend_start, tomorrow),
        )
        trend_by_date = {
            str(day): bucket.to_dict() for day, bucket in trend_buckets.items()
        }
        daily_trend = []
        for offset in range(30):
            day = trend_start + timedelta(days=offset)
            day_key = (
                day.replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(ZoneInfo("Asia/Tokyo"))
                .strftime("%Y-%m-%d")
            )
            daily_trend.append(
                {"date": day_key, **trend_by_date.get(day_key, _empty_bucket_dict())}
            )

        daily_model_buckets, _ = self._aggregate_loaded_rows(
            rows,
            allocations,
            lambda row: (
                _jst_day(row),
                row.provider,
                row.model,
                row.resolved_model or "",
                row.free_incentive_group or "",
            ),
            include_free_incentive=include_free_incentive,
            row_filter=lambda row: _in_range(row, trend_start, tomorrow),
        )
        daily_model_trend = [
            {
                "date": day,
                "provider": provider,
                "model": model,
                "resolved_model": resolved or None,
                "free_incentive_group": free_group or None,
                **bucket.to_dict(),
            }
            for (
                day,
                provider,
                model,
                resolved,
                free_group,
            ), bucket in daily_model_buckets.items()
        ]
        daily_model_trend.sort(
            key=lambda item: (
                item["date"],
                item["provider"],
                item["model"],
                item.get("resolved_model") or "",
            )
        )

        model_buckets, _ = self._aggregate_loaded_rows(
            rows,
            allocations,
            lambda row: (row.provider, row.model, row.resolved_model or ""),
            include_free_incentive=include_free_incentive,
            row_filter=lambda row: _in_range(row, month_ago, tomorrow),
        )
        model_breakdown = [
            {
                "provider": provider,
                "model": model,
                "resolved_model": resolved or None,
                **bucket.to_dict(),
            }
            for (provider, model, resolved), bucket in model_buckets.items()
        ]
        model_breakdown.sort(key=lambda item: item["list_cost"], reverse=True)

        free_tier = await self.get_free_tier_status()
        pricing = await self.get_pricing_status()

        return {
            "today": today_cost,
            "monthly_total": monthly_total,
            "daily_trend": daily_trend,
            "daily_model_trend": daily_model_trend,
            # 旧クライアントとの互換用。従来どおり直近7点を返す。
            "weekly_trend": daily_trend[-7:],
            "model_breakdown": model_breakdown,
            "free_tier": free_tier,
            "pricing": pricing,
            "generated_at": now.isoformat(),
        }

    async def get_pricing_status(self) -> Dict[str, Any]:
        """料金カタログのバージョン・最終更新日時などを返す。"""
        try:
            from .pricing.updater import get_pricing_catalog_status

            return await get_pricing_catalog_status()
        except Exception:
            logger.exception("料金カタログ状態の取得に失敗しました")
            engine = get_pricing_engine()
            return {
                "catalog_version": engine.catalog_version,
                "rule_count": engine.rule_count,
                "sources": [],
            }


# ────────────────────────────────────────────
# シングルトンアクセス
# ────────────────────────────────────────────


def get_token_tracking_service() -> TokenTrackingService:
    """グローバルな TokenTrackingService インスタンスを返す。"""
    return TokenTrackingService()


# ────────────────────────────────────────────
# デコレータ
# ────────────────────────────────────────────


def track_tokens(
    provider: str,
    model: str,
    *,
    agent_name: Optional[str] = None,
    request_type: str = "chat",
):
    """非同期 LLM 呼び出し関数をラップし、トークン使用量を自動記録するデコレータ。

    ラップ対象の関数は、以下のいずれかを返す必要がある:
      1) ``{"input_tokens": int, "output_tokens": int, ...}`` を含む辞書
      2) ``usage`` 属性に上記相当のオブジェクトを持つレスポンス

    使用例::

        @track_tokens(provider="openai", model="gpt-5.6")
        async def call_openai(messages):
            response = await client.chat.completions.create(...)
            return response

    Notes:
        - キーワード引数 ``session_id``, ``user_id``, ``project_id`` が
          ラップ対象関数に渡されていれば、記録にも使用する。
        - 関数が例外を送出した場合は記録せずそのまま再送出する。
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.monotonic()
            result = await fn(*args, **kwargs)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            # レスポンスからトークン情報を抽出
            input_tokens = 0
            output_tokens = 0
            cached_tokens = 0
            cache_read_tokens = 0
            cache_write_tokens = 0
            reasoning_tokens = 0
            prompt_eval_tokens = 0
            prompt_eval_ms = 0
            resolved_model = None
            provider_reported_cost = None
            provider_reported_cost_details = None

            if isinstance(result, dict):
                usage = result.get("usage", result)
                input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                cached_tokens = usage.get("cached_tokens", 0)
                cache_read_tokens = usage.get("cache_read_tokens", cached_tokens) or 0
                cache_write_tokens = usage.get("cache_write_tokens", 0) or 0
                reasoning_tokens = usage.get("reasoning_tokens", 0) or 0
                prompt_eval_tokens = usage.get("prompt_eval_tokens", 0) or 0
                prompt_eval_ms = usage.get("prompt_eval_ms", 0) or 0
                resolved_model = usage.get("resolved_model") or result.get("model")
                provider_reported_cost = usage.get("provider_reported_cost")
                provider_reported_cost_details = usage.get("provider_reported_cost_details")
            elif hasattr(result, "usage") and result.usage is not None:
                usage = result.usage
                input_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
                output_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)
                cached_tokens = getattr(usage, "cached_tokens", 0) or 0
                cache_read_tokens = getattr(usage, "cache_read_tokens", cached_tokens) or 0
                cache_write_tokens = getattr(usage, "cache_write_tokens", 0) or 0
                reasoning_tokens = getattr(usage, "reasoning_tokens", 0) or 0
                prompt_eval_tokens = getattr(usage, "prompt_eval_tokens", 0) or 0
                prompt_eval_ms = getattr(usage, "prompt_eval_ms", 0) or 0
                resolved_model = getattr(result, "model", None)
                provider_reported_cost = getattr(usage, "cost", None)
                provider_reported_cost_details = getattr(usage, "cost_details", None)

            if input_tokens or output_tokens:
                service = get_token_tracking_service()
                await service.record_usage(
                    provider=provider,
                    model=model,
                    requested_model=model,
                    resolved_model=resolved_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    prompt_eval_tokens=prompt_eval_tokens,
                    prompt_eval_ms=prompt_eval_ms,
                    provider_reported_cost=provider_reported_cost,
                    provider_reported_cost_details=provider_reported_cost_details,
                    session_id=kwargs.get("session_id"),
                    user_id=kwargs.get("user_id"),
                    project_id=kwargs.get("project_id"),
                    agent_name=agent_name,
                    request_type=request_type,
                    latency_ms=elapsed_ms,
                    is_streaming=kwargs.get("stream", False),
                )

            return result

        return wrapper  # type: ignore[return-value]

    return decorator
