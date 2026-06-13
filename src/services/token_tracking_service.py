"""
トークン使用量・コスト追跡サービス

LLM API呼び出しごとのトークン消費量とコストを記録・集計する。
シングルトンとして動作し、ダッシュボード向けサマリーを提供する。
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.database import get_db_session
from ..models.ecc_models import ModelPricing, TokenUsage

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ────────────────────────────────────────────
# デフォルト料金データ（USD per 1M tokens）
# ────────────────────────────────────────────

DEFAULT_PRICING: List[Dict[str, Any]] = [
    # OpenAI
    {
        "provider": "openai",
        "model": "gpt-4o",
        "input_price_per_1m": 2.50,
        "output_price_per_1m": 10.00,
        "cached_input_price_per_1m": 1.25,
    },
    {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "input_price_per_1m": 0.15,
        "output_price_per_1m": 0.60,
        "cached_input_price_per_1m": 0.075,
    },
    {
        "provider": "openai",
        "model": "o4-mini",
        "input_price_per_1m": 1.10,
        "output_price_per_1m": 4.40,
        "cached_input_price_per_1m": 0.55,
    },
    # Google
    {
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "input_price_per_1m": 0.10,
        "output_price_per_1m": 0.40,
        "cached_input_price_per_1m": 0.025,
    },
    {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "input_price_per_1m": 1.25,
        "output_price_per_1m": 10.00,
        "cached_input_price_per_1m": 0.315,
    },
    # Anthropic
    {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "input_price_per_1m": 3.00,
        "output_price_per_1m": 15.00,
        "cached_input_price_per_1m": 0.30,
    },
    {
        "provider": "anthropic",
        "model": "claude-haiku-3.5",
        "input_price_per_1m": 0.80,
        "output_price_per_1m": 4.00,
        "cached_input_price_per_1m": 0.08,
    },
]


# ────────────────────────────────────────────
# ヘルパー
# ────────────────────────────────────────────

def _to_datetime(d: Union[date, datetime, str, None]) -> Optional[datetime]:
    """date / datetime / ISO文字列 を datetime に正規化する。"""
    if d is None:
        return None
    if isinstance(d, str):
        return datetime.fromisoformat(d)
    if isinstance(d, date) and not isinstance(d, datetime):
        return datetime(d.year, d.month, d.day)
    return d


def _date_filter(column, start: Optional[datetime], end: Optional[datetime]):
    """SQLAlchemy の日付範囲フィルターリストを返す。"""
    filters = []
    if start is not None:
        filters.append(column >= start)
    if end is not None:
        filters.append(column < end)
    return filters


# ────────────────────────────────────────────
# サービス本体
# ────────────────────────────────────────────

class TokenTrackingService:
    """トークン使用量・コスト追跡のシングルトンサービス。"""

    _instance: Optional["TokenTrackingService"] = None
    _pricing_cache: Dict[str, ModelPricing] = {}
    _pricing_loaded: bool = False

    def __new__(cls) -> "TokenTrackingService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ──────── 料金テーブル ────────

    async def ensure_default_pricing(self) -> int:
        """デフォルト料金データがDBに存在しなければ挿入する。

        Returns:
            挿入した行数
        """
        inserted = 0
        try:
            async with await get_db_session() as session:
                for entry in DEFAULT_PRICING:
                    existing = await session.execute(
                        select(ModelPricing).where(
                            and_(
                                ModelPricing.provider == entry["provider"],
                                ModelPricing.model == entry["model"],
                            )
                        )
                    )
                    if existing.scalars().first() is not None:
                        continue

                    row = ModelPricing(
                        id=uuid.uuid4(),
                        provider=entry["provider"],
                        model=entry["model"],
                        input_price_per_1m=entry["input_price_per_1m"],
                        output_price_per_1m=entry["output_price_per_1m"],
                        cached_input_price_per_1m=entry.get("cached_input_price_per_1m", 0.0),
                    )
                    session.add(row)
                    inserted += 1

                if inserted:
                    await session.commit()
                    logger.info("デフォルト料金データを %d 件挿入しました", inserted)

            # キャッシュをリフレッシュ
            await self._load_pricing_cache()
        except Exception:
            logger.exception("デフォルト料金データの挿入に失敗しました")
        return inserted

    async def _load_pricing_cache(self) -> None:
        """DB から料金テーブルを読み込みインメモリキャッシュに保持する。"""
        try:
            async with await get_db_session() as session:
                result = await session.execute(select(ModelPricing))
                rows = result.scalars().all()
                self._pricing_cache = {
                    f"{r.provider}:{r.model}": r for r in rows
                }
                self._pricing_loaded = True
                logger.debug("料金キャッシュを更新: %d 件", len(self._pricing_cache))
        except Exception:
            logger.exception("料金キャッシュの読み込みに失敗しました")

    async def _get_pricing(self, provider: str, model: str) -> Optional[ModelPricing]:
        """指定モデルの料金を取得する。キャッシュ未読込なら先にロードする。"""
        if not self._pricing_loaded:
            await self._load_pricing_cache()

        key = f"{provider}:{model}"
        if key in self._pricing_cache:
            return self._pricing_cache[key]

        # 部分一致フォールバック（例: "gpt-4o-2024-08-06" → "gpt-4o"）
        for cache_key, pricing in self._pricing_cache.items():
            cached_model = cache_key.split(":", 1)[1]
            if model.startswith(cached_model) or cached_model.startswith(model):
                return pricing

        return None

    def _calculate_cost(
        self,
        pricing: Optional[ModelPricing],
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> tuple[float, float, float]:
        """トークン数からコスト(USD)を計算する。

        Returns:
            (input_cost, output_cost, total_cost)
        """
        if pricing is None:
            return 0.0, 0.0, 0.0

        regular_input = max(input_tokens - cached_tokens, 0)
        input_cost = (
            (regular_input / 1_000_000) * pricing.input_price_per_1m
            + (cached_tokens / 1_000_000) * (pricing.cached_input_price_per_1m or 0.0)
        )
        output_cost = (output_tokens / 1_000_000) * pricing.output_price_per_1m
        total_cost = input_cost + output_cost
        return round(input_cost, 8), round(output_cost, 8), round(total_cost, 8)

    # ──────── 記録 ────────

    async def record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cached_tokens: int = 0,
        session_id: Optional[uuid.UUID] = None,
        user_id: Optional[str] = None,
        project_id: Optional[uuid.UUID] = None,
        agent_name: Optional[str] = None,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """API呼び出し1回分のトークン使用量を記録する。

        Returns:
            記録した行の辞書表現。失敗時は None。
        """
        try:
            pricing = await self._get_pricing(provider, model)
            input_cost, output_cost, total_cost = self._calculate_cost(
                pricing, input_tokens, output_tokens, cached_tokens
            )

            row = TokenUsage(
                id=uuid.uuid4(),
                session_id=session_id,
                user_id=user_id,
                project_id=project_id,
                provider=provider,
                model=model,
                agent_name=agent_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cached_tokens=cached_tokens,
                input_cost=input_cost,
                output_cost=output_cost,
                total_cost=total_cost,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )

            async with await get_db_session() as session:
                session.add(row)
                await session.commit()

            logger.debug(
                "トークン使用量を記録: %s/%s  in=%d out=%d cost=$%.6f",
                provider, model, input_tokens, output_tokens, total_cost,
            )
            return row.to_dict()

        except Exception:
            logger.exception("トークン使用量の記録に失敗しました")
            return None

    # ──────── 集計クエリ ────────

    async def get_daily_summary(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
    ) -> List[Dict[str, Any]]:
        """日別のトークン・コスト集計を返す。

        Returns:
            [{"date": "2025-05-01", "total_input": ..., "total_output": ...,
              "total_cost": ..., "request_count": ...}, ...]
        """
        start = _to_datetime(start_date)
        end = _to_datetime(end_date)

        try:
            async with await get_db_session() as session:
                date_trunc = func.date_trunc("day", TokenUsage.created_at)
                stmt = (
                    select(
                        date_trunc.label("day"),
                        func.sum(TokenUsage.input_tokens).label("total_input"),
                        func.sum(TokenUsage.output_tokens).label("total_output"),
                        func.sum(TokenUsage.total_tokens).label("total_tokens"),
                        func.sum(TokenUsage.total_cost).label("total_cost"),
                        func.count(TokenUsage.id).label("request_count"),
                    )
                    .where(and_(*_date_filter(TokenUsage.created_at, start, end)))
                    .group_by(date_trunc)
                    .order_by(date_trunc)
                )
                result = await session.execute(stmt)
                rows = result.all()

            return [
                {
                    "date": r.day.strftime("%Y-%m-%d") if r.day else None,
                    "total_input": r.total_input or 0,
                    "total_output": r.total_output or 0,
                    "total_tokens": r.total_tokens or 0,
                    "total_cost": round(float(r.total_cost or 0), 6),
                    "request_count": r.request_count or 0,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("日別サマリーの取得に失敗しました")
            return []

    async def get_summary_by_model(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
    ) -> List[Dict[str, Any]]:
        """モデル別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_datetime(end_date)

        try:
            async with await get_db_session() as session:
                stmt = (
                    select(
                        TokenUsage.provider,
                        TokenUsage.model,
                        func.sum(TokenUsage.input_tokens).label("total_input"),
                        func.sum(TokenUsage.output_tokens).label("total_output"),
                        func.sum(TokenUsage.total_tokens).label("total_tokens"),
                        func.sum(TokenUsage.total_cost).label("total_cost"),
                        func.count(TokenUsage.id).label("request_count"),
                        func.avg(TokenUsage.latency_ms).label("avg_latency_ms"),
                    )
                    .where(and_(*_date_filter(TokenUsage.created_at, start, end)))
                    .group_by(TokenUsage.provider, TokenUsage.model)
                    .order_by(func.sum(TokenUsage.total_cost).desc())
                )
                result = await session.execute(stmt)
                rows = result.all()

            return [
                {
                    "provider": r.provider,
                    "model": r.model,
                    "total_input": r.total_input or 0,
                    "total_output": r.total_output or 0,
                    "total_tokens": r.total_tokens or 0,
                    "total_cost": round(float(r.total_cost or 0), 6),
                    "request_count": r.request_count or 0,
                    "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
                }
                for r in rows
            ]
        except Exception:
            logger.exception("モデル別サマリーの取得に失敗しました")
            return []

    async def get_summary_by_project(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
    ) -> List[Dict[str, Any]]:
        """プロジェクト別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_datetime(end_date)

        try:
            async with await get_db_session() as session:
                stmt = (
                    select(
                        TokenUsage.project_id,
                        func.sum(TokenUsage.input_tokens).label("total_input"),
                        func.sum(TokenUsage.output_tokens).label("total_output"),
                        func.sum(TokenUsage.total_tokens).label("total_tokens"),
                        func.sum(TokenUsage.total_cost).label("total_cost"),
                        func.count(TokenUsage.id).label("request_count"),
                    )
                    .where(and_(*_date_filter(TokenUsage.created_at, start, end)))
                    .group_by(TokenUsage.project_id)
                    .order_by(func.sum(TokenUsage.total_cost).desc())
                )
                result = await session.execute(stmt)
                rows = result.all()

            return [
                {
                    "project_id": str(r.project_id) if r.project_id else None,
                    "total_input": r.total_input or 0,
                    "total_output": r.total_output or 0,
                    "total_tokens": r.total_tokens or 0,
                    "total_cost": round(float(r.total_cost or 0), 6),
                    "request_count": r.request_count or 0,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("プロジェクト別サマリーの取得に失敗しました")
            return []

    async def get_summary_by_agent(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
    ) -> List[Dict[str, Any]]:
        """エージェント別のトークン・コスト集計を返す。"""
        start = _to_datetime(start_date)
        end = _to_datetime(end_date)

        try:
            async with await get_db_session() as session:
                stmt = (
                    select(
                        TokenUsage.agent_name,
                        func.sum(TokenUsage.input_tokens).label("total_input"),
                        func.sum(TokenUsage.output_tokens).label("total_output"),
                        func.sum(TokenUsage.total_tokens).label("total_tokens"),
                        func.sum(TokenUsage.total_cost).label("total_cost"),
                        func.count(TokenUsage.id).label("request_count"),
                    )
                    .where(
                        and_(
                            TokenUsage.agent_name.isnot(None),
                            *_date_filter(TokenUsage.created_at, start, end),
                        )
                    )
                    .group_by(TokenUsage.agent_name)
                    .order_by(func.sum(TokenUsage.total_cost).desc())
                )
                result = await session.execute(stmt)
                rows = result.all()

            return [
                {
                    "agent_name": r.agent_name,
                    "total_input": r.total_input or 0,
                    "total_output": r.total_output or 0,
                    "total_tokens": r.total_tokens or 0,
                    "total_cost": round(float(r.total_cost or 0), 6),
                    "request_count": r.request_count or 0,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("エージェント別サマリーの取得に失敗しました")
            return []

    async def get_total_cost(
        self,
        start_date: Union[date, datetime, str, None] = None,
        end_date: Union[date, datetime, str, None] = None,
    ) -> Dict[str, Any]:
        """指定期間の合計コスト・トークン数を返す。"""
        start = _to_datetime(start_date)
        end = _to_datetime(end_date)

        try:
            async with await get_db_session() as session:
                stmt = select(
                    func.sum(TokenUsage.input_tokens).label("total_input"),
                    func.sum(TokenUsage.output_tokens).label("total_output"),
                    func.sum(TokenUsage.total_tokens).label("total_tokens"),
                    func.sum(TokenUsage.total_cost).label("total_cost"),
                    func.count(TokenUsage.id).label("request_count"),
                ).where(and_(*_date_filter(TokenUsage.created_at, start, end)))

                result = await session.execute(stmt)
                row = result.one()

            return {
                "total_input": row.total_input or 0,
                "total_output": row.total_output or 0,
                "total_tokens": row.total_tokens or 0,
                "total_cost": round(float(row.total_cost or 0), 6),
                "request_count": row.request_count or 0,
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
            }
        except Exception:
            logger.exception("合計コストの取得に失敗しました")
            return {
                "total_input": 0,
                "total_output": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "request_count": 0,
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
            }

    # ──────── ダッシュボード向けサマリー ────────

    async def get_dashboard_summary(self) -> Dict[str, Any]:
        """ダッシュボード表示用のサマリーを一括で返す。

        - 今日のコスト
        - 過去7日の日別推移
        - モデル別内訳（過去30日）
        """
        now = datetime.utcnow()
        today_start = datetime(now.year, now.month, now.day)
        week_ago = today_start - timedelta(days=7)
        month_ago = today_start - timedelta(days=30)
        tomorrow = today_start + timedelta(days=1)

        today_cost = await self.get_total_cost(today_start, tomorrow)
        weekly_trend = await self.get_daily_summary(week_ago, tomorrow)
        model_breakdown = await self.get_summary_by_model(month_ago, tomorrow)

        return {
            "today": today_cost,
            "daily_trend": weekly_trend,
            "weekly_trend": weekly_trend,
            "model_breakdown": model_breakdown,
            "generated_at": now.isoformat(),
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

        @track_tokens(provider="openai", model="gpt-4o")
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

            if isinstance(result, dict):
                usage = result.get("usage", result)
                input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                cached_tokens = usage.get("cached_tokens", 0)
            elif hasattr(result, "usage") and result.usage is not None:
                usage = result.usage
                input_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
                output_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)
                cached_tokens = getattr(usage, "cached_tokens", 0) or 0

            if input_tokens or output_tokens:
                service = get_token_tracking_service()
                await service.record_usage(
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
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
