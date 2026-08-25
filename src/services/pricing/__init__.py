"""料金計算基盤パッケージ。

金額は必ず `Decimal` で扱い、float を一切経由しない。
モデル解決は canonical_model / alias の完全一致のみで、prefix 部分一致は行わない。
未知モデルは `$0` の `priced` にせず `unknown` として明示する。

注意: このモジュールは import 時に DB へ触らない。
DB を使う `sync_catalog_to_db` / `refresh_openrouter_catalog` /
`backfill_token_usage_costs` / `get_pricing_catalog_status` は
それぞれのサブモジュールから直接 import して使う。
"""

from __future__ import annotations

from .catalog import (
    CatalogValidationError,
    PricingRuleSnapshot,
    diff_catalog,
    load_catalog_file,
    validate_catalog,
)
from .engine import (
    CostBreakdown,
    PricingEngine,
    PricingStatus,
    UsageInput,
    get_pricing_engine,
)
from .free_tier import (
    FREE_TIER_LIMITS,
    AllocatableRecord,
    FreeTierAllocation,
    FreeTierConfig,
    allocate_free_tier,
    free_tier_usage,
)
from .providers import (
    CLI_PROVIDERS,
    LOCAL_PROVIDERS,
    PROVIDER_REPORTED_PROVIDERS,
    canonical_provider,
    is_cli_provider,
    is_local_provider,
)

__all__ = [
    # providers
    "canonical_provider",
    "is_cli_provider",
    "is_local_provider",
    "CLI_PROVIDERS",
    "LOCAL_PROVIDERS",
    "PROVIDER_REPORTED_PROVIDERS",
    # engine
    "PricingEngine",
    "PricingStatus",
    "UsageInput",
    "CostBreakdown",
    "get_pricing_engine",
    # free tier
    "FreeTierConfig",
    "FreeTierAllocation",
    "AllocatableRecord",
    "FREE_TIER_LIMITS",
    "allocate_free_tier",
    "free_tier_usage",
    # catalog
    "PricingRuleSnapshot",
    "load_catalog_file",
    "validate_catalog",
    "diff_catalog",
    "CatalogValidationError",
]
