"""料金計算エンジン。

`calculate()` は同期・純粋関数で、事前にロード済みのメモリキャッシュだけを見る。
金額は必ず `Decimal`。float は一切経由しない。
モデル解決は canonical_model / alias の**完全一致のみ**で、prefix 部分一致は行わない。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .catalog import (
    CatalogDocument,
    PricingRuleSnapshot,
    load_catalog_file,
)
from .providers import canonical_provider, is_cli_provider, is_local_provider

logger = logging.getLogger(__name__)

__all__ = [
    "PricingStatus",
    "UsageInput",
    "CostBreakdown",
    "PricingEngine",
    "get_pricing_engine",
    "MONEY_QUANT",
    "PER_MILLION",
]

MONEY_QUANT = Decimal("0.0000000001")  # 小数10桁
PER_MILLION = Decimal("1000000")
ZERO = Decimal("0")


class PricingStatus:
    """料金判定ステータス。値はこの文字列そのもの。"""

    PRICED = "priced"
    PROVIDER_REPORTED = "provider_reported"
    FREE_INCENTIVE = "free_incentive"
    SUBSCRIPTION = "subscription"
    LOCAL = "local"
    UNKNOWN = "unknown"

    ALL = (
        "priced",
        "provider_reported",
        "free_incentive",
        "subscription",
        "local",
        "unknown",
    )


@dataclass(frozen=True)
class UsageInput:
    """1リクエスト分の使用量。"""

    provider: str
    requested_model: str
    resolved_model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    tool_invocations: Optional[Mapping[str, int]] = None
    provider_reported_cost: Optional[Decimal] = None
    provider_reported_cost_details: Optional[Mapping[str, Any]] = None
    occurred_at: Optional[datetime] = None


@dataclass(frozen=True)
class CostBreakdown:
    """料金計算結果。"""

    pricing_status: str
    provider: str
    canonical_model: Optional[str]
    rule_id: Optional[str]
    catalog_version: Optional[str]
    applied_input_rate: Optional[Decimal]
    applied_cached_input_rate: Optional[Decimal]
    applied_cache_write_rate: Optional[Decimal]
    applied_output_rate: Optional[Decimal]
    list_input_cost: Decimal
    list_output_cost: Decimal
    list_tool_cost: Decimal
    list_total_cost: Decimal
    provider_reported_cost: Optional[Decimal]
    provider_reported_cost_details: Optional[Dict[str, Any]]
    free_incentive_group: Optional[str]
    is_unknown: bool


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 外部（プロバイダ応答）由来の float は文字列経由で Decimal 化する
        return Decimal(repr(value))
    return Decimal(str(value))


def _normalize_model(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    text = str(name).strip().lower()
    return text or None


class PricingEngine:
    """料金ルールのメモリキャッシュと計算ロジック。"""

    def __init__(
        self,
        rules: Sequence[PricingRuleSnapshot] = (),
        *,
        catalog_version: Optional[str] = None,
        free_incentive_groups: Optional[Mapping[str, Sequence[str]]] = None,
        catalog_path: "str | Path | None" = None,
    ) -> None:
        self._catalog_path = catalog_path
        self._lock = threading.RLock()
        self._loaded = False
        self._set_rules(rules, catalog_version, free_incentive_groups)

    # ──────── 構築 ────────

    @classmethod
    def from_catalog_file(cls, path: "str | Path | None" = None) -> "PricingEngine":
        """DB 不要でカタログ JSON だけからエンジンを作る。"""
        document = load_catalog_file(path)
        engine = cls(catalog_path=path)
        engine._apply_document(document)
        return engine

    def _apply_document(self, document: CatalogDocument) -> None:
        self._set_rules(
            document.rules,
            document.catalog_version,
            document.free_incentive_groups,
        )
        self._loaded = True

    def _set_rules(
        self,
        rules: Sequence[PricingRuleSnapshot],
        catalog_version: Optional[str],
        free_incentive_groups: Optional[Mapping[str, Sequence[str]]],
    ) -> None:
        with self._lock:
            self._rules: Tuple[PricingRuleSnapshot, ...] = tuple(rules)
            self._catalog_version = catalog_version
            self._free_groups: Dict[str, frozenset] = {
                str(name): frozenset(
                    _normalize_model(m) for m in members if _normalize_model(m)
                )
                for name, members in (free_incentive_groups or {}).items()
            }
            index: Dict[Tuple[str, str], List[PricingRuleSnapshot]] = {}
            for rule in self._rules:
                names = {rule.canonical_model, *rule.aliases}
                for name in names:
                    normalized = _normalize_model(name)
                    if not normalized:
                        continue
                    index.setdefault((rule.provider, normalized), []).append(rule)
            for bucket in index.values():
                bucket.sort(key=lambda r: r.effective_from, reverse=True)
            self._index = index

    # ──────── ロード ────────

    async def ensure_loaded(self, force: bool = False) -> None:
        """DB の `pricing_rules` をメモリキャッシュへ読み込む。

        DB が使えない場合はカタログファイルへフォールバックし、例外を外へ出さない。
        """
        if self._loaded and not force:
            return
        try:
            from ...memory.database import get_db_session

            from .catalog import _load_current_snapshots  # noqa: WPS437

            async with await get_db_session() as session:
                snapshots = await _load_current_snapshots(session)
            if snapshots:
                versions = {s.catalog_version for s in snapshots if s.catalog_version}
                catalog_version = max(versions) if versions else None
                groups = self._load_groups_from_file()
                self._set_rules(snapshots, catalog_version, groups)
                self._loaded = True
                return
            logger.info("pricing_rules が空のため料金カタログファイルを使用します")
        except Exception:
            logger.warning(
                "DB からの料金ルール読み込みに失敗したためカタログファイルへフォールバックします",
                exc_info=True,
            )
        try:
            document = load_catalog_file(self._catalog_path)
            self._apply_document(document)
        except Exception:
            logger.exception("料金カタログファイルの読み込みにも失敗しました")

    def _load_groups_from_file(self) -> Dict[str, Sequence[str]]:
        try:
            return dict(load_catalog_file(self._catalog_path).free_incentive_groups)
        except Exception:
            logger.warning("free_incentive_groups の読み込みに失敗しました", exc_info=True)
            return dict(self._free_groups)

    def invalidate(self) -> None:
        """キャッシュを破棄する。次回 ensure_loaded で再読込。"""
        with self._lock:
            self._loaded = False

    # ──────── 参照 ────────

    @property
    def catalog_version(self) -> Optional[str]:
        return self._catalog_version

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @property
    def free_incentive_groups(self) -> Dict[str, frozenset]:
        return dict(self._free_groups)

    # ──────── 解決 ────────

    def resolve(
        self,
        provider: str,
        requested_model: str,
        resolved_model: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> Optional[PricingRuleSnapshot]:
        """厳密な alias 解決のみ。prefix 部分一致は行わない。"""
        canonical = canonical_provider(provider)
        moment = at or datetime.utcnow()
        # レスポンス由来の実モデル名を優先する
        for name in (resolved_model, requested_model):
            normalized = _normalize_model(name)
            if not normalized:
                continue
            for rule in self._index.get((canonical, normalized), ()):
                if rule.effective_from > moment:
                    continue
                if rule.effective_to is not None and rule.effective_to <= moment:
                    continue
                return rule
        return None

    def _free_group_for(
        self, provider: str, resolved_model: Optional[str], canonical_model: Optional[str]
    ) -> Optional[str]:
        if provider != "openai":
            return None
        for name in (_normalize_model(resolved_model), _normalize_model(canonical_model)):
            if not name:
                continue
            for group, members in self._free_groups.items():
                if name in members:
                    return group
        return None

    # ──────── 計算 ────────

    def calculate(self, usage: UsageInput) -> CostBreakdown:
        """同期・純粋関数。DB には触れない。"""
        provider = canonical_provider(usage.provider)
        reported_cost = _to_decimal(usage.provider_reported_cost)
        reported_details = (
            dict(usage.provider_reported_cost_details)
            if usage.provider_reported_cost_details
            else None
        )

        # 1. CLI（サブスクリプション）
        if is_cli_provider(provider):
            return self._flat_status(
                PricingStatus.SUBSCRIPTION, provider, reported_cost, reported_details
            )

        # 2. ローカル実行
        if is_local_provider(provider):
            return self._flat_status(
                PricingStatus.LOCAL, provider, reported_cost, reported_details
            )

        moment = usage.occurred_at or datetime.utcnow()
        rule = self.resolve(
            provider, usage.requested_model, usage.resolved_model, at=moment
        )

        # 3. プロバイダ申告コスト優先
        if reported_cost is not None:
            computed = (
                self._compute_priced(usage, rule, provider)
                if rule is not None and rule.pricing_kind in ("flat_token", "tiered_token")
                else None
            )
            if computed is None:
                return CostBreakdown(
                    pricing_status=PricingStatus.PROVIDER_REPORTED,
                    provider=provider,
                    canonical_model=(rule.canonical_model if rule else None),
                    rule_id=(rule.rule_id if rule else None),
                    catalog_version=(rule.catalog_version if rule else self._catalog_version),
                    applied_input_rate=None,
                    applied_cached_input_rate=None,
                    applied_cache_write_rate=None,
                    applied_output_rate=None,
                    list_input_cost=ZERO,
                    list_output_cost=ZERO,
                    list_tool_cost=ZERO,
                    list_total_cost=ZERO,
                    provider_reported_cost=reported_cost,
                    provider_reported_cost_details=reported_details,
                    free_incentive_group=None,
                    is_unknown=False,
                )
            return CostBreakdown(
                **{
                    **computed,
                    "pricing_status": PricingStatus.PROVIDER_REPORTED,
                    "provider_reported_cost": reported_cost,
                    "provider_reported_cost_details": reported_details,
                    "is_unknown": False,
                }
            )

        # 4. ルール解決成功
        if rule is not None:
            if rule.pricing_kind == "subscription":
                return self._flat_status(
                    PricingStatus.SUBSCRIPTION, provider, None, None, rule=rule
                )
            if rule.pricing_kind == "local":
                return self._flat_status(
                    PricingStatus.LOCAL, provider, None, None, rule=rule
                )
            computed = self._compute_priced(usage, rule, provider)
            if computed is not None:
                return CostBreakdown(
                    **{
                        **computed,
                        "pricing_status": PricingStatus.PRICED,
                        "provider_reported_cost": None,
                        "provider_reported_cost_details": None,
                        "is_unknown": False,
                    }
                )

        # 5. 未知モデル（$0 の priced にしてはならない）
        return CostBreakdown(
            pricing_status=PricingStatus.UNKNOWN,
            provider=provider,
            canonical_model=None,
            rule_id=None,
            catalog_version=None,
            applied_input_rate=None,
            applied_cached_input_rate=None,
            applied_cache_write_rate=None,
            applied_output_rate=None,
            list_input_cost=ZERO,
            list_output_cost=ZERO,
            list_tool_cost=ZERO,
            list_total_cost=ZERO,
            provider_reported_cost=None,
            provider_reported_cost_details=reported_details,
            free_incentive_group=None,
            is_unknown=True,
        )

    def _flat_status(
        self,
        status: str,
        provider: str,
        reported_cost: Optional[Decimal],
        reported_details: Optional[Dict[str, Any]],
        rule: Optional[PricingRuleSnapshot] = None,
    ) -> CostBreakdown:
        return CostBreakdown(
            pricing_status=status,
            provider=provider,
            canonical_model=(rule.canonical_model if rule else None),
            rule_id=(rule.rule_id if rule else None),
            catalog_version=(rule.catalog_version if rule else None),
            applied_input_rate=None,
            applied_cached_input_rate=None,
            applied_cache_write_rate=None,
            applied_output_rate=None,
            list_input_cost=ZERO,
            list_output_cost=ZERO,
            list_tool_cost=ZERO,
            list_total_cost=ZERO,
            provider_reported_cost=reported_cost,
            provider_reported_cost_details=reported_details,
            free_incentive_group=None,
            is_unknown=False,
        )

    def _select_rates(
        self, rule: PricingRuleSnapshot, input_tokens: int
    ) -> Optional[Dict[str, Decimal]]:
        """段階料金を含めた基本単価セットを返す（リクエスト全体に適用する単価）。"""
        if rule.pricing_kind == "tiered_token":
            for tier in rule.tiers:
                max_tokens = tier.get("max_input_tokens")
                if max_tokens is None or input_tokens <= int(max_tokens):
                    return dict(tier.get("rates") or {})
            return None
        if rule.pricing_kind == "flat_token":
            return dict(rule.rates)
        return None

    def _compute_priced(
        self, usage: UsageInput, rule: PricingRuleSnapshot, provider: str
    ) -> Optional[Dict[str, Any]]:
        base = self._select_rates(rule, max(int(usage.input_tokens or 0), 0))
        if not base or "input" not in base or "output" not in base:
            return None

        in_rate = base["input"]
        # cached_input / cache_write の単価が無い場合は入力単価と同額扱い
        cached_rate = base.get("cached_input", in_rate)
        cw_rate = base.get("cache_write", in_rate)
        out_rate = base["output"]

        input_tokens = max(int(usage.input_tokens or 0), 0)
        output_tokens = max(int(usage.output_tokens or 0), 0)
        cached_tokens = max(int(usage.cached_tokens or 0), 0)
        cache_write_tokens = max(int(usage.cache_write_tokens or 0), 0)

        # 長文倍率: 閾値超過時は**リクエスト全体**へ適用（超過分だけではない）
        long_context = rule.long_context
        if long_context and input_tokens > int(long_context["threshold_tokens"]):
            in_multiplier = long_context["input_multiplier"]
            out_multiplier = long_context["output_multiplier"]
            in_rate = in_rate * in_multiplier
            cached_rate = cached_rate * in_multiplier
            cw_rate = cw_rate * in_multiplier
            out_rate = out_rate * out_multiplier

        uncached_input = max(input_tokens - cached_tokens - cache_write_tokens, 0)

        list_input_cost = _quantize(
            (
                Decimal(uncached_input) * in_rate
                + Decimal(cached_tokens) * cached_rate
                + Decimal(cache_write_tokens) * cw_rate
            )
            / PER_MILLION
        )
        list_output_cost = _quantize(Decimal(output_tokens) * out_rate / PER_MILLION)

        tool_cost = ZERO
        for name, count in (usage.tool_invocations or {}).items():
            rate = rule.tool_rates.get(str(name))
            if rate is None:
                continue
            tool_cost += Decimal(int(count)) * rate
        list_tool_cost = _quantize(tool_cost)

        list_total_cost = _quantize(list_input_cost + list_output_cost + list_tool_cost)

        return {
            "provider": provider,
            "canonical_model": rule.canonical_model,
            "rule_id": rule.rule_id,
            "catalog_version": rule.catalog_version,
            "applied_input_rate": in_rate,
            "applied_cached_input_rate": cached_rate,
            "applied_cache_write_rate": cw_rate,
            "applied_output_rate": out_rate,
            "list_input_cost": list_input_cost,
            "list_output_cost": list_output_cost,
            "list_tool_cost": list_tool_cost,
            "list_total_cost": list_total_cost,
            "free_incentive_group": self._free_group_for(
                provider, usage.resolved_model, rule.canonical_model
            ),
        }


# ────────────────────────────────────────────
# シングルトン
# ────────────────────────────────────────────

_ENGINE: Optional[PricingEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_pricing_engine() -> PricingEngine:
    """プロセス共有シングルトン。"""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = PricingEngine()
    return _ENGINE
