"""無料Teamの設定、候補選択、原子的クォータ予約を管理する。"""

from __future__ import annotations

import asyncio
import logging
import os
import hashlib
import inspect
import json
import re
import threading
import uuid
import weakref
from fnmatch import fnmatchcase
from dataclasses import dataclass, field
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Awaitable, Callable, Iterable, Mapping

from sqlalchemy import String, and_, cast, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_db_session
from ..memory.models.free_team import (
    FreeTeamCandidateModel,
    FreeTeamCredentialProfile,
    FreeTeamQuotaPool,
    FreeTeamReservation,
    OPENAI_DAILY_QUOTA_GROUPS,
    OPENAI_SAFETY_TOKENS,
)
from .runtime_policy_utils import config_get
from .free_team_defaults import (
    OPENROUTER_DEFAULT_FREE_DAILY_TIER,
    OPENROUTER_FREE_DAILY_TIERS,
    OPENROUTER_SAFETY_MARGIN_REQUESTS,
    free_team_profile_template,
)
from .decision_receipt_service import DecisionReceipt, finalize_decision_receipt
from .pricing.providers import canonical_provider
from .openai_provider_circuit import (
    OPENAI_QUOTA_429_CODES,
    admit_openai_provider_call,
    classify_openai_quota_429,
    openai_provider_is_blocked,
    openai_provider_probe_available,
    provider_circuit_status,
    quota_429_evidence_from_fields,
    trip_openai_provider_circuit,
)

logger = logging.getLogger(__name__)
OPENAI_USAGE_STALE_AFTER_SECONDS = 15 * 60
_OPENAI_USAGE_REFRESH_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_OPENAI_USAGE_REFRESH_LOCKS_GUARD = threading.Lock()
_OPENAI_USAGE_HEALTH_LOCK = threading.Lock()
_OPENAI_USAGE_HEALTH_CACHE: dict[str, Any] | None = None


ROUTING_PROFILE_PROVIDER = "routing-profile"
FREE_TEAM_PROFILE_ID = "free-team"
FREE_TEAM_MODEL_ID = "free-team"
OPENROUTER_CREDENTIAL_SOURCES = frozenset({"dedicated", "existing_openrouter"})
ALLOWED_BILLING_MODES = {
    "complimentary",
    "free_tier",
    "promo_credit",
    "subscription_cli",
}
OPENAI_BILLING_MODES = {"paid", "complimentary"}
RETRYABLE_ERROR_CLASSES = {"429", "402", "5xx", "timeout", "connection"}
CLI_PROVIDERS = {"codex-cli", "antigravity-cli", "grok-cli", "claude-cli"}
# Windowed quotas use bounded holds and grace, except Gemini promo USD holds.
# Exact supported TokenUsage evidence is reconciled before a hold is capped or expired.
UNCERTAIN_HOLD_PER_RESERVATION_RATIO = Decimal("0.05")
UNCERTAIN_HOLD_POOL_RATIO = Decimal("0.20")
UNCERTAIN_WINDOW_GRACE_MINIMUM = timedelta(seconds=60)
OPENAI_INPUT_TOKEN_SAFETY_FACTOR = Decimal("1.5")
OPENAI_INPUT_TOKEN_BYTES_FLOOR_DIVISOR = Decimal("3")


class FreeTeamUnavailableError(RuntimeError):
    """安全に予約できる無料候補がない。"""


class ComplimentaryMediaUnboundedError(FreeTeamUnavailableError):
    """The current request cannot be bounded before sending media."""

    code = "complimentary_media_unbounded"
    stage = "request_budget_estimate"

    def __init__(self) -> None:
        super().__init__(self.code)


class ComplimentaryImageBudgetInsufficient(FreeTeamUnavailableError):
    """The verified image request cannot fit the existing free quota."""

    code = "complimentary_image_budget_insufficient"
    stage = "quota_reservation"

    def __init__(self) -> None:
        super().__init__(self.code)


class ComplimentaryImageUsageUnknown(FreeTeamUnavailableError):
    """The provider responded but the image usage cannot be safely settled."""

    code = "complimentary_image_usage_unknown"
    stage = "provider_usage_settlement"

    def __init__(self) -> None:
        super().__init__(self.code)


class ComplimentaryImageCostExceeded(FreeTeamUnavailableError):
    """The final verified wire request cannot fit its existing cost ceiling."""

    code = "complimentary_image_cost_budget_exceeded"
    stage = "request_budget_estimate"

    def __init__(self) -> None:
        super().__init__(self.code)


class FreeTeamReservationReconciliationError(RuntimeError):
    """An uncertain reservation could not be settled from its usage evidence."""

    def __init__(self, code: str, http_status: int = 409) -> None:
        self.code = str(code or "free_team_reconciliation_conflict")
        self.http_status = int(http_status)
        super().__init__(self.code)


class FreeTeamQuotaExhausted(FreeTeamUnavailableError):
    """All otherwise eligible candidates are waiting for a known quota reset.

    ``reset_at`` uses the service's existing UTC-naive quota-window convention.
    Callers can wait before creating a provider attempt; generic unavailable
    conditions intentionally remain ``FreeTeamUnavailableError``.
    """

    def __init__(
        self,
        *,
        retry_after_seconds: float,
        reset_at: datetime,
        reset_kind: str,
        quota_ids: Iterable[str],
    ) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.reset_at = reset_at
        self.reset_kind = reset_kind
        self.quota_ids = tuple(sorted({str(value) for value in quota_ids}))
        super().__init__("free_team_quota_exhausted")


class FreeTeamCooldownWait(FreeTeamUnavailableError):
    """All currently eligible candidates are inside their retry cooldown."""

    def __init__(
        self,
        *,
        retry_after_seconds: float,
        cooldown_until: datetime,
        candidate_ids: Iterable[str],
    ) -> None:
        # Work-item retries stay minute-scale. Longer configured cooldowns are
        # rechecked after one minute and can be deferred again if still active.
        self.retry_after_seconds = min(60.0, max(0.0, float(retry_after_seconds)))
        self.cooldown_until = cooldown_until
        self.candidate_ids = tuple(sorted({str(value) for value in candidate_ids}))
        super().__init__("free_team_candidate_cooldown")


class FreeTeamCredentialBlocked(FreeTeamUnavailableError):
    """The selected free-only credential cannot be observed or used safely."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "free_team_credential_blocked")
        super().__init__(self.code)


class FreeTeamObserverRefreshWait(FreeTeamUnavailableError):
    """A transient OpenRouter /key observation failure before provider use."""

    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: float = 60.0,
    ) -> None:
        self.code = str(code or "openrouter_observer_refresh_wait")
        self.retry_after_seconds = max(
            1.0, min(float(retry_after_seconds or 60.0), 300.0)
        )
        super().__init__(self.code)


def _safe_openrouter_observer_failure(value: Any) -> str:
    """Project an observer failure into a bounded, non-secret reason token."""

    failure = str(value or "").strip().casefold()
    fixed = {
        "timeout",
        "network_error",
        "http_408",
        "http_429",
        "credential_changed",
        "sync_failed",
        "missing_key",
        "unsupported_endpoint",
        "invalid_response",
        "redirect_rejected",
    }
    if failure in fixed:
        return failure
    if re.fullmatch(r"http_5[0-9]{2}", failure):
        return failure
    if failure.startswith("http_"):
        return "provider_rejected"
    return "refresh_failed"


@dataclass(frozen=True)
class RouteIntent:
    """同期設定解決の結果。DBアクセスは含めない。"""

    kind: str
    provider: str = ""
    model: str = ""
    effort: str = ""
    runner: str = ""
    routing_profile_id: str = ""
    pool_id: str = ""
    team_id: str = ""
    subagent_id: str = ""
    llm_profile_id: str = ""
    effort_policy: str = ""
    tool_mode: str = ""
    candidate_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if value not in (None, "")
        }


@dataclass(frozen=True)
class FreeTeamApiTaskPreflight:
    """Read-only proof that one strict Free Team task can be admitted.

    ``preflight_route_quota`` intentionally reports only wait/error states and
    returns ``None`` for several kinds of unavailable candidate.  A scheduler
    needs a positive result before it creates an AgentTask, so this small
    projection makes that proof explicit without acquiring a lease or writing
    a reservation.
    """

    eligible: bool
    reason: str
    candidate_id: str = ""
    provider: str = ""
    model: str = ""
    quota_ids: tuple[str, ...] = ()
    available_quota: dict[str, str] = field(default_factory=dict)


@dataclass
class RouteLease:
    """1ターンまたは1委譲に固定される具体的な実行対象。"""

    reservation_id: uuid.UUID
    provider: str
    model: str
    credential_profile_id: str
    candidate_id: str
    quota_pool_ids: tuple[str, ...]
    effort: str
    routing_profile_id: str
    pool_id: str
    max_output_tokens: int
    timeout_seconds: int
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    cli_auth_reference: str = field(default="", repr=False)
    provider_options: dict[str, Any] = field(default_factory=dict, repr=False)
    fallback_count: int = 0
    team_id: str = ""
    subagent_id: str = ""
    llm_profile_id: str = ""
    decision_receipt: DecisionReceipt | None = field(default=None, repr=False)
    billing_scope_id: str = ""
    billing_mode: str = "paid"

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "routing_profile": self.routing_profile_id,
            "pool": self.pool_id,
            "team_id": self.team_id or None,
            "subagent_id": self.subagent_id or None,
            "llm_profile_id": self.llm_profile_id or None,
            "credential_profile": (
                "openai_api_key"
                if self.provider == "openai"
                and self.billing_mode == "complimentary"
                else self.credential_profile_id
            ),
            "candidate": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "billing_mode": self.billing_mode,
            "effort": self.effort or None,
            "quota_pool_ids": list(self.quota_pool_ids),
            "reservation_id": str(self.reservation_id),
            "fallback_count": self.fallback_count,
        }


def free_team_selected(config: Any) -> bool:
    selected = (
        str(config_get(config, "llm_provider", "") or "").strip().lower()
        == ROUTING_PROFILE_PROVIDER
        and str(config_get(config, "llm_model", "") or "").strip().lower()
        == FREE_TEAM_MODEL_ID
    )
    return selected and bool(free_team_profile(config).get("enabled", True))


def openai_billing_mode(config: Any, model: str | None = None) -> str:
    """Resolve the billing contract for an official OpenAI request.

    The ordinary OpenAI route is paid unless the configuration explicitly
    selects the complimentary incentive. Free Team remains complimentary
    because the ordinary OpenAI client is bound to the owner's confirmed
    organization entitlement and shared model-specific quota pools.

    A missing ``openai`` branch is treated as the legacy, unconfigured request
    boundary.  Requests for incentive models still fail through the existing
    complimentary verification checks instead of silently becoming paid.  A
    normal application config includes the ``openai`` branch from the
    canonical defaults, where the existing data-sharing flag is the explicit
    complimentary-vs-paid intent when no billing-mode override is supplied.
    """

    raw = config_get(config, "openai.billing_mode", None)
    if raw is None:
        if free_team_selected(config) or config_get(
            config, "openai.data_sharing_incentive_enabled", False
        ) is True:
            return "complimentary"
        if config_get(config, "openai", None) is None:
            model_text = str(model or "").strip().lower()
            if complimentary_model_group(model_text) is not None or model_text.startswith(
                ("gpt-5.6", "gpt-6")
            ):
                return "complimentary"
        return "paid"

    mode = str(raw).strip().lower()
    if mode not in OPENAI_BILLING_MODES:
        raise FreeTeamUnavailableError("openai_billing_mode_unsupported")
    # ``free-team`` uses the same OpenAI key, but its eligibility and quota
    # guards remain mandatory even when the global billing mode says paid.
    if mode == "paid" and free_team_selected(config):
        return "complimentary"
    return mode


def free_team_profile(config: Any) -> dict[str, Any]:
    raw = config_get(config, f"routing_profiles.{FREE_TEAM_PROFILE_ID}", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    profile = free_team_profile_template()
    profile.setdefault("openrouter_credential_source", "dedicated")
    for key, value in raw.items():
        if key == "pools" and isinstance(value, dict):
            merged_pools = dict(profile["pools"])
            for pool_id, stored_pool in value.items():
                if isinstance(stored_pool, dict):
                    merged_pools[str(pool_id)] = {
                        **dict(merged_pools.get(str(pool_id)) or {}),
                        **stored_pool,
                    }
            profile["pools"] = merged_pools
        elif key in profile:
            profile[key] = value
    return profile


def main_route_intent(config: Any) -> RouteIntent:
    if free_team_selected(config):
        profile = free_team_profile(config)
        pool_id = str(profile.get("main_pool_id") or "coordinator")
        pool = (profile.get("pools") or {}).get(pool_id, {}) or {}
        return RouteIntent(
            kind="pool",
            routing_profile_id=FREE_TEAM_PROFILE_ID,
            pool_id=pool_id,
            tool_mode=str(pool.get("tool_mode") or "auto"),
            candidate_ids=tuple(str(value) for value in (pool.get("candidate_ids") or [])),
        )
    return RouteIntent(
        kind="static",
        provider=str(config_get(config, "llm_provider", "openai") or "openai"),
        model=str(config_get(config, "llm_model", "") or ""),
    )


def pool_route_intent(
    config: Any,
    *,
    subagent_id: str = "",
    llm_profile_id: str = "",
    profile: Mapping[str, Any] | None = None,
    team_id: str = "",
    # These aliases are accepted only while callers migrate to the v3
    # subagent/profile vocabulary; they are never returned or persisted.
    member_key: str = "",
    group_id: str = "",
    group: Mapping[str, Any] | None = None,
) -> RouteIntent | None:
    selected_profile = profile
    if selected_profile is None and llm_profile_id:
        # P5 accepts a pool id directly or resolves a role's first pool route
        # from model_roles.  The retired routing_profiles.free-team.llm_profiles
        # map is never consulted.
        pool_id = str(llm_profile_id).strip()
        pools = free_team_profile(config).get("pools") or {}
        if pool_id in pools:
            selected_profile = {"kind": "pool", "pool_id": pool_id}
        else:
            roles = config_get(config, "model_roles.roles", {}) or {}
            role = roles.get(pool_id) if isinstance(roles, Mapping) else None
            routes = role.get("routes") if isinstance(role, Mapping) else None
            if isinstance(routes, list):
                selected_profile = next(
                    (
                        route
                        for route in routes
                        if isinstance(route, Mapping)
                        and str(route.get("kind") or "").strip().lower() == "pool"
                    ),
                    None,
                )
    # Legacy callers may still pass a group mapping at the migration boundary.
    if selected_profile is None and isinstance(group, Mapping):
        selected_profile = group
    selected_profile = selected_profile if isinstance(selected_profile, Mapping) else {}
    route_kind = str(selected_profile.get("kind") or "").strip().lower()
    target_type = str(selected_profile.get("target_type") or "").strip().lower()
    if route_kind != "pool" and target_type != "pool":
        return None
    pool_id = str(selected_profile.get("pool_id") or "").strip()
    if not pool_id:
        return None
    profile = free_team_profile(config)
    pool = (profile.get("pools") or {}).get(pool_id, {}) or {}
    resolved_subagent_id = str(subagent_id or member_key or "").strip()
    resolved_profile_id = str(
        llm_profile_id
        or selected_profile.get("profile_id")
        or group_id
        or ""
    ).strip()
    return RouteIntent(
        kind="pool",
        routing_profile_id=str(
            selected_profile.get("routing_profile_id") or FREE_TEAM_PROFILE_ID
        ),
        pool_id=pool_id,
        team_id=str(team_id or "").strip(),
        subagent_id=resolved_subagent_id,
        llm_profile_id=resolved_profile_id,
        effort_policy=str(selected_profile.get("effort_policy") or ""),
        effort=str(selected_profile.get("effort") or ""),
        tool_mode=str(pool.get("tool_mode") or "auto"),
        candidate_ids=tuple(str(value) for value in (pool.get("candidate_ids") or [])),
    )


def _utcnow() -> datetime:
    return datetime.utcnow()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _credential_env_name(profile_id: str) -> str | None:
    return {
        "gemini-free": "GEMINI_FREE_API_KEY",
        "gemini-promo": "GEMINI_PROMO_API_KEY",
        "openrouter-free": "OPENROUTER_FREE_API_KEY",
    }.get(profile_id)


def openrouter_credential_source(config: Any) -> str:
    """Return the selected OpenRouter Free key source, or an invalid marker.

    Stored values are validated by the settings API.  A malformed value from
    another configuration path must still fail closed instead of silently
    selecting either credential.
    """

    source = free_team_profile(config).get("openrouter_credential_source")
    if not isinstance(source, str):
        return ""
    return source if source in OPENROUTER_CREDENTIAL_SOURCES else ""


def _dedicated_credential_api_key(profile: FreeTeamCredentialProfile) -> str:
    value = str(profile.api_key or "").strip()
    if value:
        return value
    env_name = _credential_env_name(profile.id)
    return str(os.getenv(env_name, "") if env_name else "").strip()


def _openrouter_existing_source_allowed(profile: FreeTeamCredentialProfile) -> bool:
    from . import free_team_provider_observer as observer

    return bool(
        profile.id == "openrouter-free"
        and str(profile.provider or "").strip().lower() == "openrouter"
        and str(profile.billing_mode or "").strip().lower() == "free_tier"
        and profile.allow_paid_overage is False
        and observer.is_official_endpoint(profile.base_url)
    )


def _credential_api_key(
    profile: FreeTeamCredentialProfile,
    config: Any = None,
) -> str:
    # Keep the legacy row only as a database FK target. OpenAI routing always
    # consumes the one key configured on the normal OpenAI client.
    if profile.id == "openai-complimentary":
        return str(
            config_get(config, "openai_api_key", "")
            or os.getenv("OPENAI_API_KEY", "")
        ).strip()
    if profile.id != "openrouter-free" or config is None:
        return _dedicated_credential_api_key(profile)
    source = openrouter_credential_source(config)
    if source == "dedicated":
        return _dedicated_credential_api_key(profile)
    if source != "existing_openrouter" or not _openrouter_existing_source_allowed(profile):
        return ""
    return str(
        config_get(config, "openrouter_api_key", "")
        or os.getenv("OPENROUTER_API_KEY", "")
    ).strip()


def _credential_configured(
    profile: FreeTeamCredentialProfile,
    config: Any = None,
) -> bool:
    if profile.authentication_type == "cli":
        return bool(profile.cli_auth_reference) or profile.provider in {
            "codex-cli",
            "antigravity-cli",
            "grok-cli",
        }
    return bool(_credential_api_key(profile, config))


def _uses_shared_openai_key(
    candidate: FreeTeamCandidateModel,
    credential: FreeTeamCredentialProfile,
) -> bool:
    """Identify the legacy FK pointer whose secret/state is no longer used."""

    return (
        str(candidate.provider or "").strip().lower() == "openai"
        and credential.id == "openai-complimentary"
    )


def _effective_candidate_billing_mode(
    candidate: FreeTeamCandidateModel,
    credential: FreeTeamCredentialProfile,
) -> str:
    if _uses_shared_openai_key(candidate, credential):
        return "complimentary"
    return str(credential.billing_mode or "")


def estimate_usage(
    prompt: str,
    *,
    max_output_tokens: int,
    provider_options: Mapping[str, Any] | None = None,
) -> dict[str, Decimal]:
    """無制限出力を避けるため、保守的な最大使用量を算出する。"""

    options = dict(provider_options or {})
    # tokenizer非依存でも過少予約にしないようUTF-8 byte数を上限近似に使う。
    initial_input_tokens = max(1, len((prompt or "").encode("utf-8")))
    request_count = max(1, int(options.get("_max_requests_per_turn") or 1))
    max_input_tokens = max(
        initial_input_tokens,
        int(options.get("_max_input_tokens") or initial_input_tokens),
    )
    input_tokens = initial_input_tokens + max(0, request_count - 1) * max_input_tokens
    output_tokens = max(1, int(max_output_tokens or 1)) * request_count
    raw_reasoning_tokens = options.get("max_reasoning_tokens")
    reasoning_tokens = max(
        0,
        int(
            output_tokens
            if raw_reasoning_tokens is None
            else int(raw_reasoning_tokens) * request_count
        ),
    )
    total_tokens = input_tokens + output_tokens + reasoning_tokens
    input_rate = _decimal(
        options.get("input_usd_per_million")
        or options.get("input_price_per_million")
    )
    output_rate = _decimal(
        options.get("output_usd_per_million")
        or options.get("output_price_per_million")
    )
    max_usd = (
        _decimal(input_tokens) * input_rate
        + _decimal(output_tokens + reasoning_tokens) * output_rate
    ) / Decimal("1000000")
    return {
        "input_tokens": _decimal(input_tokens),
        "output_tokens": _decimal(output_tokens),
        "reasoning_tokens": _decimal(reasoning_tokens),
        "total_tokens": _decimal(total_tokens),
        "requests": _decimal(request_count),
        "usd": max_usd,
    }


def _amount_for_metric(metric_type: str, usage: Mapping[str, Any]) -> Decimal:
    metric = str(metric_type or "").lower()
    if metric == "concurrency":
        return Decimal("1")
    if metric in {"requests", "rpm", "rpd"}:
        return max(Decimal("1"), _decimal(usage.get("requests")))
    if metric == "tpm":
        metric = "total_tokens"
    amount = _decimal(usage.get(metric))
    if metric == "usd":
        # Persisted quota balances have six decimal places. Round a monetary
        # hold UP before checking/admitting it, never down after reservation.
        if not amount.is_finite() or amount < 0:
            raise FreeTeamUnavailableError("free_team_invalid_usd_estimate")
        return amount.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    return amount


def _candidate_in_pool(candidate_id: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(candidate_id, str(pattern)) for pattern in patterns)


def openrouter_free_catalog_evidence(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return minimal persisted proof for a catalog-listed free model."""

    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return None
    from ..llm.deployment_resolver import is_retired_model

    if is_retired_model(model_id):
        return None
    pricing = item.get("pricing") if isinstance(item.get("pricing"), Mapping) else {}
    if model_id == "openrouter/free":
        return {
            "source": "openrouter_models_api",
            "model_id": model_id,
            "pricing": {
                key: str(pricing[key])
                for key in ("prompt", "completion", "request", "image")
                if key in pricing and isinstance(pricing[key], (str, int, float))
            },
        }
    if any(key not in pricing for key in ("prompt", "completion")):
        return None
    normalized: dict[str, str] = {}
    for key in ("prompt", "completion", "request", "image"):
        if key not in pricing:
            continue
        value = pricing[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            amount = Decimal(str(value))
        except Exception:
            return None
        if not amount.is_finite() or amount != 0:
            return None
        normalized[key] = str(value)
    if not {"prompt", "completion"}.issubset(normalized):
        return None
    return {
        "source": "openrouter_models_api",
        "model_id": model_id,
        "pricing": normalized,
    }


def _candidate_output_token_limit(
    candidate: FreeTeamCandidateModel,
    config: Any,
    *,
    strict_complimentary_only: bool,
) -> int | None:
    """Resolve a lease output ceiling without exceeding its persisted cap.

    Strict complimentary calls may carry an in-memory request-local cap. The
    lease forwards this value to the target factory, while quota estimation
    still reserves against the larger persisted candidate maximum.
    """

    candidate_limit = candidate.max_output_tokens or 1
    if type(candidate_limit) is not int or candidate_limit < 1:
        return None
    if not strict_complimentary_only:
        return candidate_limit
    request_limit = config_get(config, "runtime.target_max_output_tokens", None)
    if request_limit is None:
        return candidate_limit
    if type(request_limit) is not int or request_limit < 1:
        return None
    return min(candidate_limit, request_limit)


def _openrouter_quota_observation_is_current(
    credential: FreeTeamCredentialProfile,
    quotas: Mapping[str, FreeTeamQuotaPool] | None,
    now: datetime,
    config: Any = None,
) -> bool:
    """Require a fresh current-key /key observation before selection."""

    pools = quotas or {}
    rpm = pools.get("openrouter-free-rpm")
    rpd = pools.get("openrouter-free-rpd")
    if rpm is None or rpd is None:
        return False
    if (
        rpm.credential_profile_id != credential.id
        or rpm.metric_type != "rpm"
        or dict(rpm.reset_policy or {}).get("kind") != "minute"
        or str(dict(rpm.reset_policy or {}).get("timezone") or "UTC").upper() != "UTC"
        or rpd.credential_profile_id != credential.id
        or rpd.metric_type != "rpd"
        or dict(rpd.reset_policy or {}).get("kind") != "daily"
        or str(dict(rpd.reset_policy or {}).get("timezone") or "UTC").upper() != "UTC"
        or int(dict(rpd.reset_policy or {}).get("hour", 0) or 0) != 0
    ):
        return False
    state = dict(rpd.provider_sync_state or {})
    last_sync = rpd.last_provider_sync_at
    if (
        type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or state.get("source") != "openrouter_key_api"
        or not isinstance(last_sync, datetime)
        or not state.get("credential_binding")
        or state.get("utc_day") != _utc_naive(now).date().isoformat()
    ):
        return False
    try:
        from . import free_team_provider_observer as observer

        limit = Decimal(str(state.get("provider_limit")))
        observed = Decimal(str(rpd.provider_observed_usage))
        recorded_total = Decimal(str(state.get("observed_total")))
        age = (_utc_naive(now) - _utc_naive(last_sync)).total_seconds()
    except (ValueError, TypeError, ArithmeticError):
        return False
    included = None
    if state.get("included_through") is not None:
        try:
            included = _utc_naive(
                datetime.fromisoformat(str(state["included_through"]))
            )
        except (ValueError, TypeError):
            return False
    if (
        any(
            not value.is_finite()
            or value < 0
            or value > observer.MAX_SAFE_INTEGER
            or value != value.to_integral_value()
            for value in (limit, observed, recorded_total)
        )
        or limit < 1
        or observed != recorded_total
        or included is not None
        and (
            included.date() != _utc_naive(now).date()
            or included > _utc_naive(now)
        )
        or age < 0
        or age > observer.STALE_AFTER_SECONDS
    ):
        return False
    try:
        binding = observer.quota_binding_fingerprint(
            credential.id,
            bool(credential.enabled),
            _credential_api_key(credential, config),
            credential.base_url,
            str(credential.updated_at or ""),
            openrouter_credential_source(config),
        )
    except Exception:
        return False
    return bool(binding) and binding == state.get("credential_binding")


def _candidate_matches(
    candidate: FreeTeamCandidateModel,
    credential: FreeTeamCredentialProfile,
    *,
    candidate_patterns: tuple[str, ...],
    required_capabilities: set[str],
    now: datetime,
    quotas: Mapping[str, FreeTeamQuotaPool] | None = None,
    config: Any = None,
    strict_complimentary_only: bool = False,
    ignore_cooldown: bool = False,
) -> bool:
    from ..llm.deployment_resolver import is_retired_model
    if is_retired_model(candidate.model):
        return False
    shared_openai_key = _uses_shared_openai_key(candidate, credential)
    billing_mode = _effective_candidate_billing_mode(candidate, credential)
    if not candidate.enabled or (not shared_openai_key and not credential.enabled):
        return False
    if not shared_openai_key and credential.status not in {"ready", "active"}:
        return False
    if billing_mode not in ALLOWED_BILLING_MODES:
        return False
    if strict_complimentary_only and billing_mode not in {
        "complimentary",
        "free_tier",
    }:
        return False
    if strict_complimentary_only:
        if (
            _candidate_output_token_limit(
                candidate,
                config,
                strict_complimentary_only=True,
            )
            is None
        ):
            return False
        # A provider-side balance is spendable credit, not proof of a free
        # entitlement. DeepSeek has no Free Team free-tier evidence path, and
        # USD quota pools represent monetary budgets rather than complimentary
        # request/token limits.
        if str(candidate.provider or "").strip().lower() == "deepseek":
            return False
        if any(
            (quota := (quotas or {}).get(str(quota_id))) is not None
            and str(quota.metric_type or "").strip().lower() == "usd"
            for quota_id in (candidate.quota_pool_ids or [])
        ):
            return False
    if not shared_openai_key and credential.allow_paid_overage:
        # 無料Teamでは有料超過を許可した認証も自動選択しない。
        return False
    if billing_mode == "complimentary" and not candidate.quota_pool_ids:
        return False
    if not _credential_configured(credential, config):
        return False
    if candidate.status not in {"ready", "active"}:
        return False
    if str(candidate.provider or "").strip().lower() == "openai":
        circuit_blocked = openai_provider_is_blocked(config, now=now)
        complimentary_probe_due = (
            billing_mode == "complimentary"
            and openai_provider_probe_available(config, now=now)
        )
        if circuit_blocked and not complimentary_probe_due:
            return False
    if (
        not ignore_cooldown
        and candidate.cooldown_until
        and candidate.cooldown_until > now
    ):
        return False
    if not _candidate_in_pool(candidate.id, candidate_patterns):
        return False
    capabilities = {str(value) for value in (candidate.capabilities or [])}
    if not required_capabilities.issubset(capabilities):
        return False
    if "tools" in required_capabilities:
        policy = dict(candidate.tool_call_policy or {})
        if policy.get("allowed") is False:
            return False
        if (
            billing_mode == "complimentary"
            and policy.get("complimentary_tool_calls_allowed") is not True
        ):
            return False
    if candidate.provider == "openrouter":
        from . import free_team_provider_observer as observer

        if (
            credential.id != "openrouter-free"
            or not observer.is_official_endpoint(credential.base_url)
            or not {"openrouter-free-rpm", "openrouter-free-rpd"}.issubset(
                {str(value) for value in (candidate.quota_pool_ids or [])}
            )
        ):
            return False
        if not _openrouter_quota_observation_is_current(
            credential, quotas, now, config
        ):
            return False
        if candidate.model == "openrouter/free":
            catalog_verified = True
        else:
            proof = dict((candidate.provider_options or {}).get("free_catalog_evidence") or {})
            pricing = proof.get("pricing") if isinstance(proof.get("pricing"), dict) else {}
            def catalog_zero(value: Any) -> bool:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return False
                try:
                    amount = Decimal(str(value))
                except Exception:
                    return False
                return amount.is_finite() and amount == 0

            catalog_verified = (
                proof.get("source") == "openrouter_models_api"
                and proof.get("model_id") == candidate.model
                and catalog_zero(pricing.get("prompt"))
                and catalog_zero(pricing.get("completion"))
                and all(
                    key not in pricing or catalog_zero(pricing[key])
                    for key in ("request", "image")
                )
            )
        if not catalog_verified:
            return False
        options = dict(candidate.provider_options or {})
        max_price = options.get("max_price")
        zero_dimensions = {"prompt", "completion", "request", "image"}

        def option_price_zero(value: Any) -> bool:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return False
            try:
                amount = Decimal(str(value))
            except Exception:
                return False
            return amount.is_finite() and amount == 0

        max_price_is_zero = (
            option_price_zero(max_price)
            or isinstance(max_price, dict)
            and not set(max_price) - zero_dimensions
            and bool(max_price)
            and all(option_price_zero(value) for value in max_price.values())
        )
        if not max_price_is_zero:
            return False
        paid_fallback_disabled = options.get("allow_fallbacks") is False
        if not paid_fallback_disabled or options.get("require_free") is not True:
            return False
    return True


async def _openai_complimentary_candidate_verified(session: AsyncSession) -> bool:
    """Use the same single-key identity and owner confirmation for every route."""

    from . import openai_confirmation_service as confirmation

    try:
        current = await confirmation.authoritative_config(session)
        shared_key_identity = await confirmation.credential_identity(
            session, current, "openai_api_key"
        )
    except Exception:
        # A missing/unavailable confirmation store is not permission to send.
        return False
    return not complimentary_stop_reason(
        current,
        {
            "api_key_sha256": shared_key_identity["api_key_sha256"],
            "organization_id": shared_key_identity["sdk_organization_id"],
            "project_id": shared_key_identity["sdk_project_id"],
            "base_url": shared_key_identity["base_url"],
        },
    )


def _candidate_score(
    candidate: FreeTeamCandidateModel,
    now: datetime,
    quotas: Mapping[str, FreeTeamQuotaPool] | None = None,
) -> tuple[Any, ...]:
    billing_rank = {
        "complimentary": 0,
        "free_tier": 1,
        "promo_credit": 2,
        "subscription_cli": 3,
    }.get(
        _effective_candidate_billing_mode(
            candidate, candidate.credential_profile
        ),
        9,
    )
    success_count = int(candidate.success_count or 0)
    failure_count = int(candidate.failure_count or 0)
    success_total = success_count + failure_count
    failure_rate = (
        failure_count / success_total if success_total else 0.0
    )
    quota_pace_deltas: list[float] = []
    for quota_id in candidate.quota_pool_ids or []:
        quota = (quotas or {}).get(str(quota_id))
        if quota is None or _decimal(quota.limit_value) <= 0:
            continue
        usable = max(
            Decimal("1"), _decimal(quota.limit_value) - quota.safety_margin
        )
        consumed_ratio = float(
            (
                _decimal(quota.consumed)
                + _decimal(quota.reserved)
                + quota.provider_sync_delta
            )
            / usable
        )
        elapsed_ratio = 0.0
        if quota.window_start and quota.window_end and quota.window_end > quota.window_start:
            elapsed_ratio = min(
                1.0,
                max(
                    0.0,
                    (now - quota.window_start).total_seconds()
                    / (quota.window_end - quota.window_start).total_seconds(),
                ),
            )
        # lower-is-better: windowの経過率より消化率が低い枠はreset前に
        # 使い切れるようboostし、消化が先行する枠は温存する。
        quota_pace_deltas.append(consumed_ratio - elapsed_ratio)
    quota_pressure = (
        sum(quota_pace_deltas) / len(quota_pace_deltas)
        if quota_pace_deltas
        else 0.0
    )
    # 同順位ではselection_countを使い、DB共有の公平なローテーションにする。
    return (
        billing_rank,
        float(candidate.priority or 100) + quota_pressure * 100.0,
        failure_rate,
        int(candidate.selection_count or 0) / max(1, int(candidate.weight or 1)),
        float(candidate.average_latency_ms or 0),
        candidate.id,
    )


def _bounded_routing_enabled(config: Any) -> bool:
    if config is None:
        return False
    value = config_get(
        config,
        "decision_layer.jev_free_team_routing_enabled",
        None,
    )
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return str(value).strip().casefold() not in {"", "0", "false", "off", "no"}


async def _semantic_candidate_order(
    candidates: list[FreeTeamCandidateModel],
    *,
    intent: RouteIntent,
    prompt: str,
    required_capabilities: set[str],
    config: Any,
    now: datetime,
    quotas: Mapping[str, FreeTeamQuotaPool],
    receipt_sink: list[DecisionReceipt] | None = None,
) -> list[FreeTeamCandidateModel]:
    """Reorder only a near-tied, already-safe cohort; never widen eligibility.

    Billing/quota/credential/capability/provider-health constraints have already
    been applied by the Host. The returned order is still revalidated under row
    and quota locks before any lease is created.
    """
    if (
        len(candidates) < 2
        or not str(prompt or "").strip()
        or not _bounded_routing_enabled(config)
    ):
        return candidates

    # A singular literal candidate is an explicit Host/user constraint. Never
    # reinterpret it semantically.
    if (
        len(intent.candidate_ids) == 1
        and not any(ch in intent.candidate_ids[0] for ch in "*?[]")
    ):
        return candidates

    try:
        from .candidate_decision_service import (
            CandidateDecisionError,
            CandidateDecisionRouter,
        )
        from .jev_decision_service import JevDecisionService
        from .outbound_privacy_service import (
            ExternalProviderBlocked,
            OutboundPrivacyGateway,
            PrivacyError,
            current_effective_privacy_mode,
        )
    except Exception:
        return candidates

    if current_effective_privacy_mode(config) == "local_only":
        return candidates

    scored = [(item, _candidate_score(item, now, quotas)) for item in candidates]
    best = scored[0][1]
    cohort: list[FreeTeamCandidateModel] = []
    for item, score in scored:
        # Preserve billing class and quota/priority pacing. Jev is allowed only
        # to break a near-tie inside that deterministic safety tier.
        if score[0] != best[0] or float(score[1]) > float(best[1]) + 3.0:
            continue
        cohort.append(item)
        if len(cohort) >= 8:
            break
    if len(cohort) < 2:
        return candidates

    async def _never_llm(*_args: Any, **_kwargs: Any) -> str:
        return "none"

    bounded_candidates = {
        item.id: {
            "provider": str(item.provider or "")[:80],
            "model": str(item.model or "")[:120],
            "quality_class": str(item.quality_class or "")[:40],
            "capabilities": sorted(str(v) for v in (item.capabilities or []))[:16],
            "max_input_tokens": int(item.max_input_tokens or 0),
            "max_output_tokens": int(item.max_output_tokens or 0),
            "average_latency_ms": round(float(item.average_latency_ms or 0), 2),
        }
        for item in cohort
    }
    router = CandidateDecisionRouter(
        llm_choose=_never_llm,
        jev=JevDecisionService(
            OutboundPrivacyGateway(config),
            timeout_seconds=5,
            max_retries=0,
            purpose="routing",
        ),
        jev_enabled=True,
        local_only=False,
        minimum_confidence=0.65,
        allow_llm_fallback=False,
        allow_abstain=True,
    )
    try:
        choice = await router.choose(
            {
                "task": str(prompt)[:2400],
                "required_capabilities": sorted(required_capabilities),
                "routing_profile": str(intent.routing_profile_id or "")[:80],
                "pool": str(intent.pool_id or "")[:80],
            },
            (
                "Choose the best suited model candidate for the task intent from "
                "this Host-approved near-tied cohort. Do not optimize around "
                "billing, quota, credentials, or permissions; the Host owns them."
            ),
            bounded_candidates,
            decision_kind="free_team_route",
            candidate_metadata={"candidate_count": len(bounded_candidates)},
        )
    except asyncio.CancelledError:
        raise
    except (CandidateDecisionError, ExternalProviderBlocked, PrivacyError):
        return candidates

    selected = next((item for item in cohort if item.id == choice.choice), None)
    shadow = str(config_get(config, "decision_layer.jev_free_team_shadow_mode", False)).strip().casefold() in {"true", "1", "on", "yes"}
    receipt = finalize_decision_receipt(
        getattr(choice, "receipt", None),
        baseline_candidate=candidates[0].id,
        shadow_candidate=selected.id if shadow and selected is not None else "",
        agreement=(candidates[0].id == selected.id) if selected is not None else None,
        shadow_mode=shadow,
        host_action="preserve_order" if shadow or selected is None else "reorder",
        outcome="ordering_preserved" if shadow or selected is None else "reordered",
        validation_result="host_candidate_valid" if selected is not None else "abstained",
    )
    if receipt is not None and receipt_sink is not None:
        receipt_sink.append(receipt)
    if selected is None:
        return candidates
    if shadow:
        return candidates

    # Host-side identity revalidation before the later locked hard-constraint
    # revalidation. The remaining deterministic order is preserved.
    return [selected] + [item for item in candidates if item.id != selected.id]


def _lease_effort(intent: RouteIntent, candidate: FreeTeamCandidateModel) -> str:
    if intent.effort:
        return intent.effort
    candidate_effort = str(candidate.effort or "")
    policy = str(intent.effort_policy or "same").lower()
    if policy == "lower" and candidate_effort:
        order = ["minimal", "low", "medium", "high", "max"]
        if candidate_effort in order:
            return order[max(0, order.index(candidate_effort) - 1)]
    if policy in {"none", "default"}:
        return ""
    return candidate_effort


def _cli_quota_contract_safe(
    candidate: FreeTeamCandidateModel,
    quotas: Mapping[str, FreeTeamQuotaPool],
) -> bool:
    """生成前token hard-capを持たないCLIはconcurrency枠だけに限定する。"""

    if str(candidate.provider) not in CLI_PROVIDERS:
        return True
    linked = [quotas.get(str(value)) for value in (candidate.quota_pool_ids or [])]
    return bool(linked) and all(
        quota is not None and str(quota.metric_type).lower() == "concurrency"
        for quota in linked
    )


async def _active_candidate_reservations(
    session: AsyncSession,
    candidate_id: str,
    now: datetime,
) -> int:
    result = await session.execute(
        select(func.count(FreeTeamReservation.id)).where(
            FreeTeamReservation.candidate_id == candidate_id,
            FreeTeamReservation.status == "reserved",
            FreeTeamReservation.expires_at > now,
        )
    )
    return int(result.scalar_one() or 0)


GEMINI_PROMO_MONTHLY_QUOTA_ID = "gemini-promo-monthly-usd"
GEMINI_PROMO_LOCAL_SCOPE = "freeteam-gemini-promo"


def _gemini_promo_scope(quota: FreeTeamQuotaPool) -> str:
    """Resolve the local ledger name without changing stored policy."""
    return str(
        (quota.reset_policy or {}).get("billing_scope_id") or GEMINI_PROMO_LOCAL_SCOPE
    ).strip()


_GEMINI_PROMO_MAX_USAGE_ROWS = 10000
GEMINI_FREE_QUOTA_IDS = frozenset(
    {"gemini-free-rpm", "gemini-free-tpm", "gemini-free-rpd"}
)
_GEMINI_FREE_CREDENTIAL_PROFILE_ID = "gemini-free"


def _is_gemini_promo_usd_quota(quota: FreeTeamQuotaPool) -> bool:
    return (
        str(quota.id) == GEMINI_PROMO_MONTHLY_QUOTA_ID
        and str(quota.metric_type or "").casefold() == "usd"
    )


def _matches_selected_free_credential_snapshot(
    reservation: FreeTeamReservation,
) -> bool:
    """Match the selected credential snapshot, not actual Google billing.

    Legacy, incomplete, and malformed snapshots remain unverified.
    """
    usage = reservation.estimated_usage
    if not isinstance(usage, Mapping):
        return False
    profile_id = usage.get("credential_profile_id")
    billing_mode = usage.get("billing_mode")
    if not isinstance(profile_id, str) or not isinstance(billing_mode, str):
        return False
    if (
        profile_id.strip() != _GEMINI_FREE_CREDENTIAL_PROFILE_ID
        or billing_mode.strip().casefold() != "free_tier"
    ):
        return False
    raw_pool_ids = reservation.quota_pool_ids
    if not isinstance(raw_pool_ids, (list, tuple, set, frozenset)):
        return False
    if not all(isinstance(item, str) for item in raw_pool_ids):
        return False
    pool_ids = set(raw_pool_ids)
    return bool(pool_ids) and pool_ids.issubset(GEMINI_FREE_QUOTA_IDS)


async def _gemini_promo_cross_route_usage(
    session: AsyncSession, billing_scope_id: str, now: datetime
) -> dict[str, Any]:
    """Count potentially billable Gemini requests in the UTC month.

    In-pool requests are represented by their reserved/consumed quota, and
    must not also count as external usage. Unknown identities or pricing
    information block *new* promo reservations rather than permit paid overage.
    """
    scope = str(billing_scope_id or "").strip()
    quota = await session.get(FreeTeamQuotaPool, GEMINI_PROMO_MONTHLY_QUOTA_ID)
    policy = dict(quota.reset_policy or {}) if quota is not None else {}
    if (
        not scope
        or scope.lower() in {"default", "unknown", "none"}
        or quota is None
        or _gemini_promo_scope(quota) != scope
        or policy.get("kind") != "monthly"
        or str(policy.get("timezone", "UTC")).upper() != "UTC"
    ):
        raise FreeTeamUnavailableError("gemini_promo_scope_unverified")

    current = _utc_naive(now)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    # A historical scope does not prove which credential was used. Count all
    # independent Gemini rows conservatively, including other explicit scopes.
    rows = (
        await session.execute(
            text(
                "SELECT provider, billing_scope_id, quota_reservation_id, model, "
                "pricing_status, list_total_cost, provider_reported_cost "
                "FROM token_usage "
                "WHERE (lower(provider) LIKE :provider_gemini_pattern "
                "OR lower(provider) LIKE :provider_google_pattern) "
                "AND created_at >= :started AND created_at < :ended "
                "LIMIT :limit"
            ),
            {
                "provider_gemini_pattern": "%gemini%",
                "provider_google_pattern": "%google%",
                "started": start,
                "ended": end,
                "limit": _GEMINI_PROMO_MAX_USAGE_ROWS + 1,
            },
        )
    ).mappings().all()
    if len(rows) > _GEMINI_PROMO_MAX_USAGE_ROWS:
        raise FreeTeamUnavailableError("gemini_promo_usage_incomplete")

    # SQL narrows the bounded scan; the provider registry determines membership.
    rows = [row for row in rows if canonical_provider(row["provider"]) == "gemini"]

    linked_ids: set[uuid.UUID] = set()
    for row in rows:
        raw_id = row["quota_reservation_id"]
        if raw_id is None:
            continue
        try:
            linked_ids.add(uuid.UUID(str(raw_id)))
        except (ValueError, TypeError, AttributeError):
            raise FreeTeamUnavailableError(
                "gemini_promo_reservation_unverified"
            ) from None
    linked = {}
    if linked_ids:
        reservations = (
            await session.execute(
                select(FreeTeamReservation).where(
                    FreeTeamReservation.id.in_(sorted(linked_ids))
                )
            )
        ).scalars().all()
        linked = {str(item.id): item for item in reservations}

    reported = Decimal("0")
    estimated = Decimal("0")
    included = 0
    for row in rows:
        raw_id = row["quota_reservation_id"]
        if raw_id is not None:
            reservation = linked.get(str(uuid.UUID(str(raw_id))))
            if reservation is None or not isinstance(reservation.estimated_usage, Mapping):
                raise FreeTeamUnavailableError("gemini_promo_reservation_unverified")
            reserved_scope = reservation.estimated_usage.get("billing_scope_id")
            if reserved_scope is not None and reserved_scope != scope:
                raise FreeTeamUnavailableError("gemini_promo_scope_mismatch")
            row_scope = str(row["billing_scope_id"] or "").strip()
            if (
                reservation.status in {"reserved", "uncertain", "committed"}
                and reserved_scope is None
                and row_scope.casefold() in {"", "default"}
                and _matches_selected_free_credential_snapshot(reservation)
            ):
                continue
            pool_ids = reservation.quota_pool_ids
            if (
                not isinstance(pool_ids, (list, tuple, set, frozenset))
                or not all(isinstance(item, str) for item in pool_ids)
                or GEMINI_PROMO_MONTHLY_QUOTA_ID not in pool_ids
                or (
                    reservation.estimated_usage.get("credential_profile_id")
                    == _GEMINI_FREE_CREDENTIAL_PROFILE_ID
                    and reservation.estimated_usage.get("billing_mode") == "free_tier"
                )
                or reservation.status not in {"reserved", "uncertain", "committed"}
            ):
                raise FreeTeamUnavailableError("gemini_promo_reservation_unverified")
            continue

        state = str(row["pricing_status"] or "").lower()
        if state not in {"priced", "provider_reported", "estimated"}:
            raise FreeTeamUnavailableError("gemini_promo_usage_price_unknown")
        actual = row["provider_reported_cost"]
        if state == "provider_reported" and actual is None:
            raise FreeTeamUnavailableError("gemini_promo_usage_price_unknown")
        source_value = (
            actual if actual is not None else row["list_total_cost"]
        )
        try:
            amount = Decimal(str(source_value))
        except (TypeError, ValueError, ArithmeticError):
            raise FreeTeamUnavailableError(
                "gemini_promo_usage_price_unknown"
            ) from None
        if not amount.is_finite() or amount < 0:
            raise FreeTeamUnavailableError("gemini_promo_usage_price_unknown")
        if actual is None and amount == 0:
            # Missing/unpriced usage must not be treated as free just because
            # a locally calculated list-cost field rounded to zero.
            raise FreeTeamUnavailableError("gemini_promo_usage_price_unknown")
        if actual is not None:
            reported += amount
        else:
            estimated += amount
        # Count all billable models in the same scope. Other Gemini models can
        # also consume the project's credit; ignoring them risks paid overage.
        included += 1

    return {
        "provider_reported": reported,
        "price_estimate": estimated,
        "amount": reported + estimated,
        "included_rows": included,
    }


async def _gemini_promo_uncertain_hold_total(
    session: AsyncSession,
) -> Decimal:
    """Validate and total full booked holds for unresolved promo USD requests."""
    rows = (
        await session.execute(
            select(
                FreeTeamReservation.quota_pool_ids,
                FreeTeamReservation.estimated_usage,
                FreeTeamReservation.actual_usage,
            ).where(FreeTeamReservation.status == "uncertain")
        )
    ).all()
    total = Decimal("0")
    for raw_pool_ids, raw_estimate, raw_actual in rows:
        estimate = raw_estimate if isinstance(raw_estimate, Mapping) else {}
        original_holds = estimate.get("quota_reservations")
        if not isinstance(original_holds, Mapping):
            original_holds = {}
        remaining_holds = estimate.get("remaining_quota_reservations")
        actual = raw_actual if isinstance(raw_actual, Mapping) else {}
        quota_actual = actual.get("quota_actual")
        quota_actual = quota_actual if isinstance(quota_actual, Mapping) else {}
        pool_ids = (
            {str(value) for value in raw_pool_ids}
            if isinstance(raw_pool_ids, (list, tuple, set))
            else set()
        )
        if (
            GEMINI_PROMO_MONTHLY_QUOTA_ID not in pool_ids
            and GEMINI_PROMO_MONTHLY_QUOTA_ID not in original_holds
            and not (
                isinstance(remaining_holds, Mapping)
                and GEMINI_PROMO_MONTHLY_QUOTA_ID in remaining_holds
            )
        ):
            continue
        if GEMINI_PROMO_MONTHLY_QUOTA_ID in quota_actual:
            # finalize_route_lease only writes quota_actual after applying an
            # explicit nonzero USD amount and releasing that pool's hold.
            continue
        if GEMINI_PROMO_MONTHLY_QUOTA_ID not in original_holds:
            raise FreeTeamUnavailableError("gemini_promo_hold_unverified")
        try:
            original = _decimal(
                original_holds[GEMINI_PROMO_MONTHLY_QUOTA_ID]
            )
            if isinstance(remaining_holds, Mapping):
                if GEMINI_PROMO_MONTHLY_QUOTA_ID not in remaining_holds:
                    raise ValueError("promo_hold_missing")
                remaining = _decimal(
                    remaining_holds[GEMINI_PROMO_MONTHLY_QUOTA_ID]
                )
            else:
                remaining = original
        except Exception:
            raise FreeTeamUnavailableError("gemini_promo_hold_unverified") from None
        if (
            not original.is_finite()
            or original <= 0
            or not remaining.is_finite()
            or remaining < original
        ):
            raise FreeTeamUnavailableError("gemini_promo_hold_unverified")
        total += remaining
    return total


async def _gemini_promo_quota_snapshot(
    session: AsyncSession, quota: FreeTeamQuotaPool, now: datetime
) -> dict[str, Any]:
    """Fail-closed promo balance, shared by admission and status projection."""
    if (
        quota.id != GEMINI_PROMO_MONTHLY_QUOTA_ID
        or str(quota.metric_type).lower() != "usd"
    ):
        raise FreeTeamUnavailableError("gemini_promo_quota_invalid")
    scope = _gemini_promo_scope(quota)
    external = await _gemini_promo_cross_route_usage(session, scope, now)
    expired = bool(quota.window_end and _utc_naive(quota.window_end) <= _utc_naive(now))
    consumed = Decimal("0") if expired else _decimal(quota.consumed)
    reserved = _decimal(quota.reserved)
    provider_delta = Decimal("0") if expired else quota.provider_sync_delta
    limit = min(Decimal("10"), _decimal(quota.limit_value))
    safety = quota.safety_margin
    amounts = (consumed, reserved, provider_delta, limit, safety)
    if any(not amount.is_finite() or amount < 0 for amount in amounts):
        raise FreeTeamUnavailableError("gemini_promo_quota_invalid")
    uncertain_holds = await _gemini_promo_uncertain_hold_total(session)
    if reserved < uncertain_holds:
        raise FreeTeamUnavailableError("gemini_promo_hold_unverified")
    if quota.status not in {"active", "exhausted"}:
        return {"available": Decimal("0"), "cross_route": external}
    available = max(
        Decimal("0"),
        limit - safety - consumed - reserved - provider_delta - external["amount"],
    )
    return {"available": available, "cross_route": external}


def _read_only_quota_available(
    quota: FreeTeamQuotaPool,
    now: datetime,
    usage_tier: str | None = None,
    config: Any = None,
) -> Decimal | None:
    """Project one quota's available amount without mutating its ORM row.

    The normal lease/preflight path resets an expired ORM row in a transaction.
    The scheduler gate must not do that write, so the same reset calculation is
    applied to local scalar values instead.  ``None`` means the quota policy is
    malformed and the caller must fail closed.
    """

    current = _utc_naive(now)
    policy = dict(quota.reset_policy or {})
    status = str(quota.status or "")
    consumed = _decimal(quota.consumed)
    observed = _decimal(quota.provider_observed_usage)
    provider_state = dict(quota.provider_sync_state or {})
    window_end = quota.window_end
    if window_end is not None:
        window_end = _utc_naive(window_end)
    if window_end is not None and window_end <= current:
        try:
            if _next_window_end(policy, current) is None:
                return None
        except (TypeError, ValueError, FreeTeamUnavailableError):
            return None
        consumed = Decimal("0")
        observed = Decimal("0")
        if not (
            quota.id in OPENAI_DAILY_QUOTA_GROUPS
            and provider_state.get("source") == "openai_usage_api"
        ):
            provider_state = {}
        if status == "exhausted":
            status = "active"
    if status != "active" and not (
        status == "exhausted" and quota.id in OPENAI_DAILY_QUOTA_GROUPS
    ):
        return Decimal("0")

    limit = _decimal(quota.limit_value)
    openrouter_provider_limit: Decimal | None = None
    if quota.id in OPENAI_DAILY_QUOTA_GROUPS:
        effective_config = config if config is not None else {
            "openai": {"usage_tier": usage_tier}
        }
        return _openai_quota_balance(
            quota,
            effective_config,
            now=current,
            local_consumed=consumed,
            reserved=_decimal(quota.reserved),
            provider_observed=observed,
            provider_state=provider_state,
        )["available"]
    elif quota.id == "openrouter-free-rpd":
        openrouter_tier = free_team_profile(config).get("openrouter_free_tier")
        if (
            isinstance(openrouter_tier, bool)
            or openrouter_tier not in OPENROUTER_FREE_DAILY_TIERS
        ):
            openrouter_tier = OPENROUTER_DEFAULT_FREE_DAILY_TIER
        try:
            openrouter_provider_limit = Decimal(str(openrouter_tier))
        except (ArithmeticError, TypeError, ValueError):
            return None
        if provider_state.get("source") == "openrouter_key_api":
            try:
                provider_limit = Decimal(str(provider_state.get("provider_limit")))
                if provider_limit.is_finite() and provider_limit >= 1:
                    openrouter_provider_limit = min(
                        openrouter_provider_limit, provider_limit
                    )
            except (ArithmeticError, TypeError, ValueError):
                return None

    ratio = _decimal(quota.safety_margin_ratio)
    units = _decimal(quota.safety_margin_units)
    safety_margin = max(units, limit * ratio)
    daily_cap = max(Decimal("0"), limit - safety_margin)
    if quota.id == "openrouter-free-rpd":
        local_cap = max(
            Decimal("0"),
            _decimal(quota.limit_value)
            - max(
                _decimal(quota.safety_margin_units),
                _decimal(quota.limit_value) * _decimal(quota.safety_margin_ratio),
            ),
        )
        provider_cap = max(
            Decimal("0"),
            (openrouter_provider_limit or Decimal(0))
            - Decimal(OPENROUTER_SAFETY_MARGIN_REQUESTS),
        )
        daily_cap = min(local_cap, provider_cap)
    external_delta = observed
    try:
        candidate_delta = Decimal(str(provider_state["external_delta"]))
        observed_total = Decimal(str(provider_state["observed_total"]))
        if (
            provider_state.get("schema_version") == 1
            and observed_total == observed
            and candidate_delta.is_finite()
            and candidate_delta >= 0
        ):
            external_delta = candidate_delta
    except (KeyError, ArithmeticError, TypeError, ValueError):
        pass
    available = daily_cap - consumed - _decimal(quota.reserved) - external_delta
    return max(Decimal("0"), available)


def _locked_quota_available(quota: FreeTeamQuotaPool, config: Any) -> Decimal:
    """Return the effective balance after the caller locks and resets a pool."""

    if quota.id == "openrouter-free-rpd":
        openrouter_tier = free_team_profile(config).get(
            "openrouter_free_tier", OPENROUTER_DEFAULT_FREE_DAILY_TIER
        )
        return quota.available_for_tier(openrouter_free_tier=openrouter_tier)
    if quota.id in OPENAI_DAILY_QUOTA_GROUPS:
        return quota.available_for_tier(
            config_get(config, "openai.usage_tier", None)
        )
    return quota.available


async def _read_only_openai_quota_available(
    session: AsyncSession,
    quota: FreeTeamQuotaPool,
    config: Any,
    now: datetime,
) -> Decimal | None:
    """Read the exact OpenAI complimentary cap without reserving it.

    The OpenAI lease path has additional billing-scope and central-usage
    accounting that a generic quota projection cannot reproduce.  Unknown
    scope/usage state therefore denies the candidate instead of estimating it.
    Expired rows are also denied here; the normal reservation path is the owner
    of the transactional reset.
    """

    if quota.id not in OPENAI_DAILY_QUOTA_GROUPS:
        return None
    current = _utc_naive(now)
    if quota.window_end is not None and _utc_naive(quota.window_end) <= current:
        return None
    scope = str(config_get(config, "openai.billing_scope_id", "") or "")
    if not scope:
        return None
    policy = dict(quota.reset_policy or {})
    policy_scope = policy.get("billing_scope_id")
    if policy_scope not in {None, scope}:
        return None
    if policy_scope is None and any(
        _decimal(getattr(quota, field_name, 0)) > 0
        for field_name in ("consumed", "reserved", "provider_observed_usage")
    ):
        return None
    group = OPENAI_DAILY_QUOTA_GROUPS[quota.id]
    try:
        if await _unmapped_central_usage(session, group, current):
            return None
        central = await _central_unmetered_usage(session, scope, group, current)
        snapshot = _openai_quota_snapshot(
            quota, config, central_usage=central
        )
    except Exception:
        return None
    return snapshot["available"]


def _strict_openrouter_intent(intent: RouteIntent) -> bool:
    return any(
        fnmatchcase("openrouter-free-router", pattern)
        or str(pattern).startswith("openrouter-dynamic-")
        for pattern in intent.candidate_ids
    )


async def preflight_free_team_api_task(
    intent: RouteIntent,
    *,
    prompt: str,
    required_capabilities: Iterable[str] = ("text",),
    config: Any = None,
    session: AsyncSession | None = None,
    openrouter_quota_refresher: Callable[
        [], Awaitable[Mapping[str, Any]]
    ]
    | None = None,
    now: datetime | None = None,
) -> FreeTeamApiTaskPreflight:
    """Prove a strict complimentary/free-tier candidate before task creation.

    This gate is deliberately separate from ``acquire_route_lease``: it never
    calls the provider, reserves quota, creates a reservation, or debits a
    ledger.  It uses the same strict candidate predicate and request estimate
    as the normal Free Team route, and returns ``eligible=True`` only when one
    candidate's complete quota set can cover this request now.
    """

    if intent.kind != "pool":
        return FreeTeamApiTaskPreflight(False, "intent_not_pool")
    now_value = _utc_naive(now or _utcnow())
    capabilities = {str(value) for value in required_capabilities if str(value)}

    # The API-task lane is the only supported path for the existing OpenRouter
    # metadata refresher.  A missing callback is a denial, even if an old local
    # observation happens to be present.
    if (
        config_get(config, "runtime.free_team_api_task", False) is True
        and _strict_openrouter_intent(intent)
    ):
        if openrouter_quota_refresher is None:
            return FreeTeamApiTaskPreflight(False, "openrouter_observer_unavailable")
        try:
            refresh_result = dict(await openrouter_quota_refresher())
        except Exception:
            return FreeTeamApiTaskPreflight(False, "openrouter_observer_failed")
        if refresh_result.get("status") != "observed":
            failure = _safe_openrouter_observer_failure(refresh_result.get("failure"))
            return FreeTeamApiTaskPreflight(False, f"openrouter_{failure}")

    own_session = session is None
    if session is None:
        session = await get_db_session()
    try:
        candidates = list(
            (
                await session.execute(
                    select(FreeTeamCandidateModel)
                    .where(FreeTeamCandidateModel.enabled.is_(True))
                    .options(selectinload(FreeTeamCandidateModel.credential_profile))
                )
            )
            .scalars()
            .all()
        )
        quotas = {
            item.id: item
            for item in (
                await session.execute(select(FreeTeamQuotaPool))
            )
            .scalars()
            .all()
        }
        openai_verified = False
        if any(
            item.credential_profile is not None
            and _uses_shared_openai_key(item, item.credential_profile)
            for item in candidates
        ):
            openai_verified = await _openai_complimentary_candidate_verified(session)

        strict_candidates = []
        for candidate in candidates:
            credential = candidate.credential_profile
            if credential is None or not _candidate_matches(
                candidate,
                credential,
                candidate_patterns=intent.candidate_ids,
                required_capabilities=capabilities,
                now=now_value,
                quotas=quotas,
                config=config,
                strict_complimentary_only=True,
                ignore_cooldown=True,
            ):
                continue
            if (
                _uses_shared_openai_key(candidate, credential)
                and not openai_verified
            ):
                continue
            if not _cli_quota_contract_safe(candidate, quotas):
                continue
            strict_candidates.append(candidate)

        if not strict_candidates:
            circuit = provider_circuit_status(config, now=now_value)
            reason = (
                "openai_provider_blocked"
                if circuit["provider_blocked"]
                else "no_strict_free_candidate"
            )
            return FreeTeamApiTaskPreflight(False, reason)

        saw_cooldown = False
        saw_quota_block = False
        saw_missing_quota = False
        saw_concurrency = False
        saw_prompt_limit = False
        for candidate in sorted(
            strict_candidates,
            key=lambda item: _candidate_score(item, now_value, quotas),
        ):
            if candidate.cooldown_until is not None and candidate.cooldown_until > now_value:
                saw_cooldown = True
                continue
            options = dict(candidate.provider_options or {})
            concurrency_limit = max(1, int(options.get("concurrency_limit") or 64))
            if await _active_candidate_reservations(session, candidate.id, now_value) >= concurrency_limit:
                saw_concurrency = True
                continue
            max_requests = (
                max(8, int(options.get("max_requests_per_turn") or 8))
                if "tools" in capabilities
                else 1
            )
            estimates = estimate_usage(
                prompt,
                max_output_tokens=int(candidate.max_output_tokens or 1),
                provider_options={
                    **options,
                    "_max_input_tokens": int(candidate.max_input_tokens or 1),
                    "_max_requests_per_turn": max_requests,
                },
            )
            if max(1, len((prompt or "").encode("utf-8"))) > int(candidate.max_input_tokens or 1):
                saw_prompt_limit = True
                continue
            quota_ids = sorted({str(value) for value in (candidate.quota_pool_ids or [])})
            if not quota_ids or any(quota_id not in quotas for quota_id in quota_ids):
                saw_missing_quota = True
                continue
            available: dict[str, str] = {}
            can_reserve = True
            for quota_id in quota_ids:
                quota = quotas[quota_id]
                if (
                    candidate.credential_profile is not None
                    and _uses_shared_openai_key(
                        candidate, candidate.credential_profile
                    )
                ):
                    quota_available = await _read_only_openai_quota_available(
                        session, quota, config, now_value
                    )
                elif quota.id == GEMINI_PROMO_MONTHLY_QUOTA_ID:
                    try:
                        quota_available = (
                            await _gemini_promo_quota_snapshot(
                                session, quota, now_value
                            )
                        )["available"]
                    except FreeTeamUnavailableError:
                        quota_available = None
                else:
                    quota_available = _read_only_quota_available(
                        quota,
                        now_value,
                        config_get(config, "openai.usage_tier", None),
                        config,
                    )
                if quota_available is None:
                    can_reserve = False
                    saw_missing_quota = True
                    break
                amount = _amount_for_metric(quota.metric_type, estimates)
                available[quota_id] = "provider_managed" if quota.provider_managed_cap else str(quota_available)
                if not quota.provider_managed_cap and (quota_available <= 0 or amount > quota_available):
                    can_reserve = False
                    saw_quota_block = True
                    break
            if not can_reserve:
                continue
            return FreeTeamApiTaskPreflight(
                True,
                "eligible",
                candidate_id=str(candidate.id),
                provider=str(candidate.provider),
                model=str(candidate.model),
                quota_ids=tuple(quota_ids),
                available_quota=available,
            )

        if saw_quota_block:
            reason = "quota_unavailable"
        elif saw_cooldown:
            reason = "candidate_cooldown"
        elif saw_concurrency:
            reason = "candidate_concurrency"
        elif saw_prompt_limit:
            reason = "prompt_exceeds_candidate_limit"
        elif saw_missing_quota:
            reason = "quota_unavailable"
        else:
            reason = "no_strict_free_candidate"
        return FreeTeamApiTaskPreflight(False, reason)
    except Exception:
        # A scheduler denial must never turn an observer/DB shape problem into
        # a paid attempt.  Keep the reason fixed and secret-free.
        return FreeTeamApiTaskPreflight(False, "preflight_unavailable")
    finally:
        if own_session:
            await session.close()


def _reset_quota_if_needed(quota: FreeTeamQuotaPool, now: datetime) -> None:
    now = _utc_naive(now)
    if not quota.window_end or quota.window_end > now:
        return
    policy = dict(quota.reset_policy or {})
    quota.consumed = Decimal("0")
    # In-flight requests can finish in either UTC day. Carry their full hold
    # into the new window; an old finalizer must never subtract a new hold.
    quota.provider_observed_usage = Decimal("0")
    provider_state = dict(quota.provider_sync_state or {})
    if not (
        quota.id in OPENAI_DAILY_QUOTA_GROUPS
        and provider_state.get("source") == "openai_usage_api"
    ):
        quota.provider_sync_state = None
    quota.provider_sync_version = int(quota.provider_sync_version or 0) + 1
    quota.last_provider_sync_at = None
    quota.window_start = now
    quota.window_end = _next_window_end(policy, now)
    if quota.status == "exhausted":
        quota.status = "active"


def _quota_reset_wait(
    quota: FreeTeamQuotaPool, now: datetime
) -> tuple[datetime, str] | None:
    """Return a future UTC request-limit reset, when waiting can help."""

    policy = dict(quota.reset_policy or {})
    kind = str(policy.get("kind") or "").lower()
    metric = str(quota.metric_type or "").lower()
    reset_at = quota.window_end
    if (
        metric not in {"requests", "rpm", "rpd", "tpm", "total_tokens"}
        or kind not in {"minute", "daily"}
        or str(policy.get("timezone") or "UTC").upper() != "UTC"
        or quota.status not in {"active", "exhausted"}
        or not isinstance(reset_at, datetime)
    ):
        return None
    if metric == "total_tokens" and kind != "daily":
        return None
    if metric == "tpm" and kind != "minute":
        return None
    now = _utc_naive(now)
    reset_at = _utc_naive(reset_at)
    if reset_at <= now:
        return None
    return reset_at, kind


def _next_window_end(
    reset_policy: Mapping[str, Any], now: datetime
) -> datetime | None:
    now = _utc_naive(now)
    kind = str(reset_policy.get("kind") or "none")
    if str(reset_policy.get("timezone") or "UTC").upper() != "UTC":
        raise FreeTeamUnavailableError("quota_reset_timezone_unsupported")
    if kind == "minute":
        return now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if kind == "daily":
        hour = max(0, min(23, int(reset_policy.get("hour") or 0)))
        boundary = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        return boundary if boundary > now else boundary + timedelta(days=1)
    if kind == "monthly":
        day = max(1, int(reset_policy.get("day") or 1))
        hour = max(0, min(23, int(reset_policy.get("hour") or 0)))
        current_day = min(day, monthrange(now.year, now.month)[1])
        boundary = datetime(now.year, now.month, current_day, hour)
        if boundary > now:
            return boundary
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        next_day = min(day, monthrange(year, month)[1])
        return datetime(year, month, next_day, hour)
    return None


def _initial_window(
    reset_policy: Mapping[str, Any], now: datetime
) -> tuple[datetime | None, datetime | None]:
    now = _utc_naive(now)
    kind = str(reset_policy.get("kind") or "none")
    if kind in {"minute", "daily", "monthly"}:
        return now, _next_window_end(reset_policy, now)
    return None, None


async def seed_free_team_defaults(session: AsyncSession) -> None:
    """不足している初期レコードだけを追加し、ユーザー設定は上書きしない。"""

    template = free_team_profile_template()
    now = _utcnow()
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        # 初回のAPI表示とルーティング開始が競合してもPK重複にしない。
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('aoitalk-free-team-seed'))")
        )
    existing_credentials = set(
        (
            await session.execute(select(FreeTeamCredentialProfile.id))
        ).scalars().all()
    )
    for profile_id, raw in (template.get("credential_profiles") or {}).items():
        if profile_id in existing_credentials:
            continue
        session.add(
            FreeTeamCredentialProfile(
                id=profile_id,
                display_name=str(raw.get("display_name") or profile_id),
                provider=str(raw.get("provider") or ""),
                authentication_type=str(raw.get("authentication_type") or "api_key"),
                cli_auth_reference=raw.get("cli_auth_reference"),
                environment_variable=raw.get("environment_variable"),
                base_url=raw.get("base_url"),
                enabled=bool(raw.get("enabled", True)),
                billing_mode=str(raw.get("billing_mode") or "free_tier"),
                privacy_class=str(raw.get("privacy_class") or "standard"),
                allow_paid_overage=False,
                status="ready",
            )
        )
    await session.flush()

    existing_quotas = set(
        (await session.execute(select(FreeTeamQuotaPool.id))).scalars().all()
    )
    for quota_id, raw in (template.get("quota_pools") or {}).items():
        if quota_id in existing_quotas:
            continue
        policy = dict(raw.get("reset_policy") or {})
        window_start, window_end = _initial_window(policy, now)
        session.add(
            FreeTeamQuotaPool(
                id=quota_id,
                credential_profile_id=str(raw.get("credential_profile_id") or ""),
                metric_type=str(raw.get("metric_type") or "requests"),
                limit_value=_decimal(raw.get("limit")),
                consumed=Decimal("0"),
                reserved=Decimal("0"),
                safety_margin_ratio=_decimal(raw.get("safety_margin_ratio")),
                safety_margin_units=_decimal(raw.get("safety_margin_units")),
                window_start=window_start,
                window_end=window_end,
                reset_policy=policy,
                provider_observed_usage=Decimal("0"),
                status="active",
            )
        )
    await session.flush()

    existing_candidates = set(
        (
            await session.execute(select(FreeTeamCandidateModel.id))
        ).scalars().all()
    )
    for candidate_id, raw in (template.get("candidates") or {}).items():
        if candidate_id in existing_candidates:
            continue
        session.add(
            FreeTeamCandidateModel(
                id=candidate_id,
                credential_profile_id=str(raw.get("credential_profile_id") or ""),
                provider=str(raw.get("provider") or ""),
                model=str(raw.get("model") or ""),
                effort=raw.get("effort"),
                priority=int(raw.get("priority") or 100),
                weight=max(1, int(raw.get("weight") or 1)),
                enabled=True,
                quota_pool_ids=list(raw.get("quota_pool_ids") or []),
                capabilities=list(raw.get("capabilities") or ["text"]),
                quality_class=str(raw.get("quality_class") or "standard"),
                max_input_tokens=max(1, int(raw.get("max_input_tokens") or 32768)),
                max_output_tokens=max(1, int(raw.get("max_output_tokens") or 2048)),
                timeout_seconds=max(1, int(raw.get("timeout") or 120)),
                max_retries=max(0, int(raw.get("max_retries") or 0)),
                cooldown_policy=dict(raw.get("cooldown_policy") or {}),
                tool_call_policy=dict(raw.get("tool_call_policy") or {}),
                privacy_class=str(raw.get("privacy_class") or "standard"),
                provider_options=dict(raw.get("provider_options") or {}),
                status="ready",
            )
        )
    await session.flush()


async def recover_credential_after_auth_update(
    session: AsyncSession,
    credential: FreeTeamCredentialProfile,
) -> None:
    """認証情報を更新したcredentialと認証失敗候補を再試行可能に戻す。"""

    credential.status = "ready"
    result = await session.execute(
        select(FreeTeamCandidateModel)
        .where(
            FreeTeamCandidateModel.credential_profile_id == credential.id,
            FreeTeamCandidateModel.status == "needs_attention",
        )
        .order_by(FreeTeamCandidateModel.id)
        .with_for_update()
    )
    for candidate in result.scalars().all():
        candidate.status = "ready"
        candidate.cooldown_until = None
        candidate.consecutive_failures = 0


def complimentary_model_group(model: str) -> str | None:
    """Use the canonical pricing catalog; never invent per-model allowances."""
    from .pricing.catalog import load_catalog_file

    for group, models in load_catalog_file().free_incentive_groups.items():
        if model in models:
            return group
    return None


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


async def _unmapped_central_usage(session: AsyncSession, group: str, now: datetime) -> bool:
    from ..models.ecc_models import TokenUsage
    now = _utc_naive(now)
    return bool((await session.execute(select(func.count()).select_from(TokenUsage).where(
        TokenUsage.provider == "openai", TokenUsage.free_incentive_group == group,
        (TokenUsage.billing_scope_id.is_(None) | TokenUsage.billing_scope_id.in_(["", "default"])),
        TokenUsage.total_tokens > 0,
        TokenUsage.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        TokenUsage.created_at < now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
    ))).scalar_one())


async def _central_unmetered_usage(session: AsyncSession, scope: str, group: str, now: datetime, *, included_through: datetime | None = None) -> Decimal:
    from ..models.ecc_models import TokenUsage
    now = _utc_naive(now)
    conditions = [
        TokenUsage.provider == "openai", TokenUsage.billing_scope_id == scope,
        TokenUsage.free_incentive_group == group, TokenUsage.quota_reservation_id.is_(None),
        TokenUsage.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        TokenUsage.created_at < now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
    ]
    if included_through is not None:
        conditions.append(TokenUsage.created_at <= _utc_naive(included_through))
    return _decimal((await session.execute(select(func.coalesce(func.sum(TokenUsage.total_tokens), 0)).where(*conditions))).scalar_one())


async def _observed_guard_overlap(session: AsyncSession, quota: FreeTeamQuotaPool, included_through: datetime) -> Decimal:
    start = included_through.replace(hour=0, minute=0, second=0, microsecond=0)
    reservations = (await session.execute(select(FreeTeamReservation).where(
        FreeTeamReservation.status == "committed", FreeTeamReservation.finalized_at >= start,
        FreeTeamReservation.finalized_at <= included_through,
    ))).scalars().all()
    overlap = Decimal(0)
    for item in reservations:
        estimate = item.estimated_usage or {}
        if quota.id not in (estimate.get("quota_reservations") or {}):
            continue
        try:
            acquired = _utc_naive(datetime.fromisoformat(estimate["window_start"]))
        except (KeyError, TypeError, ValueError):
            continue
        # Cross-day/legacy requests have no proven overlap with this day's
        # provider bucket. Keep their consumption in addition to observation.
        if start <= acquired <= included_through:
            actual = item.actual_usage or {}
            quota_actual = actual.get("quota_actual") if isinstance(actual, Mapping) else None
            amount = _decimal(
                (quota_actual or {}).get(quota.id)
                if isinstance(quota_actual, Mapping)
                else actual.get(
                    {"tpm": "total_tokens", "rpm": "requests", "rpd": "requests"}.get(
                        str(quota.metric_type).lower(), quota.metric_type
                    )
                )
            )
            if amount.is_finite() and amount >= 0:
                overlap += amount
    return min(_decimal(quota.consumed), overlap)


async def sync_provider_observation(session: AsyncSession, quota: FreeTeamQuotaPool, value: Any, *, config: Any, now: datetime | None = None) -> None:
    """Keep a fixed, nondecreasing external adjustment within one UTC day.

    Numeric legacy observations have no coverage proof and are counted fully.
    An object may declare ``included_through``: the provider-confirmed coverage
    cut, never the time the operator fetched a lagging snapshot.
    """
    now = _utc_naive(now or _utcnow())
    raw = value.get("usage") if isinstance(value, Mapping) else value
    try:
        observed = Decimal(str(raw))
        if not observed.is_finite() or observed < 0:
            raise ValueError
    except (ValueError, ArithmeticError):
        raise FreeTeamUnavailableError("provider_observation_invalid") from None
    included_through = None
    if isinstance(value, Mapping) and value.get("included_through") is not None:
        try:
            cut = datetime.fromisoformat(str(value["included_through"]).replace("Z", "+00:00"))
            if cut.tzinfo is None:
                raise ValueError
            included_through = _utc_naive(cut)
        except (TypeError, ValueError):
            raise FreeTeamUnavailableError("provider_observation_invalid") from None
        if included_through.date() != now.date() or included_through > now:
            raise FreeTeamUnavailableError("provider_observation_day_mismatch")
    openrouter_sync: dict[str, Any] = {}
    if isinstance(value, Mapping) and value.get("source") == "openrouter_key_api":
        try:
            provider_limit = Decimal(str(value.get("limit")))
        except (ValueError, TypeError, ArithmeticError):
            raise FreeTeamUnavailableError("provider_observation_invalid") from None
        credential_binding = str(value.get("credential_binding") or "")
        if (
            quota.id != "openrouter-free-rpd"
            or str(quota.metric_type).lower() != "rpd"
            or not provider_limit.is_finite()
            or provider_limit < 1
            or provider_limit != provider_limit.to_integral_value()
            or not re.fullmatch(r"[0-9a-f]{64}", credential_binding)
        ):
            raise FreeTeamUnavailableError("provider_observation_invalid")
        openrouter_sync = {
            "source": "openrouter_key_api",
            "provider_limit": str(provider_limit),
            "credential_binding": credential_binding,
        }
    _reset_quota_if_needed(quota, now)
    scope = str(config_get(config, "openai.billing_scope_id", "") or "")
    policy = dict(quota.reset_policy or {})
    if quota.id in OPENAI_DAILY_QUOTA_GROUPS and scope:
        if policy.get("billing_scope_id") not in {None, scope}:
            raise FreeTeamUnavailableError("billing_scope_mismatch")
        if policy.get("billing_scope_id") is None:
            if quota.consumed or quota.reserved or quota.provider_observed_usage:
                raise FreeTeamUnavailableError("billing_scope_unbound_usage")
            quota.reset_policy = {**policy, "billing_scope_id": scope}
    previous = _decimal(quota.provider_observed_usage)
    state = dict(quota.provider_sync_state or {})
    openrouter_same_identity_day = False
    openrouter_same_binding_day = False
    if openrouter_sync:
        previous_cut = None
        try:
            previous_cut = datetime.fromisoformat(str(state.get("included_through") or ""))
        except (TypeError, ValueError):
            pass
        openrouter_same_identity_day = (
            state.get("source") == "openrouter_key_api"
            and state.get("credential_binding")
            == openrouter_sync["credential_binding"]
            and state.get("utc_day") == now.date().isoformat()
        )
        openrouter_same_binding_day = openrouter_same_identity_day
        if openrouter_same_binding_day:
            if (
                included_through is not None
                and previous_cut is not None
                and included_through <= _utc_naive(previous_cut)
            ):
                return
            try:
                previous_total = Decimal(str(state.get("observed_total")))
            except (TypeError, ValueError, ArithmeticError):
                openrouter_same_binding_day = False
            else:
                if not previous_total.is_finite() or previous_total < 0:
                    openrouter_same_binding_day = False
                elif observed < previous_total:
                    # Provider daily usage is monotonic. A newer but lower
                    # same-binding snapshot cannot erase already counted use.
                    return
                elif previous_total != previous:
                    openrouter_same_binding_day = False
            if openrouter_same_binding_day:
                try:
                    previous_guard_overlap = Decimal(
                        str(state.get("guard_watermark"))
                    )
                    previous_external = Decimal(str(state.get("external_delta")))
                except (TypeError, ValueError, ArithmeticError):
                    openrouter_same_binding_day = False
                else:
                    if (
                        not previous_guard_overlap.is_finite()
                        or previous_guard_overlap < 0
                        or not previous_external.is_finite()
                        or previous_external < 0
                        or included_through is not None
                        and previous_cut is None
                    ):
                        openrouter_same_binding_day = False
    elif observed <= previous and (state or previous > 0):
        return  # Duplicate/out-of-order snapshots can never erase a hold.
    guard_overlap = central_overlap = Decimal(0)
    if included_through is not None and (
        quota.id in OPENAI_DAILY_QUOTA_GROUPS or quota.id == "openrouter-free-rpd"
    ):
        if quota.id in OPENAI_DAILY_QUOTA_GROUPS and (
            not scope or (quota.reset_policy or {}).get("billing_scope_id") != scope
        ):
            raise FreeTeamUnavailableError("billing_scope_unverified")
        guard_overlap = await _observed_guard_overlap(session, quota, included_through)
        if quota.id in OPENAI_DAILY_QUOTA_GROUPS:
            central_overlap = await _central_unmetered_usage(session, scope, OPENAI_DAILY_QUOTA_GROUPS[quota.id], now,
                included_through=included_through)
    if openrouter_sync:
        if openrouter_same_binding_day:
            external = max(
                Decimal(0),
                previous_external
                + (observed - previous_total)
                - (guard_overlap - previous_guard_overlap),
            )
        elif openrouter_same_identity_day:
            # Without a verifiable prior watermark, retain the old hold rather
            # than subtracting a local overlap we cannot safely reconcile.
            external = max(
                quota.provider_sync_delta,
                observed - guard_overlap,
                Decimal(0),
            )
        else:
            # A new binding/day starts a fresh provider counter. Do not carry
            # forward an external delta from another credential revision/day.
            external = max(observed - guard_overlap, Decimal(0))
    else:
        external = max(
            quota.provider_sync_delta,
            observed - guard_overlap - central_overlap,
            Decimal(0),
        )
    next_state = {"schema_version": 1, "observed_total": str(observed), "external_delta": str(external),
        "guard_watermark": str(guard_overlap), "central_watermark": str(central_overlap),
        "included_through": included_through.isoformat() if included_through else None,
        "utc_day": now.date().isoformat(), **openrouter_sync}
    version = int(quota.provider_sync_version or 0)
    await session.flush()
    result = await session.execute(update(FreeTeamQuotaPool).where(
        FreeTeamQuotaPool.id == quota.id, FreeTeamQuotaPool.provider_sync_version == version,
        FreeTeamQuotaPool.consumed == _decimal(quota.consumed), FreeTeamQuotaPool.reserved == _decimal(quota.reserved),
    ).values(provider_observed_usage=observed, provider_sync_state=next_state, last_provider_sync_at=now,
        provider_sync_version=FreeTeamQuotaPool.provider_sync_version + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise FreeTeamUnavailableError("provider_observation_conflict")
    from sqlalchemy.orm.attributes import set_committed_value
    for key, new_value in {"provider_observed_usage": observed, "provider_sync_state": next_state,
        "last_provider_sync_at": now, "provider_sync_version": version + 1}.items():
        set_committed_value(quota, key, new_value)


def _openai_effective_cap(quota: FreeTeamQuotaPool, config: Any) -> Decimal:
    return quota.daily_cap_for_tier(
        config_get(config, "openai.usage_tier", None)
    )


def _openai_usage_actual_info(
    state: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """Return only a current-day, recent Usage API projection."""
    from ..security.secret_env import openai_admin_key_configured

    current = _utc_naive(now)
    day = current.date().isoformat()
    source = state.get("source")
    usage_api_attempt = state.get("usage_api_attempt")
    usage_api_attempt = usage_api_attempt if isinstance(usage_api_attempt, Mapping) else {}
    last_success = state.get("last_success_at") if source == "openai_usage_api" else None
    last_success_at = None
    if isinstance(last_success, str):
        try:
            last_success_at = _utc_naive(
                datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            )
        except (TypeError, ValueError, OverflowError):
            last_success_at = None
    last_error = usage_api_attempt.get("error_code")
    usage_day = state.get("utc_day") if source == "openai_usage_api" else None
    usage = state.get("actual_usage") if source == "openai_usage_api" else None
    valid_usage = isinstance(usage, Mapping) and usage_day == day
    normalized_usage: dict[str, int] | None = None
    if valid_usage:
        normalized_usage = {}
        for field in (
            "input_tokens",
            "output_tokens",
            "input_cached_tokens",
            "total_tokens",
            "requests",
        ):
            item = usage.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 2**53 - 1:
                valid_usage = False
                normalized_usage = None
                break
            normalized_usage[field] = item
        if normalized_usage and (
            normalized_usage["total_tokens"] != normalized_usage["input_tokens"] + normalized_usage["output_tokens"]
            or normalized_usage["input_cached_tokens"] > normalized_usage["input_tokens"]
        ):
            valid_usage = False
            normalized_usage = None
    fresh = bool(
        valid_usage
        and last_success_at is not None
        and 0 <= (current - last_success_at).total_seconds() < OPENAI_USAGE_STALE_AFTER_SECONDS
        and not last_error
    )
    key_configured = openai_admin_key_configured()
    if fresh:
        status = "ok"
    elif not key_configured:
        status = "not_configured"
    elif last_error:
        status = f"error:{_safe_openai_usage_error(last_error)}"
    else:
        status = "stale"
    return {
        "fresh": fresh,
        "status": status,
        "source": "openai_usage_api" if fresh else "ledger",
        "usage": normalized_usage if valid_usage else None,
        "last_synced_at": (
            last_success
            if isinstance(last_success, str) and len(last_success) <= 64 and last_success_at is not None
            else None
        ),
        "has_success": last_success_at is not None,
        # Do not change the legacy ledger-only behavior before the first
        # successful organization observation. Once we have seen an actual
        # total, a missing/stale observation must reserve safety headroom.
        "apply_unavailable_margin": (not fresh) and last_success_at is not None,
        "unknown_models": (
            _safe_openai_unknown_models(state.get("unknown_models"))
            if source == "openai_usage_api" and usage_day == day
            else []
        ),
    }


def _safe_openai_usage_error(value: Any) -> str:
    text = str(value or "")
    if text in {
        "not_configured",
        "unauthorized",
        "timeout",
        "http_5xx",
        "parse_error",
        "network_error",
        "redirect_rejected",
        "pool_unavailable",
        "reservation_conflict",
    } or re.fullmatch(r"http_[1-5][0-9]{2}", text):
        return text
    return "network_error"


def _safe_openai_unknown_models(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in value[:50]:
        if not isinstance(item, Mapping):
            continue
        model = item.get("model")
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", model
        ):
            continue
        counts: dict[str, int] = {}
        valid = True
        for field in (
            "input_tokens",
            "output_tokens",
            "input_cached_tokens",
            "total_tokens",
            "requests",
        ):
            count = item.get(field)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count > 2**53 - 1
            ):
                valid = False
                break
            counts[field] = count
        if (
            valid
            and counts["total_tokens"] == counts["input_tokens"] + counts["output_tokens"]
            and counts["input_cached_tokens"] <= counts["input_tokens"]
        ):
            sanitized.append({"model": model, **counts})
    return sanitized


def _openai_quota_balance(
    quota: FreeTeamQuotaPool,
    config: Any,
    *,
    central_usage: Decimal = Decimal("0"),
    now: datetime | None = None,
    local_consumed: Decimal | None = None,
    reserved: Decimal | None = None,
    provider_observed: Decimal | None = None,
    provider_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = _utc_naive(now or _utcnow())
    local = _decimal(quota.consumed if local_consumed is None else local_consumed)
    central = _decimal(central_usage)
    held = _decimal(quota.reserved if reserved is None else reserved)
    observed = _decimal(
        quota.provider_observed_usage if provider_observed is None else provider_observed
    )
    state = dict(provider_state if provider_state is not None else (quota.provider_sync_state or {}))
    if state.get("source") == "openai_usage_api":
        legacy_external = Decimal("0")
    else:
        legacy_external = observed
        try:
            external_delta = Decimal(str(state["external_delta"]))
            observed_total = Decimal(str(state["observed_total"]))
            if (
                state.get("schema_version") == 1
                and observed_total == observed
                and external_delta.is_finite()
                and external_delta >= 0
            ):
                legacy_external = external_delta
        except (KeyError, ArithmeticError, TypeError, ValueError):
            pass

    cap = _openai_effective_cap(quota, config)
    actual_info = _openai_usage_actual_info(state, current)
    ledger_base = local + central + legacy_external
    ledger_with_holds = ledger_base + held
    actual_group = actual_info.get("usage")
    actual_total = None
    if actual_info.get("fresh") and isinstance(actual_group, Mapping):
        try:
            actual_total = Decimal(str(actual_group.get("total_tokens")))
        except (TypeError, ValueError, ArithmeticError):
            actual_total = None
        if actual_total is None or not actual_total.is_finite() or actual_total < 0:
            actual_info = {
                **actual_info,
                "fresh": False,
                "status": "error:parse_error",
                "source": "ledger",
                "apply_unavailable_margin": True,
            }
            actual_total = None

    if actual_info.get("fresh") and actual_total is not None:
        used_with_holds = max(actual_total, ledger_with_holds)
        consumed = max(actual_total, ledger_base)
        external_estimate = max(Decimal(0), actual_total - local - central)
        external_delta = max(Decimal(0), consumed - local - central)
        unavailable_margin = Decimal(0)
    else:
        margin_ratio = config_get(
            config, "openai.usage_api.unavailable_margin_ratio", 0.15
        )
        try:
            ratio = Decimal(str(margin_ratio))
            if not ratio.is_finite() or ratio < 0 or ratio > 1:
                ratio = Decimal("0.15")
        except (TypeError, ValueError, ArithmeticError):
            ratio = Decimal("0.15")
        unavailable_margin = cap * ratio if actual_info.get("apply_unavailable_margin") else Decimal(0)
        used_with_holds = ledger_with_holds + unavailable_margin
        consumed = ledger_base
        external_delta = legacy_external
        external_estimate = None

    return {
        "cap": cap,
        "local_consumed": local,
        "central_usage": central,
        "provider_sync_delta": external_delta,
        "consumed": consumed,
        "reserved": held,
        "available": max(Decimal(0), cap - used_with_holds),
        "used_for_admission": used_with_holds,
        "unavailable_margin": unavailable_margin,
        "actual_usage": actual_total if actual_info.get("fresh") else None,
        "actual_usage_last_observed": actual_group.get("total_tokens") if isinstance(actual_group, Mapping) else None,
        "actual_usage_state": actual_info["status"],
        "actual_usage_source": actual_info["source"],
        "actual_usage_last_synced_at": actual_info["last_synced_at"],
        "external_usage_estimate": external_estimate,
        "actual_usage_group": actual_group,
        "unknown_models": actual_info.get("unknown_models") or [],
    }


def _openai_quota_snapshot(
    quota: FreeTeamQuotaPool,
    config: Any,
    *,
    central_usage: Decimal = Decimal("0"),
) -> dict[str, Any]:
    """Share one balance definition between status, preflight and admission."""

    return _openai_quota_balance(
        quota,
        config,
        central_usage=central_usage,
    )


def openai_usage_actual_health_status() -> dict[str, Any]:
    """A tiny health projection that never opens the database or returns secrets."""
    from ..security.secret_env import openai_admin_key_configured

    with _OPENAI_USAGE_HEALTH_LOCK:
        cached = dict(_OPENAI_USAGE_HEALTH_CACHE or {})
    if not cached:
        return {
            "status": "stale" if openai_admin_key_configured() else "not_configured",
            "last_synced_at": None,
        }
    synced_at = cached.get("last_synced_at")
    if cached.get("status") == "ok" and isinstance(synced_at, str):
        try:
            last_sync = _utc_naive(datetime.fromisoformat(synced_at.replace("Z", "+00:00")))
            age = (_utc_naive(_utcnow()) - last_sync).total_seconds()
            if age < 0 or age >= OPENAI_USAGE_STALE_AFTER_SECONDS:
                cached["status"] = "stale"
        except (TypeError, ValueError, OverflowError):
            cached["status"] = "stale"
    return cached


def _set_openai_usage_health(status: str, synced_at: str | None = None) -> None:
    global _OPENAI_USAGE_HEALTH_CACHE
    if status not in {
        "ok",
        "not_configured",
        "stale",
        "error:not_configured",
        "error:unauthorized",
        "error:timeout",
        "error:http_5xx",
        "error:parse_error",
        "error:network_error",
        "error:redirect_rejected",
        "error:reservation_conflict",
    } and not re.fullmatch(r"error:http_[1-5][0-9]{2}", status):
        status = "error:network_error"
    with _OPENAI_USAGE_HEALTH_LOCK:
        _OPENAI_USAGE_HEALTH_CACHE = {
            "status": status,
            "last_synced_at": synced_at if isinstance(synced_at, str) and len(synced_at) <= 64 else None,
        }


def _openai_usage_timestamp(state: Mapping[str, Any]) -> datetime | None:
    attempt = state.get("usage_api_attempt")
    attempt = attempt if isinstance(attempt, Mapping) else {}
    value = attempt.get("last_attempt_at") or state.get("last_success_at")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return _utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_openai_usage_snapshot(value: Any, *, utc_day: str) -> dict[str, Any]:
    from .openai_usage_observer import OpenAIUsageObservationError

    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping) or value.get("utc_day") != utc_day:
        raise OpenAIUsageObservationError("parse_error")
    raw_groups = value.get("groups")
    if not isinstance(raw_groups, Mapping):
        raise OpenAIUsageObservationError("parse_error")
    groups: dict[str, dict[str, int]] = {}
    for group in ("1m", "10m"):
        raw = raw_groups.get(group)
        if not isinstance(raw, Mapping):
            raise OpenAIUsageObservationError("parse_error")
        normalized = {}
        for field in (
            "input_tokens",
            "output_tokens",
            "input_cached_tokens",
            "total_tokens",
            "requests",
        ):
            item = raw.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 2**53 - 1:
                raise OpenAIUsageObservationError("parse_error")
            normalized[field] = item
        if (
            normalized["total_tokens"]
            != normalized["input_tokens"] + normalized["output_tokens"]
            or normalized["input_cached_tokens"] > normalized["input_tokens"]
        ):
            raise OpenAIUsageObservationError("parse_error")
        groups[group] = normalized

    unknown_models = value.get("unknown_models", [])
    if not isinstance(unknown_models, list) or len(unknown_models) > 100:
        raise OpenAIUsageObservationError("parse_error")
    safe_unknown = []
    for item in unknown_models:
        if not isinstance(item, Mapping):
            raise OpenAIUsageObservationError("parse_error")
        model = item.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", model):
            raise OpenAIUsageObservationError("parse_error")
        safe_counts = {}
        for field in ("input_tokens", "output_tokens", "input_cached_tokens", "total_tokens", "requests"):
            count = item.get(field)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 2**53 - 1:
                raise OpenAIUsageObservationError("parse_error")
            safe_counts[field] = count
        if safe_counts["total_tokens"] != safe_counts["input_tokens"] + safe_counts["output_tokens"]:
            raise OpenAIUsageObservationError("parse_error")
        safe_unknown.append({"model": model, **safe_counts})
    pages = value.get("pages", 1)
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 100:
        raise OpenAIUsageObservationError("parse_error")
    return {"utc_day": utc_day, "groups": groups, "unknown_models": safe_unknown, "pages": pages}


async def _persist_openai_usage_attempt(
    *,
    now: datetime,
    snapshot: Mapping[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    """Persist two existing quota rows with optimistic version checks only."""
    if (snapshot is None) == (error_code is None):
        raise ValueError("openai_usage_persistence_input_invalid")
    session = await get_db_session()
    try:
        async with session.begin():
            rows = (await session.execute(
                select(FreeTeamQuotaPool)
                .where(FreeTeamQuotaPool.id.in_(OPENAI_DAILY_QUOTA_GROUPS))
                .order_by(FreeTeamQuotaPool.id)
                .with_for_update()
            )).scalars().all()
            by_group = {
                OPENAI_DAILY_QUOTA_GROUPS[row.id]: row
                for row in rows
                if row.id in OPENAI_DAILY_QUOTA_GROUPS
            }
            if set(by_group) != {"1m", "10m"}:
                raise FreeTeamUnavailableError("openai_usage_pool_unavailable")
            stamp = now.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            for group, quota in by_group.items():
                state = dict(quota.provider_sync_state or {})
                version = int(quota.provider_sync_version or 0)
                update_values: dict[str, Any] = {
                    "provider_sync_version": FreeTeamQuotaPool.provider_sync_version + 1,
                }
                if snapshot is not None:
                    group_data = dict(snapshot["groups"][group])
                    previous_actual = state.get("actual_usage")
                    if state.get("source") == "openai_usage_api" and state.get("utc_day") == snapshot["utc_day"]:
                        if isinstance(previous_actual, Mapping) and int(previous_actual.get("total_tokens", 0)) > group_data["total_tokens"]:
                            raise FreeTeamUnavailableError("openai_usage_observation_rewound")
                    next_state = {
                        "schema_version": 2,
                        "source": "openai_usage_api",
                        "utc_day": snapshot["utc_day"],
                        "last_success_at": stamp,
                        "actual_usage": group_data,
                        "unknown_models": list(snapshot.get("unknown_models") or []),
                        "pages": int(snapshot.get("pages", 1)),
                        "usage_api_attempt": {"last_attempt_at": stamp, "error_code": None},
                    }
                    update_values.update(
                        provider_observed_usage=Decimal(group_data["total_tokens"]),
                        provider_sync_state=next_state,
                        last_provider_sync_at=now,
                    )
                else:
                    attempt = {
                        "last_attempt_at": stamp,
                        "error_code": _safe_openai_usage_error(error_code),
                    }
                    next_state = {**state, "usage_api_attempt": attempt}
                    update_values["provider_sync_state"] = next_state

                await session.flush()
                result = await session.execute(
                    update(FreeTeamQuotaPool)
                    .where(
                        FreeTeamQuotaPool.id == quota.id,
                        FreeTeamQuotaPool.provider_sync_version == version,
                        FreeTeamQuotaPool.consumed == _decimal(quota.consumed),
                        FreeTeamQuotaPool.reserved == _decimal(quota.reserved),
                    )
                    .values(**update_values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise FreeTeamUnavailableError("openai_usage_reservation_conflict")
                from sqlalchemy.orm.attributes import set_committed_value

                for field, new_value in update_values.items():
                    if field == "provider_sync_version":
                        new_value = version + 1
                    set_committed_value(quota, field, new_value)
    finally:
        await session.close()


async def refresh_openai_usage_actual_if_due(
    *,
    config: Any,
    now: datetime | None = None,
    trigger: str = "periodic",
) -> dict[str, Any]:
    """Single-flight periodic/pre-reservation Usage API refresh; all errors are safe codes."""
    from ..security.secret_env import _openai_admin_key_for_usage_api
    from .openai_usage_observer import (
        OpenAIUsageObservationError,
        fetch_openai_usage,
        utc_day_start,
    )

    current = _utc_naive(now or _utcnow())
    key = _openai_admin_key_for_usage_api()
    if not str(key or "").strip():
        _set_openai_usage_health("not_configured")
        return {"status": "not_configured", "failure": "not_configured"}

    loop = asyncio.get_running_loop()
    with _OPENAI_USAGE_REFRESH_LOCKS_GUARD:
        lock = _OPENAI_USAGE_REFRESH_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _OPENAI_USAGE_REFRESH_LOCKS[loop] = lock
    async with lock:
        session = await get_db_session()
        try:
            rows = (await session.execute(
                select(FreeTeamQuotaPool)
                .where(FreeTeamQuotaPool.id.in_(OPENAI_DAILY_QUOTA_GROUPS))
                .order_by(FreeTeamQuotaPool.id)
            )).scalars().all()
            if {row.id for row in rows} != set(OPENAI_DAILY_QUOTA_GROUPS):
                await session.rollback()
                _set_openai_usage_health("stale")
                return {"status": "failed", "failure": "pool_unavailable"}
            previous_states = {
                row.id: dict(row.provider_sync_state or {}) for row in rows
            }
            attempt_times = [
                stamp
                for state in previous_states.values()
                if (stamp := _openai_usage_timestamp(state)) is not None
            ]
            last_attempt = max(attempt_times) if attempt_times else None
            refresh_raw = config_get(config, "openai.usage_api.refresh_seconds", 180)
            try:
                refresh_seconds = int(refresh_raw)
                if isinstance(refresh_raw, bool) or refresh_seconds < 60:
                    refresh_seconds = 180
                refresh_seconds = min(refresh_seconds, 3600)
            except (TypeError, ValueError, OverflowError):
                refresh_seconds = 180
            minimum_age = 60 if trigger in {"reservation", "preflight"} else refresh_seconds
            if last_attempt is not None and 0 <= (current - last_attempt).total_seconds() < minimum_age:
                await session.rollback()
                return {"status": "not_due", "failure": ""}
            await session.rollback()

            try:
                value = fetch_openai_usage(str(key), now=current)
                snapshot_value = await value if inspect.isawaitable(value) else value
                snapshot = _normalize_openai_usage_snapshot(
                    snapshot_value,
                    utc_day=utc_day_start(current)[0],
                )
                # Last success wins for each UTC window; an older cached total
                # is never allowed to reduce a same-day observation.
                for quota_id, state in previous_states.items():
                    if state.get("source") != "openai_usage_api" or state.get("utc_day") != snapshot["utc_day"]:
                        continue
                    prior = state.get("actual_usage")
                    group = OPENAI_DAILY_QUOTA_GROUPS[quota_id]
                    if isinstance(prior, Mapping) and int(prior.get("total_tokens", 0)) > snapshot["groups"][group]["total_tokens"]:
                        raise OpenAIUsageObservationError("parse_error")
                await _persist_openai_usage_attempt(
                    now=current,
                    snapshot=snapshot,
                )
                stamp = current.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                _set_openai_usage_health("ok", stamp)
                return {"status": "observed", "failure": "", "last_synced_at": stamp}
            except OpenAIUsageObservationError as exc:
                code = _safe_openai_usage_error(exc)
            except FreeTeamUnavailableError as exc:
                code = _safe_openai_usage_error(exc.code)
            except Exception:
                code = "network_error"

            try:
                await _persist_openai_usage_attempt(
                    now=current,
                    error_code=code,
                )
            except Exception:
                code = "reservation_conflict"
            status = f"error:{code}"
            _set_openai_usage_health(status)
            return {"status": "failed", "failure": code}
        finally:
            await session.close()


def openai_credential_identity(client: Any, config: Any = None) -> dict[str, str]:
    """Bind evidence to the actual SDK credential without exposing it."""
    key = getattr(client, "api_key", None)
    return {
        "api_key_sha256": hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) and key else "",
        "organization_id": str(getattr(client, "organization", "") or ""),
        "project_id": str(getattr(client, "project", "") or ""),
        "base_url": str(getattr(client, "base_url", "https://api.openai.com/v1/") or ""),
    }


async def require_cli_provider_enabled(provider: str, *, session: AsyncSession | None = None) -> None:
    """Use the existing credential switch for explicit Team CLI routes too."""
    if provider not in CLI_PROVIDERS:
        return
    own_session = session is None
    if own_session:
        from .token_tracking_service import _database_ready_for_usage
        if not _database_ready_for_usage():
            # Standalone CLI embedders have no persisted settings ledger.
            # Do not initialize/migrate one merely to run their existing CLI.
            return
    session = session or await get_db_session()
    try:
        credentials = (await session.execute(select(FreeTeamCredentialProfile).where(
            FreeTeamCredentialProfile.provider == provider
        ))).scalars().all()
        if credentials and not any(item.enabled for item in credentials):
            raise FreeTeamUnavailableError("provider_disabled")
    finally:
        if own_session:
            await session.close()


def complimentary_stop_reason(config: Any, identity: Mapping[str, str] | None = None) -> str:
    """Read administrative evidence only; this never opts into data sharing."""
    evidence = config_get(config, "openai.complimentary_verification", {})
    if not isinstance(evidence, dict) or evidence.get("eligible") is not True or evidence.get("enrolled") is not True:
        return "complimentary_unverified"
    if not config_get(config, "openai.data_sharing_incentive_enabled", False):
        return "complimentary_not_enabled"
    scope = str(config_get(config, "openai.billing_scope_id", "") or "")
    if not scope or evidence.get("organization_id") != scope:
        return "billing_scope_unverified"
    if config_get(config, "openai.usage_tier", "") not in {"tier_1_2", "tier_3_plus"}:
        return "complimentary_tier_unverified"
    if evidence.get("usage_tier") != config_get(config, "openai.usage_tier", ""):
        return "complimentary_tier_unverified"
    # The owner's existing eligibility/enrollment confirmation is sufficient.
    # Honor an explicitly supplied expiry without inventing a renewal deadline.
    if evidence.get("valid_until") is not None:
        try:
            valid_until = datetime.fromisoformat(str(evidence["valid_until"]).replace("Z", "+00:00"))
            if valid_until.tzinfo is None or _utc_naive(valid_until) <= _utc_naive(_utcnow()):
                return "complimentary_verification_expired"
        except (TypeError, ValueError):
            return "complimentary_verification_expired"
    if identity is not None:
        if identity.get("base_url", "").rstrip("/") != "https://api.openai.com/v1":
            return "complimentary_endpoint_unverified"
        bindings = evidence.get("credentials")
        if not isinstance(bindings, list) or not any(
            isinstance(item, dict)
            and item.get("credential_reference") == "openai_api_key"
            and item.get("api_key_sha256") == identity.get("api_key_sha256")
            and bool(identity.get("api_key_sha256"))
            and item.get("organization_id") == scope
            and item.get("project_enrolled") is True
            and item.get("sdk_project_id", "") == identity.get("project_id", "")
            and identity.get("organization_id", "") in {"", scope}
            for item in bindings
        ):
            return "credential_project_unverified"
    return ""


def _openai_token_count(serialized_payload: str, model: Any) -> int | None:
    """Return the known model tokenizer count, or ``None`` for a safe fallback."""

    try:
        import tiktoken

        encoding = tiktoken.encoding_for_model(str(model or ""))
        return len(encoding.encode(serialized_payload, disallowed_special=()))
    except Exception:
        return None


def _proven_openai_connect_failure(
    error: BaseException,
    *,
    sdk_max_retries: int | None,
    httpx_transport_retries: int | None,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return an audit proof only for one SDK attempt that never connected."""

    if (
        type(sdk_max_retries) is not int
        or sdk_max_retries != 0
        or type(httpx_transport_retries) is not int
        or httpx_transport_retries != 0
        or "max_retries" in payload
    ):
        return None
    try:
        import httpx
        from openai import APIConnectionError
    except Exception:
        return None
    if not isinstance(error, APIConnectionError):
        return None
    cause = error.__cause__
    # HTTPX ConnectError/ConnectTimeout are raised while establishing the
    # connection. Read/write/protocol/pool errors can happen after request
    # bytes have left this process and must retain the uncertain hold.
    if type(cause) not in {httpx.ConnectError, httpx.ConnectTimeout}:
        return None
    return {
        "exception_type": type(error).__name__,
        "cause_type": type(cause).__name__,
        "physical_attempts": 1,
        "sdk_max_retries": 0,
        "httpx_transport_retries": httpx_transport_retries,
        "request_sent": False,
    }


def _openai_input_token_bound(
    serialized_payload: str,
    *,
    model: Any,
    framing_item_count: int,
) -> int:
    """Bound one serialized request with the model tokenizer plus slack.

    ``tiktoken`` is already a project dependency. The per-model encoding is
    used when available; the 1.5 multiplier and 64-token framing allowance are
    deliberate safety margins. The UTF-8/3 floor is an additional
    language-neutral lower bound on the estimate. If the model has no known
    encoding, the old one-token-per-byte bound remains the fail-safe fallback.
    """

    wire_bytes = len(serialized_payload.encode("utf-8"))
    framing_tokens = 64 * (1 + max(0, int(framing_item_count)))
    token_count = _openai_token_count(serialized_payload, model)
    if token_count is None:
        return wire_bytes + framing_tokens

    tokenizer_bound = int(
        (Decimal(token_count) * OPENAI_INPUT_TOKEN_SAFETY_FACTOR).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    byte_floor = int(
        (Decimal(wire_bytes) / OPENAI_INPUT_TOKEN_BYTES_FLOOR_DIVISOR).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(tokenizer_bound, byte_floor) + framing_tokens


def request_budget_estimate(payload: Mapping[str, Any]) -> dict[str, Decimal]:
    """Bound every wire field, including instructions/history/tool schemas."""
    if payload.get("previous_response_id") or payload.get("conversation") or payload.get("stream"):
        raise FreeTeamUnavailableError("complimentary_unbounded_request")
    output_keys = [key for key in ("max_output_tokens", "max_completion_tokens", "max_tokens") if key in payload]
    if output_keys:
        # An explicit wire limit (caller/target setting) is the hard bound.
        output = payload[output_keys[0]]
    else:
        # No limit is injected on the wire.  Reserve the documented model
        # maximum instead, which already includes reasoning tokens.
        from ..llm.openai_model_context_registry import openai_model_context_spec
        spec = openai_model_context_spec(payload.get("model"))
        output = spec.max_output_tokens if spec is not None else None
    if type(output) is not int or output <= 0:
        raise FreeTeamUnavailableError("complimentary_output_limit_required")
    if payload.get("n", 1) != 1:
        raise FreeTeamUnavailableError("complimentary_unbounded_request")
    from ..llm.image_budget import bounded_image_text_tokens, sanitize_image_payload
    try:
        bounded_payload, image_tokens = sanitize_image_payload(payload, payload.get("model"))
    except ValueError as exc:
        raise ComplimentaryMediaUnboundedError() from exc
    # Media URLs/file IDs have remote token cost not bounded by their string
    # length. Only textual payloads and local function schemas are supported.
    def check(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") in {"image_url", "input_image", "input_audio", "input_file", "file", "audio", "video", "item_reference"}:
                raise ComplimentaryMediaUnboundedError()
            for item in value.values():
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check(item)
        elif value is not None and type(value) not in {str, int, float, bool}:
            raise FreeTeamUnavailableError("complimentary_payload_unsupported")
    check(bounded_payload)
    if payload.get("extra_headers") or payload.get("extra_query"):
        raise FreeTeamUnavailableError("complimentary_credential_override")
    extra_body = payload.get("extra_body") or {}
    if not isinstance(extra_body, dict) or set(extra_body) - {"prompt_cache_key", "prompt_cache_retention"}:
        raise FreeTeamUnavailableError("complimentary_wire_override")
    tools = payload.get("tools") or []
    if any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools):
        raise FreeTeamUnavailableError("complimentary_billable_tool")
    serialized_payload = json.dumps(bounded_payload, ensure_ascii=False, separators=(",", ":"))
    # Preserve the existing protocol-framing allowance. The output hard limit
    # already includes reasoning; cached inputs and reasoning are not added twice.
    framing_items = payload.get("messages") or payload.get("input")
    framing_item_count = (
        len(framing_items)
        if isinstance(framing_items, (list, tuple))
        else 1 if framing_items is not None else 0
    )
    # Responses API accepts ``input`` as one string as well as an item list.
    # A string is one framed input item; its character count is already part
    # of the tokenizer and UTF-8 byte estimate above.
    input_bound = image_tokens + (
        bounded_image_text_tokens(bounded_payload) if image_tokens else
        _openai_input_token_bound(
            serialized_payload,
            model=payload.get("model"),
            framing_item_count=framing_item_count,
        )
    )
    return {"input_tokens": Decimal(input_bound), "output_tokens": Decimal(output),
            "total_tokens": Decimal(input_bound + output), "requests": Decimal(1)}


async def acquire_openai_request_lease(
    payload: Mapping[str, Any], *, config: Any, model: str,
    identity: Mapping[str, str] | None, session: AsyncSession | None = None,
) -> RouteLease:
    """Reserve the existing org-wide pool immediately before one send."""
    from . import openai_confirmation_service as confirmation
    if payload.get("model") != model:
        raise FreeTeamUnavailableError("complimentary_model_mismatch")
    group = complimentary_model_group(model)
    if group is None:
        raise FreeTeamUnavailableError("complimentary_model_unverified")
    estimates = request_budget_estimate(payload)
    from ..llm.image_budget import (
        current_image_cost_limit, current_image_price, initial_image_tokens,
        quote_image_cost_micros, serialize_image_price_rule,
    )
    if initial_image_tokens() is not None:
        from .pricing import get_pricing_engine
        pricing = get_pricing_engine()
        await pricing.ensure_loaded()
        try:
            if serialize_image_price_rule(pricing.resolve("openai", model), model) != current_image_price():
                raise ValueError("image price changed")
        except ValueError as exc:
            raise ComplimentaryMediaUnboundedError() from exc
        cost_limit = current_image_cost_limit()
        if (cost_limit is not None and quote_image_cost_micros(current_image_price(),
                int(estimates["input_tokens"]), int(estimates["output_tokens"])) > cost_limit):
            raise ComplimentaryImageCostExceeded()
    quota_id = next(key for key, value in OPENAI_DAILY_QUOTA_GROUPS.items() if value == group)
    scope = str(config_get(config, "openai.billing_scope_id"))
    own_session = session is None
    if own_session:
        from .token_tracking_service import _database_ready_for_usage
        if not _database_ready_for_usage():
            raise FreeTeamUnavailableError("usage_ledger_not_ready")
    if session is None or not session.in_transaction():
        await refresh_openai_usage_actual_if_due(
            config=config,
            trigger="reservation",
        )
    session = session or await get_db_session()
    try:
        async with session.begin():
            try:
                current_config = await confirmation.authoritative_config(
                    session, actual_identity=dict(identity or {})
                )
            except confirmation.ConfirmationError as exc:
                raise FreeTeamUnavailableError(exc.code) from exc
            if confirmation.managed(current_config):
                config = current_config
            reason = complimentary_stop_reason(config, identity or {})
            if reason:
                raise FreeTeamUnavailableError(reason)
            scope = str(config_get(config, "openai.billing_scope_id"))
            await seed_free_team_defaults(session)
            # Same lock ordering as route selection: candidate -> quota.
            candidate = (await session.execute(select(FreeTeamCandidateModel).where(
                FreeTeamCandidateModel.id == quota_id.removesuffix("-daily")
            ).with_for_update())).scalar_one()
            if (
                not candidate.enabled
                or candidate.status not in {"ready", "active"}
                or candidate.provider != "openai"
                or candidate.model != model
            ):
                raise FreeTeamUnavailableError("provider_disabled")
            quota = (await session.execute(select(FreeTeamQuotaPool).where(
                FreeTeamQuotaPool.id == quota_id
            ).with_for_update())).scalar_one()
            if initial_image_tokens() is not None and str(quota.metric_type or "").casefold() != "total_tokens":
                raise ComplimentaryMediaUnboundedError()
            now = _utcnow()
            _reset_quota_if_needed(quota, now)
            policy = dict(quota.reset_policy or {})
            if policy.get("billing_scope_id") not in {None, scope}:
                raise FreeTeamUnavailableError("billing_scope_mismatch")
            if policy.get("billing_scope_id") is None and (quota.consumed or quota.reserved or quota.provider_observed_usage):
                raise FreeTeamUnavailableError("billing_scope_unbound_usage")
            if policy.get("kind") != "daily" or str(policy.get("timezone", "UTC")).upper() != "UTC" or policy.get("hour", 0) != 0:
                raise FreeTeamUnavailableError("complimentary_reset_policy_invalid")
            credential = await session.get(FreeTeamCredentialProfile, quota.credential_profile_id)
            if credential is None or not _uses_shared_openai_key(candidate, credential):
                raise FreeTeamUnavailableError("provider_disabled")
            # Import only here so ordinary routing fixtures need no usage DB.
            if await _unmapped_central_usage(session, group, now):
                raise FreeTeamUnavailableError("billing_scope_unmapped_usage")
            central = await _central_unmetered_usage(session, scope, group, now)
            # Local guard consumption and pre-existing/unmetered central usage
            # are disjoint. Observation's fixed external adjustment cannot
            # shrink as new local requests settle.
            balance = _openai_quota_snapshot(
                quota, config, central_usage=central
            )
            amount = estimates["total_tokens"]
            if quota.status != "active" or amount > balance["available"]:
                if initial_image_tokens() is not None:
                    raise ComplimentaryImageBudgetInsufficient()
                raise FreeTeamUnavailableError("budget_insufficient")
            quota.reset_policy = {**policy, "billing_scope_id": scope}
            previous_reserved = balance["reserved"]
            await session.flush()
            result = await session.execute(update(FreeTeamQuotaPool).where(
                FreeTeamQuotaPool.id == quota_id,
                FreeTeamQuotaPool.reserved == previous_reserved,
                FreeTeamQuotaPool.consumed == _decimal(quota.consumed),
                FreeTeamQuotaPool.provider_sync_version == int(quota.provider_sync_version or 0),
                FreeTeamQuotaPool.status == "active",
            ).values(reserved=FreeTeamQuotaPool.reserved + amount).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise FreeTeamUnavailableError("budget_reservation_conflict")
            # Flush reset/scope changes without writing the stale reserved
            # value over the atomic SQL increment.
            from sqlalchemy.orm.attributes import set_committed_value
            set_committed_value(quota, "reserved", previous_reserved + amount)
            reservation = FreeTeamReservation(candidate_id=candidate.id,
                routing_profile_id=FREE_TEAM_PROFILE_ID, pool_id="api-request", status="reserved",
                estimated_usage={"metrics": {key: float(value) for key, value in estimates.items()},
                    "quota_reservations": {quota_id: float(amount)}, "billing_scope_id": scope,
                    "model": model, "window_start": now.isoformat(),
                    "quota_windows": (
                        {quota_id: period_window}
                        if (period_window := _quota_period_window(quota)) is not None
                        else {}
                    )},
                quota_pool_ids=[quota_id], expires_at=now + timedelta(seconds=180))
            session.add(reservation)
            await session.flush()
            return RouteLease(reservation_id=reservation.id, provider="openai", model=model,
                credential_profile_id=quota.credential_profile_id, candidate_id=candidate.id,
                quota_pool_ids=(quota_id,), effort="", routing_profile_id=FREE_TEAM_PROFILE_ID,
                pool_id="api-request", max_output_tokens=int(estimates["output_tokens"]), timeout_seconds=120,
                billing_scope_id=scope, billing_mode="complimentary")
    finally:
        if own_session:
            await session.close()


async def execute_budgeted_openai_request(
    payload: Any, *, config: Any, provider: str, model: str,
    identity: Mapping[str, str] | None, sender: Any, session: AsyncSession | None = None,
    sdk_max_retries: int | None = None,
    httpx_transport_retries: int | None = None,
) -> Any:
    """Quota ownership only; canonical usage persistence stays with callers."""
    model = str(model or "")
    is_openai = provider.strip().lower() == "openai"
    billing_mode = openai_billing_mode(config, model) if is_openai else "paid"
    admission = (
        admit_openai_provider_call(
            config,
            allow_probe=is_openai and billing_mode == "complimentary",
        )
        if is_openai
        else {"blocked": False, "probe": False, "state": None}
    )
    if admission["blocked"]:
        state = admission.get("state") or {}
        circuit_status = provider_circuit_status(config)
        reason = (
            "openai_provider_half_open_probe_in_progress"
            if circuit_status.get("provider_half_open")
            and circuit_status.get("provider_blocked")
            and state.get("probe_started_at")
            else "openai_provider_blocked:"
            f"{state.get('reason_code') or circuit_status.get('provider_block_reason_code') or 'quota_rejected'}:"
            f"{state.get('next_probe_at') or state.get('blocked_until') or circuit_status.get('next_probe_at') or circuit_status.get('provider_blocked_until') or 'unknown'}"
        )
        raise FreeTeamUnavailableError(reason)
    half_open_probe = bool(admission.get("probe"))
    half_open_probe_id = admission.get("probe_id")
    probe_finished = False

    def finish_probe(*, clear: bool, result: str | None = None) -> None:
        nonlocal probe_finished
        if half_open_probe and not probe_finished:
            from .openai_provider_circuit import finish_openai_provider_probe

            finish_openai_provider_probe(
                config,
                clear=clear,
                result=result,
                probe_id=half_open_probe_id,
            )
            probe_finished = True

    async def invoke_sender() -> Any:
        value = sender(payload)
        return await value if inspect.isawaitable(value) else value

    # Paid OpenAI calls stay inside the already-approved privacy transaction and
    # return to the ordinary sender.  Token/rate-card attribution is performed
    # by the existing caller after the provider response; this helper owns only
    # the complimentary shared quota when that mode is selected.
    try:
        guarded = is_openai and billing_mode == "complimentary"
        from ..llm.image_budget import initial_image_tokens
        if initial_image_tokens() is not None and not guarded:
            raise ComplimentaryMediaUnboundedError()
        if not guarded:
            try:
                result = await invoke_sender()
            except BaseException as error:
                quota_rejection = (
                    classify_openai_quota_429(error) if is_openai else None
                )
                if quota_rejection is not None:
                    circuit_state = trip_openai_provider_circuit(config, quota_rejection)
                    finish_probe(clear=False)
                    raise FreeTeamUnavailableError(
                        "openai_provider_blocked:"
                        f"{circuit_state.get('reason_code')}:{circuit_state.get('blocked_until')}"
                    ) from error
                finish_probe(
                    clear=False,
                    result="error",
                )
                raise
            finish_probe(clear=True)
            return result

        if not isinstance(payload, Mapping):
            raise FreeTeamUnavailableError("complimentary_payload_unsupported")
        lease = await acquire_openai_request_lease(
            payload,
            config=config,
            model=model,
            identity=identity,
            session=session,
        )
        try:
            result = await invoke_sender()
        except BaseException as error:
            quota_rejection = classify_openai_quota_429(error)
            if quota_rejection is not None:
                circuit_state = trip_openai_provider_circuit(config, quota_rejection)
                await asyncio.shield(
                    finalize_route_lease(
                        lease,
                        actual_usage={
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_tokens": 0,
                            "total_tokens": 0,
                            "requests": 0,
                        },
                        success=False,
                        consume_reserved_on_failure=False,
                        error_class="429",
                        quota_rejection_evidence=quota_rejection,
                        config=config,
                        session=session,
                    )
                )
                finish_probe(clear=False)
                raise FreeTeamUnavailableError(
                    "openai_provider_blocked:"
                    f"{circuit_state.get('reason_code')}:{circuit_state.get('blocked_until')}"
                ) from error

            pre_send_proof = _proven_openai_connect_failure(
                error,
                sdk_max_retries=sdk_max_retries,
                httpx_transport_retries=httpx_transport_retries,
                payload=payload,
            )
            if pre_send_proof is not None:
                await asyncio.shield(
                    finalize_route_lease(
                        lease,
                        actual_usage={
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_tokens": 0,
                            "total_tokens": 0,
                            "requests": 0,
                        },
                        success=False,
                        consume_reserved_on_failure=False,
                        error_class="pre_send_connect_failure",
                        pre_send_evidence=pre_send_proof,
                        config=config,
                        session=session,
                    )
                )
            else:
                response_status = getattr(
                    getattr(error, "response", None), "status_code", None
                )
                await asyncio.shield(
                    finalize_route_lease(
                        lease,
                        success=False,
                        consume_reserved_on_failure=True,
                        error_class=(
                            "429" if response_status == 429 else "usage_unknown"
                        ),
                        config=config,
                        session=session,
                    )
                )
            finish_probe(clear=False, result="error")
            raise

        # A provider response proves the route recovered even if local usage
        # settlement later has a database problem.
        finish_probe(clear=True)
        raw = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)

        def field(key: str) -> Any:
            return raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)

        input_tokens = field("input_tokens") if field("input_tokens") is not None else field("prompt_tokens")
        output_tokens = field("output_tokens") if field("output_tokens") is not None else field("completion_tokens")
        complete = all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens))
        usage = {}
        if complete:
            total = input_tokens + output_tokens
            reported_total = field("total_tokens")
            if reported_total is not None and (type(reported_total) is not int or reported_total != total):
                complete = False
            usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                     "total_tokens": total, "requests": 1}
        if initial_image_tokens() is not None:
            from ..llm.image_budget import current_image_price, normalize_openai_image_response

            image_usage = normalize_openai_image_response(result, current_image_price())
            complete = image_usage is not None
            if complete:
                usage = {key: image_usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
                usage["requests"] = 1
                # These are actual receipt counts (including only documented
                # defaults), never the reservation estimate.
                for key in ("cached_tokens", "cache_write_tokens", "cost_micros", "resolved_model"):
                    if isinstance(raw, dict):
                        raw[key] = image_usage[key]
                    elif raw is not None:
                        setattr(raw, key, image_usage[key])
        await asyncio.shield(finalize_route_lease(lease, actual_usage=usage if complete else None,
            success=True, config=config, session=session))
        if initial_image_tokens() is not None and not complete:
            raise ComplimentaryImageUsageUnknown()
        marker = {"quota_reservation_id": str(lease.reservation_id),
                  "billing_scope_id": lease.billing_scope_id}
        if isinstance(raw, dict):
            raw.update(marker)
        elif raw is not None:
            for key, value in marker.items():
                setattr(raw, key, value)
        return result
    finally:
        finish_probe(clear=False, result="not_sent")


async def acquire_route_lease(
    intent: RouteIntent,
    *,
    prompt: str,
    required_capabilities: Iterable[str] = ("text",),
    subagent_id: str = "",
    team_id: str = "",
    # ``member_key`` is retained only as an input compatibility alias for
    # callers that have not yet moved their lease call to v3 metadata.
    member_key: str = "",
    excluded_candidate_ids: Iterable[str] = (),
    fallback_count: int = 0,
    config: Any = None,
    session: AsyncSession | None = None,
    meter_per_request: bool = False,
    strict_complimentary_only: bool = False,
) -> RouteLease:
    """候補と全共有クォータを1トランザクションで予約する。"""

    if intent.kind != "pool":
        raise ValueError("RouteLeaseはpool intentにのみ必要です")
    own_session = session is None
    if session is None:
        session = await get_db_session()
    excluded = {str(value) for value in excluded_candidate_ids}
    capabilities = {str(value) for value in required_capabilities if str(value)}
    now = _utcnow()
    decision_receipts: list[DecisionReceipt] = []
    leased = False
    cooldown_candidates: list[FreeTeamCandidateModel] = []
    try:
        # 候補一覧の読み取りではlockしない。全行をSKIP LOCKED付きで一括lockすると、
        # 1本目が全候補を保持している間に2本目が候補0件になってしまう。候補を
        # 優先順に並べた後、各候補を1行ずつ短いtransactionで予約する。
        async with session.begin():
            await seed_free_team_defaults(session)
            result = await session.execute(
                select(FreeTeamCandidateModel)
                .where(FreeTeamCandidateModel.enabled.is_(True))
                .options(
                    selectinload(FreeTeamCandidateModel.credential_profile)
                )
            )
            candidates = list(result.scalars().all())
            quota_snapshot = {
                item.id: item
                for item in (
                    await session.execute(select(FreeTeamQuotaPool))
                ).scalars().all()
            }
            openai_complimentary_verified = False
            if any(
                item.credential_profile is not None
                and _uses_shared_openai_key(item, item.credential_profile)
                for item in candidates
            ):
                openai_complimentary_verified = (
                    await _openai_complimentary_candidate_verified(session)
                )
            otherwise_eligible = [
                item
                for item in candidates
                if item.id not in excluded
                and item.credential_profile is not None
                and _candidate_matches(
                    item,
                    item.credential_profile,
                    candidate_patterns=intent.candidate_ids,
                    required_capabilities=capabilities,
                    now=now,
                    quotas=quota_snapshot,
                    config=config,
                    strict_complimentary_only=strict_complimentary_only,
                    ignore_cooldown=True,
                )
                and not (
                    _uses_shared_openai_key(item, item.credential_profile)
                    and not openai_complimentary_verified
                )
                and _cli_quota_contract_safe(item, quota_snapshot)
            ]
            cooldown_candidates = [
                item
                for item in otherwise_eligible
                if item.cooldown_until is not None and item.cooldown_until > now
            ]
            candidates = [
                item
                for item in otherwise_eligible
                if item.cooldown_until is None or item.cooldown_until <= now
            ]
            candidates.sort(
                key=lambda item: _candidate_score(item, now, quota_snapshot)
            )

        if not candidates and cooldown_candidates:
            cooldown_until = min(
                item.cooldown_until for item in cooldown_candidates
                if item.cooldown_until is not None
            )
            raise FreeTeamCooldownWait(
                retry_after_seconds=max(0.0, (cooldown_until - now).total_seconds()),
                cooldown_until=cooldown_until,
                candidate_ids=(item.id for item in cooldown_candidates),
            )

        circuit = provider_circuit_status(config, now=now)

        candidates = await _semantic_candidate_order(
            candidates,
            intent=intent,
            prompt=prompt,
            required_capabilities=capabilities,
            config=config,
            now=now,
            quotas=quota_snapshot,
            receipt_sink=decision_receipts,
        )
        ordered_candidate_ids = [item.id for item in candidates]
        candidate_hints = {item.id: item for item in candidates}
        quota_blocked_candidates: dict[str, tuple[datetime, str, tuple[str, ...]]] = {}
        non_quota_blocker = False

        for candidate_id in ordered_candidate_ids:
            lease: RouteLease | None = None
            async with session.begin():
                hint = candidate_hints[candidate_id]
                if (
                    hint.credential_profile is not None
                    and _uses_shared_openai_key(hint, hint.credential_profile)
                    and not await _openai_complimentary_candidate_verified(session)
                ):
                    non_quota_blocker = True
                    continue
                locked_credential = None
                if hint.provider == "openrouter":
                    credential_result = await session.execute(
                        select(FreeTeamCredentialProfile)
                        .where(
                            FreeTeamCredentialProfile.id
                            == hint.credential_profile_id
                        )
                        .with_for_update()
                    )
                    locked_credential = credential_result.scalar_one_or_none()
                    if locked_credential is None:
                        non_quota_blocker = True
                        continue
                locked_result = await session.execute(
                    select(FreeTeamCandidateModel)
                    .where(
                        FreeTeamCandidateModel.id == candidate_id,
                        FreeTeamCandidateModel.enabled.is_(True),
                    )
                    .options(
                        selectinload(FreeTeamCandidateModel.credential_profile)
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update(skip_locked=True)
                )
                candidate = locked_result.scalar_one_or_none()
                if (
                    candidate is not None
                    and locked_credential is not None
                    and candidate.credential_profile_id == locked_credential.id
                ):
                    candidate.credential_profile = locked_credential
                if (
                    candidate is None
                    or candidate.credential_profile is None
                    or not _candidate_matches(
                        candidate,
                        candidate.credential_profile,
                        candidate_patterns=intent.candidate_ids,
                        required_capabilities=capabilities,
                        now=now,
                        quotas=quota_snapshot,
                        config=config,
                        strict_complimentary_only=strict_complimentary_only,
                    )
                ):
                    non_quota_blocker = True
                    continue
                output_token_limit = _candidate_output_token_limit(
                    candidate,
                    config,
                    strict_complimentary_only=strict_complimentary_only,
                )
                if output_token_limit is None:
                    non_quota_blocker = True
                    continue
                options = dict(candidate.provider_options or {})
                concurrency_limit = max(1, int(options.get("concurrency_limit") or 64))
                if await _active_candidate_reservations(session, candidate.id, now) >= concurrency_limit:
                    non_quota_blocker = True
                    continue
                max_requests = (
                    max(
                        8,
                        int(options.get("max_requests_per_turn") or 8),
                    )
                    if "tools" in capabilities
                    else 1
                )
                estimates = estimate_usage(
                    prompt,
                    max_output_tokens=int(candidate.max_output_tokens or 1),
                    provider_options={
                        **options,
                        "_max_input_tokens": int(candidate.max_input_tokens or 1),
                        "_max_requests_per_turn": max_requests,
                    },
                )
                initial_input_estimate = max(
                    1, len((prompt or "").encode("utf-8"))
                )
                if initial_input_estimate > int(candidate.max_input_tokens or 1):
                    non_quota_blocker = True
                    continue
                quota_ids = sorted({str(value) for value in (candidate.quota_pool_ids or [])})
                quota_result = await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id.in_(quota_ids))
                    .order_by(FreeTeamQuotaPool.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                quota_by_id = {item.id: item for item in quota_result.scalars().all()}
                if len(quota_by_id) != len(quota_ids):
                    non_quota_blocker = True
                    continue
                locked_quota_snapshot = {**quota_snapshot, **quota_by_id}
                if not _candidate_matches(
                    candidate,
                    candidate.credential_profile,
                    candidate_patterns=intent.candidate_ids,
                    required_capabilities=capabilities,
                    now=now,
                    quotas=locked_quota_snapshot,
                    config=config,
                    strict_complimentary_only=strict_complimentary_only,
                ):
                    non_quota_blocker = True
                    continue
                reservations: dict[str, Decimal] = {}
                can_reserve = True
                quota_waits: list[tuple[datetime, str, str]] = []
                for quota_id in quota_ids:
                    quota = quota_by_id[quota_id]
                    _reset_quota_if_needed(quota, now)
                    if quota.status != "active" and not (
                        quota.status == "exhausted"
                        and quota.id in OPENAI_DAILY_QUOTA_GROUPS
                    ):
                        can_reserve = False
                        wait = _quota_reset_wait(quota, now)
                        if wait is not None:
                            quota_waits.append((wait[0], wait[1], quota_id))
                        else:
                            non_quota_blocker = True
                        continue
                    amount = _amount_for_metric(quota.metric_type, estimates)
                    if _is_gemini_promo_usd_quota(quota) and amount <= 0:
                        # A zero estimate cannot conservatively book a paid
                        # Gemini request, so do not admit it as free.
                        can_reserve = False
                        non_quota_blocker = True
                        continue
                    if (
                        candidate.credential_profile is not None
                        and _uses_shared_openai_key(
                            candidate, candidate.credential_profile
                        )
                    ):
                        quota_available = await _read_only_openai_quota_available(
                            session, quota, config, now
                        )
                    elif quota.id == GEMINI_PROMO_MONTHLY_QUOTA_ID:
                        try:
                            quota_available = (
                                await _gemini_promo_quota_snapshot(
                                    session, quota, now
                                )
                            )["available"]
                        except FreeTeamUnavailableError:
                            quota_available = None
                    else:
                        quota_available = _locked_quota_available(quota, config)
                    if (
                        quota.status == "exhausted"
                        and quota.id in OPENAI_DAILY_QUOTA_GROUPS
                        and quota_available > 0
                    ):
                        quota.status = "active"
                    if quota_available is None or (not quota.provider_managed_cap and amount > quota_available):
                        can_reserve = False
                        wait = _quota_reset_wait(quota, now)
                        if wait is not None:
                            quota_waits.append((wait[0], wait[1], quota_id))
                        else:
                            non_quota_blocker = True
                        continue
                    # Official OpenAI requests are metered at the privacy
                    # gateway's exact wire boundary, including every retry.
                    # This outer lease only selects the target.
                    if not (meter_per_request and candidate.provider == "openai"):
                        reservations[quota_id] = amount
                if not can_reserve:
                    if quota_waits:
                        reset_at = max(item[0] for item in quota_waits)
                        reset_kind = next(
                            (item[1] for item in quota_waits if item[0] == reset_at),
                            "daily",
                        )
                        quota_blocked_candidates[candidate.id] = (
                            reset_at,
                            reset_kind,
                            tuple(item[2] for item in quota_waits),
                        )
                    continue

                # Bind an unused OpenAI pool to the currently verified billing
                # scope on its first routed lease. Later reads then account
                # central usage from that same scope consistently.
                for quota_id in quota_ids:
                    if quota_id not in OPENAI_DAILY_QUOTA_GROUPS:
                        continue
                    quota = quota_by_id[quota_id]
                    billing_scope = str(
                        config_get(config, "openai.billing_scope_id", "") or ""
                    )
                    policy = dict(quota.reset_policy or {})
                    if not billing_scope or policy.get("billing_scope_id") not in {
                        None,
                        billing_scope,
                    }:
                        non_quota_blocker = True
                        can_reserve = False
                        break
                    policy["billing_scope_id"] = billing_scope
                    quota.reset_policy = policy
                if not can_reserve:
                    continue

                for quota_id, amount in reservations.items():
                    quota_by_id[quota_id].reserved = _decimal(
                        quota_by_id[quota_id].reserved
                    ) + amount
                candidate.selection_count = int(candidate.selection_count or 0) + 1
                candidate.last_selected_at = now
                promo_billing_scope_id = (
                    _gemini_promo_scope(quota_by_id[GEMINI_PROMO_MONTHLY_QUOTA_ID])
                    if str(candidate.provider or "").strip().lower() == "gemini"
                    and GEMINI_PROMO_MONTHLY_QUOTA_ID in quota_ids
                    else ""
                )
                selected_credential_profile_id = str(
                    candidate.credential_profile.id or ""
                )
                selected_billing_mode = (
                    _effective_candidate_billing_mode(
                        candidate, candidate.credential_profile
                    )
                    or "paid"
                )
                reservation = FreeTeamReservation(
                    candidate_id=candidate.id,
                    routing_profile_id=intent.routing_profile_id or FREE_TEAM_PROFILE_ID,
                    pool_id=intent.pool_id,
                    member_key=(
                        subagent_id
                        or member_key
                        or intent.subagent_id
                        or None
                    ),
                    status="reserved",
                    estimated_usage={
                        "metrics": {key: float(value) for key, value in estimates.items()},
                        "quota_reservations": {
                            key: float(value) for key, value in reservations.items()
                        },
                        "window_start": now.isoformat(),
                        "credential_profile_id": selected_credential_profile_id,
                        "billing_mode": selected_billing_mode,
                        **(
                            {"billing_scope_id": promo_billing_scope_id}
                            if promo_billing_scope_id
                            else {}
                        ),
                        "quota_windows": {
                            quota_id: window
                            for quota_id in reservations
                            if (window := _quota_period_window(
                                quota_by_id[quota_id]
                            )) is not None
                        },
                    },
                    quota_pool_ids=quota_ids,
                    fallback_count=max(0, int(fallback_count)),
                    expires_at=now
                    + timedelta(seconds=max(30, int(candidate.timeout_seconds or 120) + 30)),
                )
                session.add(reservation)
                await session.flush()
                credential = candidate.credential_profile
                credential_base_url = str(credential.base_url or "")
                if _uses_shared_openai_key(candidate, credential):
                    credential_base_url = str(
                        config_get(config, "openai.base_url", "")
                        or config_get(config, "openai_base_url", "")
                        or os.getenv("OPENAI_BASE_URL", "")
                        or "https://api.openai.com/v1"
                    )
                lease = RouteLease(
                    reservation_id=reservation.id,
                    provider=candidate.provider,
                    model=candidate.model,
                    credential_profile_id=credential.id,
                    candidate_id=candidate.id,
                    quota_pool_ids=tuple(quota_ids),
                    effort=_lease_effort(intent, candidate),
                    routing_profile_id=reservation.routing_profile_id,
                    pool_id=reservation.pool_id,
                    max_output_tokens=output_token_limit,
                    timeout_seconds=int(candidate.timeout_seconds or 120),
                    base_url=credential_base_url,
                    api_key=_credential_api_key(credential, config),
                    cli_auth_reference=str(credential.cli_auth_reference or ""),
                    provider_options=options,
                    fallback_count=reservation.fallback_count,
                    team_id=team_id or intent.team_id,
                    subagent_id=subagent_id or intent.subagent_id,
                    llm_profile_id=intent.llm_profile_id,
                    billing_scope_id=promo_billing_scope_id,
                    billing_mode=_effective_candidate_billing_mode(
                        candidate, credential
                    ) or "paid",
                )
            if lease is not None:
                leased = True
                for receipt in decision_receipts:
                    lease.decision_receipt = finalize_decision_receipt(receipt, host_action="lease", host_candidate=lease.candidate_id,
                                                                      outcome="leased", validation_result="host_locked_revalidated")
                return lease
        if (
            ordered_candidate_ids
            and not non_quota_blocker
            and len(quota_blocked_candidates) == len(ordered_candidate_ids)
        ):
            candidate_resets = [
                (value[0], candidate_id, value[1], value[2])
                for candidate_id, value in quota_blocked_candidates.items()
            ]
            reset_at, _candidate_id, reset_kind, quota_ids = min(candidate_resets)
            earliest_cooldown = min(
                (
                    item.cooldown_until
                    for item in cooldown_candidates
                    if item.cooldown_until is not None
                ),
                default=None,
            )
            if earliest_cooldown is not None and earliest_cooldown < reset_at:
                raise FreeTeamCooldownWait(
                    retry_after_seconds=max(
                        0.0, (earliest_cooldown - now).total_seconds()
                    ),
                    cooldown_until=earliest_cooldown,
                    candidate_ids=(
                        item.id
                        for item in cooldown_candidates
                        if item.cooldown_until == earliest_cooldown
                    ),
                )
            raise FreeTeamQuotaExhausted(
                retry_after_seconds=max(0.0, (reset_at - now).total_seconds()),
                reset_at=reset_at,
                reset_kind=reset_kind,
                quota_ids=quota_ids,
            )
        if circuit["provider_blocked"]:
            raise FreeTeamUnavailableError(
                "openai_provider_blocked:"
                f"{circuit.get('provider_block_reason_code') or 'quota_rejected'}:"
                f"{circuit.get('next_probe_at') or circuit.get('provider_blocked_until') or 'unknown'}"
            )
        raise FreeTeamUnavailableError(
            f"無料Teamのプール「{intent.pool_id}」で安全に利用できる候補がありません。"
        )
    finally:
        if not leased:
            for receipt in decision_receipts:
                finalize_decision_receipt(receipt, host_action="no_lease", outcome="not_leased", validation_result="host_lease_unavailable")
        if own_session:
            await session.close()


async def preflight_route_quota(
    intent: RouteIntent,
    *,
    prompt: str,
    required_capabilities: Iterable[str] = ("text",),
    config: Any = None,
    session: AsyncSession | None = None,
    meter_per_request: bool = False,
    strict_complimentary_only: bool = False,
    openrouter_quota_refresher: Callable[
        [], Awaitable[Mapping[str, Any]]
    ]
    | None = None,
) -> None:
    """Read-only check for a known minute/daily quota wait before a run starts.

    This function never reserves or consumes quota.  Only the explicit API
    task overlay may refresh OpenRouter's fixed read-only /key observation;
    ordinary/manual Free Team preflight remains network-free.  It returns
    ``None`` unless every currently safe candidate is blocked specifically by
    a valid request-quota reset window.
    """

    if intent.kind != "pool":
        return None
    credential_blocker: FreeTeamCredentialBlocked | None = None
    observer_wait: FreeTeamObserverRefreshWait | None = None
    if (
        strict_complimentary_only
        and config_get(config, "runtime.free_team_api_task", False) is True
        and any(
            fnmatchcase("openrouter-free-router", pattern)
            or str(pattern).startswith("openrouter-dynamic-")
            for pattern in intent.candidate_ids
        )
    ):
        if openrouter_quota_refresher is None:
            credential_blocker = FreeTeamCredentialBlocked(
                "openrouter_observer_unavailable"
            )
        else:
            result = dict(await openrouter_quota_refresher())
            if result.get("status") != "observed":
                failure = str(result.get("failure") or "refresh_failed")
                transient = (
                    failure
                    in {
                        "timeout",
                        "network_error",
                        "http_408",
                        "http_429",
                        "credential_changed",
                        "sync_failed",
                    }
                    or re.fullmatch(r"http_5[0-9]{2}", failure) is not None
                )
                if transient:
                    observer_wait = FreeTeamObserverRefreshWait(
                        f"openrouter_{failure}",
                        retry_after_seconds=60.0,
                    )
                else:
                    allowed = {
                        "missing_key",
                        "unsupported_endpoint",
                        "invalid_response",
                        "redirect_rejected",
                    }
                    if failure.startswith("http_"):
                        failure = "provider_rejected"
                    elif failure not in allowed:
                        failure = "refresh_failed"
                    credential_blocker = FreeTeamCredentialBlocked(
                        f"openrouter_{failure}"
                    )
    own_session = session is None
    if session is None:
        session = await get_db_session()
    now = _utcnow()
    capabilities = {str(value) for value in required_capabilities if str(value)}
    eligible: list[FreeTeamCandidateModel] = []
    cooldown_candidates: list[FreeTeamCandidateModel] = []
    reset_by_candidate: dict[str, tuple[datetime, str, tuple[str, ...]]] = {}
    has_non_quota_candidate = False
    try:
        async with session.begin():
            result = await session.execute(
                select(FreeTeamCandidateModel).where(
                    FreeTeamCandidateModel.enabled.is_(True)
                ).options(selectinload(FreeTeamCandidateModel.credential_profile))
            )
            rows = list(result.scalars().all())
            quotas = {
                item.id: item
                for item in (await session.execute(select(FreeTeamQuotaPool))).scalars().all()
            }
            openai_verified = False
            if any(
                item.credential_profile is not None
                and _uses_shared_openai_key(item, item.credential_profile)
                for item in rows
            ):
                openai_verified = await _openai_complimentary_candidate_verified(session)
            otherwise_eligible = [
                item
                for item in rows
                if item.credential_profile is not None
                and _candidate_matches(
                    item,
                    item.credential_profile,
                    candidate_patterns=intent.candidate_ids,
                    required_capabilities=capabilities,
                    now=now,
                    quotas=quotas,
                    config=config,
                    strict_complimentary_only=strict_complimentary_only,
                    ignore_cooldown=True,
                )
                and not (
                    _uses_shared_openai_key(item, item.credential_profile)
                    and not openai_verified
                )
                and _cli_quota_contract_safe(item, quotas)
            ]
            cooldown_candidates = [
                item
                for item in otherwise_eligible
                if item.cooldown_until is not None and item.cooldown_until > now
            ]
            eligible = [
                item
                for item in otherwise_eligible
                if item.cooldown_until is None or item.cooldown_until <= now
            ]
            if not otherwise_eligible:
                if observer_wait is not None:
                    raise observer_wait
                if credential_blocker is not None:
                    raise credential_blocker
                circuit = provider_circuit_status(config, now=now)
                if circuit["provider_blocked"]:
                    raise FreeTeamUnavailableError(
                        "openai_provider_blocked:"
                        f"{circuit.get('provider_block_reason_code') or 'quota_rejected'}:"
                        f"{circuit.get('next_probe_at') or circuit.get('provider_blocked_until') or 'unknown'}"
                    )
            if not eligible and cooldown_candidates:
                cooldown_until = min(
                    item.cooldown_until
                    for item in cooldown_candidates
                    if item.cooldown_until is not None
                )
                raise FreeTeamCooldownWait(
                    retry_after_seconds=max(0.0, (cooldown_until - now).total_seconds()),
                    cooldown_until=cooldown_until,
                    candidate_ids=(item.id for item in cooldown_candidates),
                )
            for candidate in eligible:
                options = dict(candidate.provider_options or {})
                concurrency_limit = max(1, int(options.get("concurrency_limit") or 64))
                if await _active_candidate_reservations(session, candidate.id, now) >= concurrency_limit:
                    has_non_quota_candidate = True
                    continue
                max_requests = (
                    max(8, int(options.get("max_requests_per_turn") or 8))
                    if "tools" in capabilities
                    else 1
                )
                estimates = estimate_usage(
                    prompt,
                    max_output_tokens=int(candidate.max_output_tokens or 1),
                    provider_options={
                        **options,
                        "_max_input_tokens": int(candidate.max_input_tokens or 1),
                        "_max_requests_per_turn": max_requests,
                    },
                )
                if max(1, len((prompt or "").encode("utf-8"))) > int(candidate.max_input_tokens or 1):
                    has_non_quota_candidate = True
                    continue
                quota_ids = sorted({str(value) for value in (candidate.quota_pool_ids or [])})
                if not quota_ids or any(quota_id not in quotas for quota_id in quota_ids):
                    has_non_quota_candidate = True
                    continue
                waits: list[tuple[datetime, str, str]] = []
                blocked = False
                non_waitable = False
                for quota_id in quota_ids:
                    quota = quotas[quota_id]
                    _reset_quota_if_needed(quota, now)
                    amount = _amount_for_metric(quota.metric_type, estimates)
                    if (
                        candidate.credential_profile is not None
                        and _uses_shared_openai_key(
                            candidate, candidate.credential_profile
                        )
                    ):
                        quota_available = await _read_only_openai_quota_available(
                            session, quota, config, now
                        )
                    elif quota.id == "openrouter-free-rpd":
                        quota_available = _read_only_quota_available(
                            quota, now, config=config
                        )
                    else:
                        quota_available = (
                            quota.available_for_tier(
                                config_get(config, "openai.usage_tier", None)
                            )
                            if quota.id in OPENAI_DAILY_QUOTA_GROUPS
                            else quota.available
                        )
                    if (
                        (
                            quota.status != "active"
                            and not (
                                quota.status == "exhausted"
                                and quota.id in OPENAI_DAILY_QUOTA_GROUPS
                            )
                        )
                        or quota_available is None
                        or (not quota.provider_managed_cap and amount > quota_available)
                    ):
                        blocked = True
                        wait = _quota_reset_wait(quota, now)
                        if wait is None:
                            non_waitable = True
                        else:
                            waits.append((wait[0], wait[1], quota_id))
                if not blocked:
                    return None
                if non_waitable or not waits:
                    has_non_quota_candidate = True
                    continue
                reset_at = max(item[0] for item in waits)
                reset_kind = next(
                    (item[1] for item in waits if item[0] == reset_at), "daily"
                )
                reset_by_candidate[candidate.id] = (
                    reset_at,
                    reset_kind,
                    tuple(item[2] for item in waits),
                )
        if (
            eligible
            and not has_non_quota_candidate
            and len(reset_by_candidate) == len(eligible)
        ):
            reset_at, _candidate_id, reset_kind, quota_ids = min(
                (value[0], candidate_id, value[1], value[2])
                for candidate_id, value in reset_by_candidate.items()
            )
            earliest_cooldown = min(
                (
                    item.cooldown_until
                    for item in cooldown_candidates
                    if item.cooldown_until is not None
                ),
                default=None,
            )
            if earliest_cooldown is not None and earliest_cooldown < reset_at:
                raise FreeTeamCooldownWait(
                    retry_after_seconds=max(
                        0.0, (earliest_cooldown - now).total_seconds()
                    ),
                    cooldown_until=earliest_cooldown,
                    candidate_ids=(
                        item.id
                        for item in cooldown_candidates
                        if item.cooldown_until == earliest_cooldown
                    ),
                )
            raise FreeTeamQuotaExhausted(
                retry_after_seconds=max(0.0, (reset_at - now).total_seconds()),
                reset_at=reset_at,
                reset_kind=reset_kind,
                quota_ids=quota_ids,
            )
        return None
    finally:
        if own_session:
            await session.close()


def _windowed_quota_policy(quota: FreeTeamQuotaPool) -> dict[str, Any] | None:
    """Return the validated UTC period policy for a pool with a real window."""

    if quota.window_end is None:
        return None
    if not isinstance(quota.reset_policy, Mapping):
        return None
    policy = dict(quota.reset_policy)
    if str(policy.get("timezone") or "UTC").upper() != "UTC":
        return None
    try:
        window_end = _utc_naive(quota.window_end)
        window_start = _utc_naive(quota.window_start) if quota.window_start is not None else None
        if window_start is None:
            window_start = _previous_window_start(policy, window_end)
        if window_start is None:
            return None
        if window_end <= window_start:
            return None
        if _next_window_end(policy, window_start) != window_end:
            return None
    except (ArithmeticError, TypeError, ValueError, FreeTeamUnavailableError):
        return None
    return policy


def _quota_period_window(quota: FreeTeamQuotaPool) -> dict[str, str] | None:
    """Snapshot canonical UTC period boundaries for a new reservation."""

    policy = _windowed_quota_policy(quota)
    if policy is None or quota.window_end is None:
        return None
    window_end = _utc_naive(quota.window_end)
    window_start = _previous_window_start(policy, window_end)
    if window_start is None or window_start >= window_end:
        return None
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }


def _previous_window_start(
    reset_policy: Mapping[str, Any], window_end: datetime
) -> datetime | None:
    """Calculate the UTC boundary immediately before an end boundary."""

    window_end = _utc_naive(window_end)
    kind = str(reset_policy.get("kind") or "none").casefold()
    if kind == "minute":
        start = window_end - timedelta(minutes=1)
    elif kind == "daily":
        start = window_end - timedelta(days=1)
    elif kind == "monthly":
        try:
            policy_day = max(1, min(31, int(reset_policy.get("day") or 1)))
            policy_hour = max(0, min(23, int(reset_policy.get("hour") or 0)))
        except (TypeError, ValueError):
            return None
        year = window_end.year
        month = window_end.month - 1
        if month < 1:
            year -= 1
            month = 12
        day = min(policy_day, monthrange(year, month)[1])
        start = datetime(year, month, day, policy_hour)
    else:
        return None
    try:
        return start if _next_window_end(reset_policy, start) == window_end else None
    except (ArithmeticError, TypeError, ValueError, FreeTeamUnavailableError):
        return None


def _uncertain_window_grace(
    window_start: datetime,
    window_end: datetime,
) -> timedelta | None:
    """Use one proportional grace rule for every period-based quota window."""

    start = _utc_naive(window_start)
    end = _utc_naive(window_end)
    duration = end - start
    if duration <= timedelta(0):
        return None
    return max(UNCERTAIN_WINDOW_GRACE_MINIMUM, duration / 24)


def _floor_uncertain_hold_cap(value: Decimal, metric_type: str) -> Decimal:
    """Floor count/USD caps, retaining one smallest unit for positive count caps."""

    if str(metric_type or "").casefold() == "usd":
        return value.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
    if value <= 0:
        return Decimal(0)
    # A sub-unit percentage cap otherwise rounds to zero and releases an
    # uncertain request inside its original period. One whole unit is the
    # smallest conservative hold an integer meter can represent.
    return max(Decimal(1), value.to_integral_value(rounding=ROUND_FLOOR))


async def _bounded_uncertain_hold(
    session: AsyncSession,
    *,
    reservation_id: Any,
    quota: FreeTeamQuotaPool,
    current_hold: Decimal,
    usage_tier: str | None = None,
) -> tuple[Decimal, dict[str, str]]:
    """Cap windowed holds at 5% / 20%, retaining full Gemini promo USD holds."""

    quota_id = str(quota.id)
    if _is_gemini_promo_usd_quota(quota):
        return current_hold, {
            "policy": "full_booked_hold_for_unknown_usd",
            "retained_hold": str(current_hold),
        }
    limit = _decimal(quota.daily_cap_for_tier(usage_tier))
    if not limit.is_finite() or limit < 0:
        return current_hold, {"pool_limit": str(limit), "cap_unavailable": "true"}
    per_reservation_cap = _floor_uncertain_hold_cap(
        limit * UNCERTAIN_HOLD_PER_RESERVATION_RATIO, quota.metric_type
    )
    aggregate_cap = _floor_uncertain_hold_cap(
        limit * UNCERTAIN_HOLD_POOL_RATIO, quota.metric_type
    )
    other_rows = (await session.execute(
        select(FreeTeamReservation.id, FreeTeamReservation.estimated_usage).where(
            FreeTeamReservation.status == "uncertain",
            FreeTeamReservation.id != reservation_id,
        )
    )).all()
    other_uncertain = Decimal(0)
    for _other_id, raw_estimate in other_rows:
        estimate = raw_estimate if isinstance(raw_estimate, Mapping) else {}
        holds = estimate.get("remaining_quota_reservations")
        if not isinstance(holds, Mapping):
            holds = estimate.get("quota_reservations")
        if not isinstance(holds, Mapping) or quota_id not in holds:
            continue
        try:
            amount = _decimal(holds[quota_id])
        except Exception:
            other_uncertain = aggregate_cap
            break
        if not amount.is_finite() or amount < 0:
            # A malformed existing ledger consumes the aggregate allowance.
            other_uncertain = aggregate_cap
            break
        other_uncertain += amount
    aggregate_remaining = max(Decimal(0), aggregate_cap - other_uncertain)
    retained = min(current_hold, per_reservation_cap, aggregate_remaining)
    return retained, {
        "pool_limit": str(limit),
        "per_reservation_cap": str(per_reservation_cap),
        "aggregate_cap": str(aggregate_cap),
        "other_uncertain_holds": str(other_uncertain),
        "retained_hold": str(retained),
    }


async def _cap_uncertain_reservation_holds(
    reservation_id: Any,
    *,
    session: AsyncSession,
    usage_tier: str | None = None,
) -> bool:
    """Bound holds for every uncertain reservation with a period-based quota."""

    reservation_result = await session.execute(
        select(FreeTeamReservation)
        .where(FreeTeamReservation.id == reservation_id)
        .with_for_update()
    )
    reservation = reservation_result.scalar_one_or_none()
    if reservation is None or str(reservation.status or "") != "uncertain":
        return False
    if not isinstance(reservation.estimated_usage, Mapping):
        return False
    accounting = dict(reservation.estimated_usage or {})
    original_holds = accounting.get("quota_reservations")
    if not isinstance(original_holds, Mapping):
        original_holds = {}
    raw_holds = accounting.get("remaining_quota_reservations")
    if not isinstance(raw_holds, Mapping):
        raw_holds = original_holds
    if not isinstance(raw_holds, Mapping):
        return False
    quota_ids = sorted(
        {str(key) for key in raw_holds}
        | (
            {GEMINI_PROMO_MONTHLY_QUOTA_ID}
            if GEMINI_PROMO_MONTHLY_QUOTA_ID in original_holds
            else set()
        )
    )
    if not quota_ids:
        return False

    quota_rows = (await session.execute(
        select(FreeTeamQuotaPool)
        .where(FreeTeamQuotaPool.id.in_(quota_ids))
        .order_by(FreeTeamQuotaPool.id)
        .with_for_update()
    )).scalars().all()
    quotas = {str(item.id): item for item in quota_rows}

    holds = dict(raw_holds)
    adjustments: dict[str, dict[str, str]] = {}
    now = _utcnow()
    for quota_id in quota_ids:
        quota = quotas.get(quota_id)
        if quota is not None and _is_gemini_promo_usd_quota(quota):
            try:
                original = _decimal(original_holds[quota_id])
                current = _decimal(holds.get(quota_id, 0))
            except Exception:
                continue
            reserved_before = _decimal(quota.reserved)
            if (
                not original.is_finite()
                or original <= 0
                or not current.is_finite()
                or current < 0
                or not reserved_before.is_finite()
                or reserved_before < 0
            ):
                continue
            if current < original:
                restored = original - current
                quota.reserved = reserved_before + restored
                holds[quota_id] = float(original)
                adjustments[quota_id] = {
                    "original_hold": str(original),
                    "prior_remaining_hold": str(current),
                    "restored_hold": str(restored),
                    "retained_hold": str(original),
                    "policy": "full_booked_hold_for_unknown_usd",
                }
                continue
            continue
        if quota is None or _reservation_window_bounds(
            reservation, quota, quota_id
        ) is None:
            continue
        try:
            original = _decimal(holds[quota_id])
        except Exception:
            continue
        if not original.is_finite() or original < 0:
            continue
        if _decimal(quota.reserved) < original:
            # A mismatch is not safe to repair by releasing more than persisted.
            continue
        retained, policy = await _bounded_uncertain_hold(
            session,
            reservation_id=reservation.id,
            quota=quota,
            current_hold=original,
            usage_tier=usage_tier,
        )
        if retained >= original:
            continue
        released = original - retained
        quota.reserved = _decimal(quota.reserved) - released
        if retained == 0:
            holds.pop(quota_id, None)
        else:
            holds[quota_id] = float(retained)
        adjustments[quota_id] = {
            "original_hold": str(original),
            "retained_hold": str(retained),
            "released_by_policy": str(released),
            **policy,
        }

    if not adjustments:
        return False
    accounting["remaining_quota_reservations"] = holds
    accounting["uncertain_hold_policy"] = {
        "applied_at": now.isoformat(),
        "per_reservation_ratio": str(UNCERTAIN_HOLD_PER_RESERVATION_RATIO),
        "aggregate_pool_ratio": str(UNCERTAIN_HOLD_POOL_RATIO),
        "pools": adjustments,
    }
    reservation.estimated_usage = accounting
    reservation.settlement_version = int(reservation.settlement_version or 0) + 1
    return True


def _reservation_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc_naive(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc_naive(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError):
        return None


def _reservation_window_bounds(
    reservation: FreeTeamReservation,
    quota: FreeTeamQuotaPool,
    quota_id: str,
) -> tuple[datetime, datetime, str] | None:
    """Use saved original boundaries; derive legacy rows from their acquisition time."""

    if not isinstance(reservation.estimated_usage, Mapping):
        return None
    estimate = reservation.estimated_usage
    captured_at = _reservation_timestamp(estimate.get("window_start"))
    if captured_at is None and reservation.created_at is not None:
        captured_at = _utc_naive(reservation.created_at)
    quota_windows = estimate.get("quota_windows")
    stored_window = quota_windows.get(quota_id) if isinstance(quota_windows, Mapping) else None
    if isinstance(quota_windows, Mapping) and quota_id in quota_windows and not isinstance(
        stored_window, Mapping
    ):
        return None
    if isinstance(stored_window, Mapping):
        window_start = _reservation_timestamp(stored_window.get("window_start"))
        window_end = _reservation_timestamp(stored_window.get("window_end"))
        if (
            window_start is not None
            and window_end is not None
            and window_end > window_start
            and captured_at is not None
            and window_start <= captured_at < window_end
        ):
            # A policy may be edited after acquisition. Saved boundaries remain
            # authoritative for this reservation and must not move its expiry.
            return window_start, window_end, "stored_pool_window"
        # Present but malformed saved boundaries are not a legacy row; do not
        # replace their evidence with a newer policy and risk early release.
        return None

    policy = _windowed_quota_policy(quota)
    if captured_at is None or policy is None:
        return None
    try:
        window_end = _next_window_end(policy, captured_at)
    except (ArithmeticError, TypeError, ValueError, FreeTeamUnavailableError):
        return None
    if window_end is None:
        return None
    window_start = _previous_window_start(policy, window_end)
    if window_start is None or window_end <= window_start:
        return None
    return window_start, window_end, "derived_from_reservation_time"


async def _release_closed_window_uncertain_reservation(
    reservation_id: Any,
    *,
    session: AsyncSession,
    now: datetime | None = None,
) -> bool:
    """Release eligible holds after window and grace; retain Gemini promo USD holds."""

    try:
        reservation_uuid = uuid.UUID(str(reservation_id))
    except (TypeError, ValueError, AttributeError):
        return False

    async with session.begin():
        reservation_result = await session.execute(
            select(FreeTeamReservation)
            .where(FreeTeamReservation.id == reservation_uuid)
            .with_for_update()
        )
        reservation = reservation_result.scalar_one_or_none()
        if reservation is None or str(reservation.status or "") != "uncertain":
            return False

        if not isinstance(reservation.estimated_usage, Mapping):
            return False
        estimate = dict(reservation.estimated_usage)
        raw_holds = estimate.get("remaining_quota_reservations")
        if not isinstance(raw_holds, Mapping):
            raw_holds = estimate.get("quota_reservations")
        if not isinstance(raw_holds, Mapping):
            return False
        original_holds = estimate.get("quota_reservations")
        if not isinstance(original_holds, Mapping):
            original_holds = {}
        linked_pool_ids = reservation.quota_pool_ids
        if not isinstance(linked_pool_ids, (list, tuple)):
            linked_pool_ids = []
        quota_ids = sorted(
            {str(key) for key in raw_holds}
            | {str(key) for key in original_holds}
            | {str(value) for value in linked_pool_ids}
        )
        if not quota_ids:
            return False

        quota_rows = (await session.execute(
            select(FreeTeamQuotaPool)
            .where(FreeTeamQuotaPool.id.in_(quota_ids))
            .order_by(FreeTeamQuotaPool.id)
            .with_for_update()
        )).scalars().all()
        quotas = {str(item.id): item for item in quota_rows}

        current = _utc_naive(now or _utcnow())
        current_holds = dict(raw_holds)
        actual_usage = (
            dict(reservation.actual_usage)
            if isinstance(reservation.actual_usage, Mapping)
            else {}
        )
        prior_events = actual_usage.get("uncertain_window_release_events")
        if not isinstance(prior_events, list):
            prior_events = []
        already_released_pools = {
            str(quota_id)
            for prior_event in prior_events
            if isinstance(prior_event, Mapping)
            and isinstance(prior_event.get("quota_pools"), Mapping)
            for quota_id in prior_event["quota_pools"]
        }
        released_pools: dict[str, dict[str, str]] = {}
        for quota_id in quota_ids:
            quota = quotas.get(quota_id)
            if quota_id == GEMINI_PROMO_MONTHLY_QUOTA_ID and (
                quota is None or _is_gemini_promo_usd_quota(quota)
            ):
                # No calendar boundary proves that an unknown paid USD charge
                # was zero; keep this reservation uncertain across rollover.
                continue
            if quota is None:
                continue
            window = _reservation_window_bounds(reservation, quota, quota_id)
            if window is None:
                continue
            window_start, window_end, window_source = window
            grace = _uncertain_window_grace(window_start, window_end)
            if grace is None:
                continue
            grace_ends_at = window_end + grace
            if current < grace_ends_at:
                continue

            raw_value = current_holds.get(quota_id, 0)
            if raw_value is None or isinstance(raw_value, bool):
                continue
            try:
                hold = _decimal(raw_value)
            except Exception:
                continue
            if hold == 0 and quota_id in already_released_pools:
                continue
            reserved_before = _decimal(quota.reserved)
            is_monetary = str(quota.metric_type or "").casefold() == "usd"
            if (
                not hold.is_finite()
                or hold < 0
                or (not is_monetary and hold != hold.to_integral_value())
                or not reserved_before.is_finite()
                or reserved_before < hold
            ):
                # Malformed or already-diverged accounting is not safe to release.
                continue

            try:
                original_hold = _decimal(original_holds.get(quota_id, hold))
            except Exception:
                original_hold = hold
            if not original_hold.is_finite() or original_hold < hold:
                original_hold = hold
            quota.reserved = reserved_before - hold
            current_holds[quota_id] = 0
            released_pools[quota_id] = {
                "metric_type": str(quota.metric_type or ""),
                "original_hold": str(original_hold),
                "hold_released": str(hold),
                "consumed_delta": "0",
                "reserved_before": str(reserved_before),
                "reserved_after": str(quota.reserved),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "grace_ends_at": grace_ends_at.isoformat(),
                "window_source": window_source,
            }

        if released_pools:
            estimate["remaining_quota_reservations"] = current_holds
            reservation.estimated_usage = estimate
            event = {
                "event_id": str(uuid.uuid4()),
                "event_type": "free_team.uncertain_window_hold_released",
                "reason": "uncertain_window_closed",
                "actor_user_id": "system:free-team-expiry-reaper",
                "reservation_id": str(reservation_uuid),
                "released_at": current.isoformat(),
                "actual_usage_status": "unknown",
                "quota_pools": released_pools,
            }
            actual_usage["uncertain_window_release_events"] = [*prior_events, event]
            reservation.actual_usage = actual_usage
            reservation.settlement_version = int(reservation.settlement_version or 0) + 1

        remaining_positive = False
        for value in current_holds.values():
            if value is None or isinstance(value, bool):
                remaining_positive = True
                break
            try:
                amount = _decimal(value)
            except Exception:
                remaining_positive = True
                break
            if not amount.is_finite() or amount > 0:
                remaining_positive = True
                break
        if not remaining_positive:
            all_pool_ids = set(quota_ids)
            released_pool_ids = already_released_pools | set(released_pools)
            # Keep the reservation uncertain until every linked pool has passed
            # its own window rule. This preserves late exact-evidence settlement
            # during grace and leaves windowless holds unresolved by definition.
            remaining_positive = bool(all_pool_ids - released_pool_ids)
        if GEMINI_PROMO_MONTHLY_QUOTA_ID in quota_ids:
            remaining_positive = True
        transitioned = False
        if not remaining_positive and reservation.status != "released":
            reservation.status = "released"
            reservation.error_class = "uncertain_window_closed"
            reservation.finalized_at = current
            if not released_pools:
                reservation.settlement_version = int(reservation.settlement_version or 0) + 1
            transitioned = True
        return bool(released_pools) or transitioned


async def _auto_reconcile_uncertain_openai_reservation(
    reservation_id: Any,
    *,
    session: AsyncSession,
) -> bool:
    """Settle only a unique, exact OpenAI TokenUsage reservation correlation."""

    from ..models.ecc_models import TokenUsage

    reservation_uuid = uuid.UUID(str(reservation_id))
    async with session.begin():
        reservation = (await session.execute(
            select(FreeTeamReservation).where(FreeTeamReservation.id == reservation_uuid)
        )).scalars().first()
        if reservation is None or str(reservation.status or "") != "uncertain":
            return False
        candidate = (await session.execute(
            select(FreeTeamCandidateModel).where(
                FreeTeamCandidateModel.id == reservation.candidate_id
            )
        )).scalars().first()
        if candidate is None or str(candidate.provider or "").casefold() != "openai":
            return False
        evidence_rows = (await session.execute(
            select(
                cast(TokenUsage.id, String).label("id"),
                TokenUsage.provider,
                TokenUsage.model,
                TokenUsage.requested_model,
                TokenUsage.resolved_model,
                TokenUsage.created_at,
                TokenUsage.input_tokens,
                TokenUsage.output_tokens,
                TokenUsage.reasoning_tokens,
                TokenUsage.total_tokens,
            )
            .where(cast(TokenUsage.quota_reservation_id, String) == str(reservation_uuid))
            .order_by(TokenUsage.created_at, TokenUsage.id)
            .limit(2)
        )).mappings().all()
        if len(evidence_rows) != 1:
            return False
        evidence = evidence_rows[0]
        model_values = {
            str(evidence[key] or "").strip().casefold()
            for key in ("model", "requested_model", "resolved_model")
            if evidence[key]
        }
        candidate_model = str(candidate.model or "").strip().casefold()
        if (
            str(evidence["provider"] or "").casefold() != "openai"
            or not candidate_model
            or not model_values
            or model_values != {candidate_model}
            or evidence["created_at"] is None
            or reservation.created_at is None
            or _utc_naive(evidence["created_at"]) < _utc_naive(reservation.created_at)
        ):
            return False
        values = [evidence[key] for key in (
            "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
        )]
        if any(type(value) is not int or value < 0 for value in values):
            return False
        input_tokens, output_tokens, reasoning_tokens, total_tokens = values
        if total_tokens != input_tokens + output_tokens:
            return False
        evidence_ref = str(evidence["id"])

    try:
        await reconcile_uncertain_openai_reservation(
            reservation_uuid,
            actual_usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
            },
            evidence_ref=evidence_ref,
            actor_user_id="system:free-team-expiry-reaper",
            allow_actual_over_hold=True,
            session=session,
        )
        return True
    except FreeTeamReservationReconciliationError:
        return False


def _reservation_quota_429_evidence(
    reservation: FreeTeamReservation,
) -> dict[str, Any] | None:
    """Read only safe, machine-readable quota rejection evidence from a row."""
    containers: list[Mapping[str, Any]] = []
    for source in (reservation.estimated_usage, reservation.actual_usage):
        if not isinstance(source, Mapping):
            continue
        containers.append(source)
        for key in (
            "provider_error",
            "provider_failure",
            "provider_rejection",
            "error",
            "failure",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
    for item in containers:
        status = item.get("http_status", item.get("status_code"))
        proof = quota_429_evidence_from_fields(
            status_code=status,
            code=item.get("code", item.get("error_code")),
            error_type=item.get("type", item.get("error_type")),
        )
        if proof is None:
            # Older records may store the machine code directly in error_class.
            direct = quota_429_evidence_from_fields(
                status_code=429,
                code=reservation.error_class,
                error_type=None,
            )
            proof = direct
        if proof is None:
            continue
        explicit = item.get("provider_rejected_before_processing")
        if explicit is False:
            continue
        return proof
    return None


def _record_unproven_quota_429_review(
    reservation: FreeTeamReservation,
    *,
    reason_code: str,
    now: datetime,
) -> bool:
    existing = reservation.actual_usage if isinstance(reservation.actual_usage, Mapping) else {}
    events = list(existing.get("provider_429_review_events") or [])
    if any(
        isinstance(item, Mapping) and item.get("reason_code") == reason_code
        for item in events
    ):
        return False
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "free_team.reservation_429_retained_without_proof",
        "actor_user_id": "system:free-team-expiry-reaper",
        "reservation_id": str(reservation.id),
        "reason_code": reason_code,
        "error_class": str(reservation.error_class or "")[:40],
        "recorded_at": now.isoformat(),
        "released": False,
    }
    reservation.actual_usage = {
        **dict(existing),
        "provider_429_review_events": [*events, event],
    }
    reservation.settlement_version = int(reservation.settlement_version or 0) + 1
    logger.warning(
        "OpenAI 429 reservation retained without pre-processing proof "
        "(reservation_id=%s reason_code=%s)",
        reservation.id,
        reason_code,
    )
    return True


async def _release_proven_quota_429_reservation(
    reservation_id: Any,
    *,
    session: AsyncSession,
    now: datetime | None = None,
) -> bool:
    """Release only an exact quota/billing 429 with no linked usage row."""
    try:
        reservation_uuid = uuid.UUID(str(reservation_id))
    except (TypeError, ValueError, AttributeError):
        return False
    current = _utc_naive(now or _utcnow())
    from ..models.ecc_models import TokenUsage

    async with session.begin():
        reservation = (
            await session.execute(
                select(FreeTeamReservation)
                .where(FreeTeamReservation.id == reservation_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if reservation is None or str(reservation.error_class or "") not in {
            "429",
            *OPENAI_QUOTA_429_CODES,
        }:
            return False
        if reservation.status not in {"reserved", "uncertain", "committed"}:
            return False
        candidate = (
            await session.execute(
                select(FreeTeamCandidateModel)
                .where(FreeTeamCandidateModel.id == reservation.candidate_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        proof = _reservation_quota_429_evidence(reservation)
        if candidate is None or candidate.provider != "openai":
            _record_unproven_quota_429_review(
                reservation, reason_code="openai_candidate_missing", now=current
            )
            return False
        if proof is None:
            _record_unproven_quota_429_review(
                reservation, reason_code="quota_code_missing_or_transient", now=current
            )
            return False

        linked_usage = (
            await session.execute(
                select(func.count(TokenUsage.id)).where(
                    TokenUsage.quota_reservation_id == reservation_uuid
                )
            )
        ).scalar_one()
        if int(linked_usage or 0) > 0:
            _record_unproven_quota_429_review(
                reservation, reason_code="linked_token_usage_exists", now=current
            )
            return False

        accounting = dict(reservation.estimated_usage or {})
        original_holds = accounting.get("quota_reservations")
        if not isinstance(original_holds, Mapping):
            original_holds = {}
        remaining_holds = accounting.get("remaining_quota_reservations")
        if not isinstance(remaining_holds, Mapping):
            remaining_holds = original_holds
        quota_ids = sorted(
            {
                str(value)
                for value in (
                    reservation.quota_pool_ids or list(original_holds.keys())
                )
                if str(value)
            }
        )
        if (
            not quota_ids
            or any(value not in OPENAI_DAILY_QUOTA_GROUPS for value in quota_ids)
        ):
            _record_unproven_quota_429_review(
                reservation, reason_code="openai_daily_pool_evidence_missing", now=current
            )
            return False
        quotas = {
            str(item.id): item
            for item in (
                await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id.in_(quota_ids))
                    .order_by(FreeTeamQuotaPool.id)
                    .with_for_update()
                )
            ).scalars().all()
        }
        if set(quotas) != set(quota_ids):
            _record_unproven_quota_429_review(
                reservation, reason_code="openai_daily_pool_row_missing", now=current
            )
            return False

        actual = reservation.actual_usage if isinstance(reservation.actual_usage, Mapping) else {}
        quota_actual = actual.get("quota_actual")
        quota_actual = quota_actual if isinstance(quota_actual, Mapping) else {}
        changes: dict[str, dict[str, str]] = {}
        unresolved_reason = ""
        for quota_id in quota_ids:
            quota = quotas[quota_id]
            if str(quota.metric_type or "").casefold() != "total_tokens":
                unresolved_reason = "openai_token_pool_required"
                break
            _reset_quota_if_needed(quota, current)
            window = _reservation_window_bounds(reservation, quota, quota_id)
            if window is None:
                unresolved_reason = "reservation_window_missing"
                break
            window_start, window_end, _source = window
            current_start = _utc_naive(quota.window_start) if quota.window_start else None
            current_window = current_start == window_start
            if not current_window:
                # Daily counters have already rolled over. The old committed
                # amount cannot consume today's pool, so no pool adjustment is
                # needed for this reservation.
                changes[quota_id] = {
                    "metric_type": str(quota.metric_type),
                    "window_end": window_end.isoformat(),
                    "counter_window_current": "false",
                    "reserved_released": "0",
                    "consumed_reversed": "0",
                }
                continue

            raw_hold = remaining_holds.get(quota_id, 0)
            try:
                hold = _decimal(raw_hold)
            except Exception:
                unresolved_reason = "reservation_hold_invalid"
                break
            if not hold.is_finite() or hold < 0 or hold != hold.to_integral_value():
                unresolved_reason = "reservation_hold_invalid"
                break
            consumed_reverse = Decimal(0)
            if reservation.status == "committed" and hold == 0:
                if quota_id not in quota_actual:
                    unresolved_reason = "committed_quota_amount_unproven"
                    break
                try:
                    consumed_reverse = _decimal(quota_actual.get(quota_id))
                except Exception:
                    consumed_reverse = Decimal("NaN")
                if (
                    not consumed_reverse.is_finite()
                    or consumed_reverse < 0
                    or consumed_reverse != consumed_reverse.to_integral_value()
                ):
                    unresolved_reason = "committed_quota_amount_unproven"
                    break
            reserved_before = _decimal(quota.reserved)
            consumed_before = _decimal(quota.consumed)
            if (
                not reserved_before.is_finite()
                or not consumed_before.is_finite()
                or reserved_before < hold
                or consumed_before < consumed_reverse
            ):
                unresolved_reason = "quota_counter_mismatch"
                break
            changes[quota_id] = {
                "metric_type": str(quota.metric_type),
                "window_end": window_end.isoformat(),
                "counter_window_current": "true",
                "reserved_before": str(reserved_before),
                "consumed_before": str(consumed_before),
                "reserved_released": str(hold),
                "consumed_reversed": str(consumed_reverse),
            }

        if unresolved_reason:
            _record_unproven_quota_429_review(
                reservation, reason_code=unresolved_reason, now=current
            )
            return False

        for quota_id, values in changes.items():
            if values["counter_window_current"] != "true":
                continue
            quota = quotas[quota_id]
            quota.reserved = _decimal(quota.reserved) - _decimal(
                values["reserved_released"]
            )
            quota.consumed = _decimal(quota.consumed) - _decimal(
                values["consumed_reversed"]
            )
            values["reserved_after"] = str(quota.reserved)
            values["consumed_after"] = str(quota.consumed)

        accounting["remaining_quota_reservations"] = {}
        reservation.estimated_usage = accounting
        release_event = {
            "event_id": str(uuid.uuid4()),
            "event_type": "free_team.reservation_released_quota_429",
            "actor_user_id": "system:free-team-expiry-reaper",
            "reservation_id": str(reservation_uuid),
            "evidence_ref": "provider_error_code_and_type",
            "recorded_at": current.isoformat(),
            "reason_code": proof["reason_code"],
            "error_code": proof["code"],
            "error_type": proof["type"],
            "actual_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "quota_pools": changes,
        }
        reservation.actual_usage = {
            **dict(actual),
            "provider_rejection_release": release_event,
        }
        reservation.status = "released"
        reservation.error_class = "openai_quota_rejected"
        reservation.finalized_at = current
        reservation.settlement_version = int(reservation.settlement_version or 0) + 1
        return True


async def _auto_reconcile_uncertain_metered_reservation(
    reservation_id: Any,
    *,
    session: AsyncSession,
) -> bool:
    """Settle non-OpenAI period meters from one exact TokenUsage row only."""

    from ..models.ecc_models import TokenUsage

    try:
        reservation_uuid = uuid.UUID(str(reservation_id))
    except (TypeError, ValueError, AttributeError):
        return False

    async with session.begin():
        reservation = (await session.execute(
            select(FreeTeamReservation)
            .where(FreeTeamReservation.id == reservation_uuid)
            .with_for_update()
        )).scalar_one_or_none()
        if reservation is None or str(reservation.status or "") != "uncertain":
            return False
        candidate = (await session.execute(
            select(FreeTeamCandidateModel).where(
                FreeTeamCandidateModel.id == reservation.candidate_id
            )
        )).scalar_one_or_none()
        provider = str(candidate.provider or "").strip().casefold() if candidate else ""
        candidate_model = str(candidate.model or "").strip().casefold() if candidate else ""
        if not provider or provider == "openai" or not candidate_model:
            return False

        evidence_rows = (await session.execute(
            select(
                cast(TokenUsage.id, String).label("id"),
                TokenUsage.provider,
                TokenUsage.model,
                TokenUsage.requested_model,
                TokenUsage.resolved_model,
                TokenUsage.created_at,
                TokenUsage.input_tokens,
                TokenUsage.output_tokens,
                TokenUsage.reasoning_tokens,
                TokenUsage.total_tokens,
                TokenUsage.provider_reported_cost,
            )
            .where(cast(TokenUsage.quota_reservation_id, String) == str(reservation_uuid))
            .order_by(TokenUsage.created_at, TokenUsage.id)
            .limit(2)
        )).mappings().all()
        if len(evidence_rows) != 1:
            return False
        evidence = evidence_rows[0]
        evidence_models = {
            str(evidence[key] or "").strip().casefold()
            for key in ("model", "requested_model", "resolved_model")
            if evidence[key]
        }
        if (
            str(evidence["provider"] or "").strip().casefold() != provider
            or evidence_models != {candidate_model}
            or evidence["created_at"] is None
            or reservation.created_at is None
            or _utc_naive(evidence["created_at"]) < _utc_naive(reservation.created_at)
        ):
            return False

        token_values = [evidence[key] for key in (
            "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
        )]
        if any(type(value) is not int or value < 0 for value in token_values):
            return False
        input_tokens, output_tokens, reasoning_tokens, total_tokens = token_values
        if total_tokens != input_tokens + output_tokens:
            return False

        accounting = dict(reservation.estimated_usage or {})
        original_holds = accounting.get("quota_reservations")
        if not isinstance(original_holds, Mapping) or not original_holds:
            return False
        remaining_holds = accounting.get("remaining_quota_reservations")
        if isinstance(remaining_holds, Mapping):
            hold_values = dict(remaining_holds)
            if not hold_values:
                prior_actual = (
                    dict(reservation.actual_usage)
                    if isinstance(reservation.actual_usage, Mapping)
                    else {}
                )
                prior_release_events = prior_actual.get(
                    "uncertain_window_release_events"
                )
                if (
                    isinstance(prior_release_events, list)
                    and prior_release_events
                ) or (
                    isinstance(prior_actual.get("quota_actual"), Mapping)
                    and prior_actual["quota_actual"]
                ):
                    # An empty map can mean every hold was released or some
                    # meters were already charged; exact evidence must not add
                    # those units a second time.
                    return False
                hold_values = {str(key): 0 for key in original_holds}
            elif {str(key) for key in hold_values} != {
                str(key) for key in original_holds
            }:
                # Do not charge pools already removed from a partial hold map;
                # they may have been settled or released independently.
                return False
        else:
            hold_values = dict(original_holds)
        quota_ids = sorted({str(value) for value in (reservation.quota_pool_ids or [])})
        if not quota_ids or set(quota_ids) != {str(key) for key in original_holds}:
            return False

        quotas_result = await session.execute(
            select(FreeTeamQuotaPool)
            .where(FreeTeamQuotaPool.id.in_(quota_ids))
            .order_by(FreeTeamQuotaPool.id)
            .with_for_update()
        )
        quotas = {str(quota.id): quota for quota in quotas_result.scalars().all()}
        if set(quotas) != set(quota_ids):
            return False

        provider_cost: Decimal | None = None
        if evidence["provider_reported_cost"] is not None:
            try:
                provider_cost = _decimal(evidence["provider_reported_cost"])
            except Exception:
                return False
            if not provider_cost.is_finite() or provider_cost < 0:
                return False

        actual_by_pool: dict[str, Decimal] = {}
        hold_by_pool: dict[str, Decimal] = {}
        for quota_id in quota_ids:
            quota = quotas[quota_id]
            if _reservation_window_bounds(reservation, quota, quota_id) is None:
                # This auto-settlement path is for period meters only. Windowless
                # process concurrency remains under its own lifecycle protocol.
                return False
            metric = str(quota.metric_type or "").casefold()
            if metric in {"requests", "rpm", "rpd"}:
                actual = Decimal(1)
            elif metric in {"total_tokens", "tpm"}:
                actual = Decimal(total_tokens)
            elif metric == "usd" and provider_cost is not None:
                actual = provider_cost
            else:
                # Without a provider-reported cost, list pricing is not proof of
                # consumed promotional credit. Keep that hold uncertain.
                return False
            raw_hold = hold_values.get(quota_id, 0)
            if raw_hold is None or isinstance(raw_hold, bool):
                return False
            try:
                hold = _decimal(raw_hold)
            except Exception:
                return False
            if (
                not hold.is_finite()
                or hold < 0
                or (metric != "usd" and hold != hold.to_integral_value())
                or not _decimal(quota.reserved).is_finite()
                or _decimal(quota.reserved) < hold
            ):
                return False
            hold_by_pool[quota_id] = hold
            actual_by_pool[quota_id] = actual

        now = _utcnow()
        before_after: dict[str, dict[str, str]] = {}
        for quota_id in quota_ids:
            quota = quotas[quota_id]
            hold = hold_by_pool[quota_id]
            actual = actual_by_pool[quota_id]
            consumed_before = _decimal(quota.consumed)
            reserved_before = _decimal(quota.reserved)
            quota.consumed = consumed_before + actual
            quota.reserved = reserved_before - hold
            before_after[quota_id] = {
                "metric_type": str(quota.metric_type or ""),
                "consumed_before": str(consumed_before),
                "reserved_before": str(reserved_before),
                "hold_released": str(hold),
                "actual_consumed": str(actual),
                "consumed_after": str(quota.consumed),
                "reserved_after": str(quota.reserved),
            }

        event = {
            "event_id": str(uuid.uuid4()),
            "event_type": "free_team.reservation_reconciled_from_token_usage",
            "created_at": now.isoformat(),
            "actor_user_id": "system:free-team-expiry-reaper",
            "reservation_id": str(reservation_uuid),
            "evidence_ref": str(evidence["id"]),
            "provider": provider,
            "model": candidate_model,
            "actual_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
                **({"provider_reported_cost": str(provider_cost)} if provider_cost is not None else {}),
            },
            "quota_pools": before_after,
        }
        previous_events = reservation.actual_usage if isinstance(reservation.actual_usage, Mapping) else {}
        reservation.actual_usage = {
            **dict(previous_events),
            "token_usage_reconciliation_event": event,
        }
        accounting["remaining_quota_reservations"] = {}
        accounting["reconciled_quota_reservations"] = {
            quota_id: str(hold_by_pool[quota_id]) for quota_id in quota_ids
        }
        reservation.estimated_usage = accounting
        reservation.status = "committed"
        reservation.error_class = "token_usage_reconciled"
        reservation.finalized_at = now
        reservation.settlement_version = int(reservation.settlement_version or 0) + 1
        return True


async def finalize_route_lease(
    lease: RouteLease,
    *,
    actual_usage: Mapping[str, Any] | None = None,
    success: bool,
    consume_reserved_on_failure: bool = False,
    error_class: str = "",
    latency_ms: float | None = None,
    session: AsyncSession | None = None,
    termination_receipt: Any = None,
    pre_send_evidence: Mapping[str, Any] | None = None,
    quota_rejection_evidence: Mapping[str, Any] | None = None,
    config: Any = None,
) -> None:
    """実績で確定し、不明なwindowed holdは各poolのwindowと猶予中だけ保持する。"""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    now = _utcnow()
    from ..llm.generation_cancellation import CLILeaseTerminationReceipt
    stopped_cli = (isinstance(termination_receipt, CLILeaseTerminationReceipt)
        and getattr(lease, "provider", None) in CLI_PROVIDERS and termination_receipt.authorizes(lease))
    try:
        async with session.begin():
            reservation_result = await session.execute(
                select(FreeTeamReservation)
                .where(FreeTeamReservation.id == lease.reservation_id)
                .with_for_update()
            )
            reservation = reservation_result.scalar_one_or_none()
            if reservation is None or (reservation.status != "reserved" and not (reservation.status == "uncertain" and stopped_cli)):
                return
            retry_uncertain = reservation.status == "uncertain"
            # Claim this settlement before touching shared quota rows. The
            # version also makes cleanup retries idempotent on fixture stores
            # without PostgreSQL row locks.
            version = int(reservation.settlement_version or 0)
            claimed = await session.execute(update(FreeTeamReservation).where(
                FreeTeamReservation.id == reservation.id,
                FreeTeamReservation.settlement_version == version,
                FreeTeamReservation.status == reservation.status,
            ).values(settlement_version=FreeTeamReservation.settlement_version + 1).execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                return
            from sqlalchemy.orm.attributes import set_committed_value
            set_committed_value(reservation, "settlement_version", version + 1)
            candidate_result = await session.execute(
                select(FreeTeamCandidateModel)
                .where(FreeTeamCandidateModel.id == reservation.candidate_id)
                .with_for_update()
            )
            candidate = candidate_result.scalar_one()
            stopped_cli = stopped_cli and candidate.provider == lease.provider and candidate.id == lease.candidate_id
            if pre_send_evidence is not None and quota_rejection_evidence is not None:
                raise ValueError("only_one_zero_usage_release_evidence_is_allowed")
            release_evidence = pre_send_evidence or quota_rejection_evidence
            normalized_quota_rejection = None
            if quota_rejection_evidence is not None:
                normalized_quota_rejection = quota_429_evidence_from_fields(
                    status_code=quota_rejection_evidence.get("http_status"),
                    code=quota_rejection_evidence.get("code"),
                    error_type=quota_rejection_evidence.get("type"),
                )
                if (
                    normalized_quota_rejection is None
                    or quota_rejection_evidence.get("provider_rejected_before_processing") is not True
                    or candidate.provider != "openai"
                    or str(getattr(lease, "provider", "")).casefold() != "openai"
                    or retry_uncertain
                    or success
                    or consume_reserved_on_failure
                ):
                    raise ValueError("quota_rejection_evidence_requires_openai_zero_release")
            if pre_send_evidence is not None and (
                candidate.provider != "openai"
                or str(getattr(lease, "provider", "")).casefold() != "openai"
                or retry_uncertain
                or success
                or consume_reserved_on_failure
                or pre_send_evidence.get("sdk_max_retries") != 0
                or pre_send_evidence.get("httpx_transport_retries") != 0
                or pre_send_evidence.get("request_sent") is not False
            ):
                raise ValueError("pre_send_evidence_requires_openai_release")
            if retry_uncertain and not stopped_cli:
                return
            credential_result = await session.execute(
                select(FreeTeamCredentialProfile)
                .where(
                    FreeTeamCredentialProfile.id
                    == candidate.credential_profile_id
                )
                .with_for_update()
            )
            credential = credential_result.scalar_one_or_none()
            quota_ids = sorted(str(value) for value in (reservation.quota_pool_ids or []))
            quota_result = await session.execute(
                select(FreeTeamQuotaPool)
                .where(FreeTeamQuotaPool.id.in_(quota_ids))
                .order_by(FreeTeamQuotaPool.id)
                .with_for_update()
            )
            quotas = {item.id: item for item in quota_result.scalars().all()}
            estimated = dict((reservation.estimated_usage or {}).get("metrics") or {})
            accounting = dict(reservation.estimated_usage or {})
            reserved = dict(accounting.get("remaining_quota_reservations", accounting.get("quota_reservations")) or {})
            normalized_actual = {}
            for key, value in dict(actual_usage or {}).items():
                if type(value) not in {int, float, Decimal}:
                    continue
                amount = _decimal(value)
                if amount.is_finite() and amount >= 0:
                    normalized_actual[key] = amount
            if release_evidence is not None and (
                any(
                    normalized_actual.get(key) != 0
                    for key in (
                        "input_tokens", "output_tokens", "reasoning_tokens",
                        "total_tokens", "requests",
                    )
                )
            ):
                raise ValueError("zero_usage_release_evidence_requires_zero_usage")
            uncertain = False
            quota_actuals: dict[str, float] = {}
            pre_send_quota_changes: dict[str, dict[str, str]] = {}
            for quota_id in quota_ids:
                quota = quotas.get(quota_id)
                if quota is None:
                    continue
                if quota_id not in reserved:
                    continue
                _reset_quota_if_needed(quota, now)
                consumed_before = _decimal(quota.consumed)
                reserved_before = _decimal(quota.reserved)
                reserved_amount = _decimal(reserved.get(quota_id))
                actual_amount = _amount_for_metric(quota.metric_type, normalized_actual)
                actual_metric = {"tpm": "total_tokens", "rpm": "requests", "rpd": "requests"}.get(quota.metric_type, quota.metric_type)
                concurrency_stopped = stopped_cli and str(quota.metric_type).lower() == "concurrency"
                if candidate.provider in CLI_PROVIDERS and (success or consume_reserved_on_failure) and str(quota.metric_type).lower() == "concurrency" and not concurrency_stopped:
                    uncertain = True
                    continue
                if retry_uncertain and not concurrency_stopped:
                    uncertain = True
                    continue
                if not concurrency_stopped and (success or consume_reserved_on_failure) and (str(quota.metric_type).lower() != "concurrency" or not success) and (
                    actual_metric not in normalized_actual
                    or not actual_amount.is_finite()
                    or actual_amount < 0
                    or (
                        _is_gemini_promo_usd_quota(quota)
                        and actual_amount == 0
                    )
                ):
                    # Neither a timeout nor a crash proves that the provider
                    # stopped. Retain the full reservation even after midnight.
                    uncertain = True
                    continue
                quota.reserved = max(
                    Decimal("0"), _decimal(quota.reserved) - reserved_amount
                )
                reserved.pop(quota_id, None)
                if release_evidence is not None:
                    pre_send_quota_changes[quota_id] = {
                        "consumed_before": str(consumed_before),
                        "reserved_before": str(reserved_before),
                        "hold_released": str(reserved_amount),
                        "actual_consumed": "0",
                        "consumed_after": str(_decimal(quota.consumed)),
                        "reserved_after": str(_decimal(quota.reserved)),
                    }
                if success or consume_reserved_on_failure:
                    actual_amount = _amount_for_metric(quota.metric_type, normalized_actual)
                    if actual_metric not in normalized_actual:
                        actual_amount = _amount_for_metric(quota.metric_type, estimated)
                    if str(quota.metric_type).lower() != "concurrency":
                        # SDK/providerの報告が事前見積りを超えた場合も実値を台帳へ
                        # 記録する。clampするとpromo上限超過を隠してしまう。
                        quota.consumed = _decimal(quota.consumed) + actual_amount
                        quota_actuals[quota_id] = float(actual_amount)

            if release_evidence is not None and set(pre_send_quota_changes) != set(quota_ids):
                raise ValueError("zero_usage_release_evidence_quota_mismatch")

            uncertain_hold_adjustments: dict[str, dict[str, str]] = {}
            if uncertain:
                for quota_id in sorted(reserved):
                    quota = quotas.get(quota_id)
                    if quota is None or _reservation_window_bounds(
                        reservation, quota, quota_id
                    ) is None:
                        continue
                    original = _decimal(reserved.get(quota_id))
                    if not original.is_finite() or original <= 0 or _decimal(quota.reserved) < original:
                        continue
                    retained, policy = await _bounded_uncertain_hold(
                        session,
                        reservation_id=reservation.id,
                        quota=quota,
                        current_hold=original,
                        usage_tier=config_get(config, "openai.usage_tier", None),
                    )
                    if retained >= original:
                        continue
                    released = original - retained
                    quota.reserved = _decimal(quota.reserved) - released
                    if retained == 0:
                        reserved.pop(quota_id, None)
                    else:
                        reserved[quota_id] = float(retained)
                    uncertain_hold_adjustments[quota_id] = {
                        "original_hold": str(original),
                        "retained_hold": str(retained),
                        "released_by_policy": str(released),
                        **policy,
                    }

            accounting["remaining_quota_reservations"] = reserved
            if normalized_quota_rejection is not None:
                accounting["provider_error"] = {
                    **normalized_quota_rejection,
                    "provider": "openai",
                    "recorded_at": now.isoformat(),
                }
            if uncertain_hold_adjustments:
                accounting["uncertain_hold_policy"] = {
                    "applied_at": now.isoformat(),
                    "per_reservation_ratio": str(UNCERTAIN_HOLD_PER_RESERVATION_RATIO),
                    "aggregate_pool_ratio": str(UNCERTAIN_HOLD_POOL_RATIO),
                    "pools": uncertain_hold_adjustments,
                }
            reservation.estimated_usage = accounting
            reservation.status = (
                "uncertain" if uncertain else "committed" if success or consume_reserved_on_failure else "released"
            )
            actual_usage_record = {**dict(reservation.actual_usage or {}), **{
                key: float(value) for key, value in normalized_actual.items()
            }, **({"quota_actual": quota_actuals} if quota_actuals else {})}
            if release_evidence is not None:
                proof = {
                    key: release_evidence[key]
                    for key in (
                        "exception_type",
                        "cause_type",
                        "physical_attempts",
                        "sdk_max_retries",
                        "httpx_transport_retries",
                        "request_sent",
                        "http_status",
                        "code",
                        "type",
                        "reason_code",
                        "provider_rejected_before_processing",
                    )
                    if key in release_evidence
                    and isinstance(release_evidence[key], (str, int, bool))
                }
                is_quota_rejection = normalized_quota_rejection is not None
                actual_usage_record[
                    "provider_rejection_release"
                    if is_quota_rejection
                    else "pre_send_reconciliation"
                ] = {
                    "event_id": str(uuid.uuid4()),
                    "event_type": (
                        "free_team.reservation_released_quota_429"
                        if is_quota_rejection
                        else "free_team.reservation_released_pre_send"
                    ),
                    "actor_user_id": (
                        "system:openai-quota-rejection-classifier"
                        if is_quota_rejection
                        else "system:openai-pre-send-classifier"
                    ),
                    "evidence_ref": str(reservation.id),
                    "recorded_at": now.isoformat(),
                    "proof": proof,
                    "quota_pools": pre_send_quota_changes,
                }
            reservation.actual_usage = actual_usage_record
            reservation.error_class = error_class or None
            reservation.finalized_at = now
            if retry_uncertain:
                pass  # Cleanup does not count the original provider outcome twice.
            elif success:
                candidate.success_count = int(candidate.success_count or 0) + 1
                candidate.consecutive_failures = 0
                candidate.status = "ready"
                candidate.last_success_at = now
                if latency_ms is not None:
                    previous = _decimal(candidate.average_latency_ms)
                    count = max(1, int(candidate.success_count or 1))
                    candidate.average_latency_ms = (
                        previous * Decimal(count - 1) + _decimal(latency_ms)
                    ) / Decimal(count)
            elif error_class not in {"cancelled", "expired"}:
                candidate.failure_count = int(candidate.failure_count or 0) + 1
                candidate.consecutive_failures = int(candidate.consecutive_failures or 0) + 1
                candidate.last_failure_at = now
                policy = dict(candidate.cooldown_policy or {})
                if error_class == "402":
                    candidate.status = "unavailable"
                elif error_class in RETRYABLE_ERROR_CLASSES:
                    seconds = max(5, int(policy.get(error_class) or policy.get("default_seconds") or 60))
                    candidate.cooldown_until = now + timedelta(seconds=seconds)
                    candidate.status = "ready"
                elif error_class in {"401", "403", "auth"}:
                    candidate.status = "needs_attention"
                    if credential is not None and candidate.provider != "openai":
                        # The OpenAI profile row is a legacy FK pointer only;
                        # authentication belongs to the shared normal client.
                        credential.status = "needs_attention"
        lease.decision_receipt = finalize_decision_receipt(
            lease.decision_receipt, outcome="execution_succeeded" if success else "execution_failed",
        )
    finally:
        if own_session:
            await session.close()


async def reconcile_uncertain_openai_reservation(
    reservation_id: str | uuid.UUID,
    *,
    actual_usage: Mapping[str, Any],
    evidence_ref: str | uuid.UUID,
    actor_user_id: str,
    allow_actual_over_hold: bool = False,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Reconcile one OpenAI hold from its exact TokenUsage reservation link.

    The reservation UUID is the idempotency key.  A TokenUsage row is accepted
    only when its quota_reservation_id points at this exact reservation and its
    provider/model/tokens match the submitted usage.  No prompt or credential
    data is read or stored in the reconciliation event.
    """

    try:
        reservation_uuid = uuid.UUID(str(reservation_id))
        evidence_uuid = uuid.UUID(str(evidence_ref))
    except (TypeError, ValueError, AttributeError):
        raise FreeTeamReservationReconciliationError(
            "reconciliation_id_invalid", 422
        ) from None
    actor = str(actor_user_id or "").strip()
    if not actor:
        raise FreeTeamReservationReconciliationError("reconciliation_actor_required", 422)
    if not isinstance(actual_usage, Mapping):
        raise FreeTeamReservationReconciliationError("reconciliation_usage_invalid", 422)
    allowed_usage_keys = {
        "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
    }
    if set(actual_usage) - allowed_usage_keys:
        raise FreeTeamReservationReconciliationError("reconciliation_usage_invalid", 422)
    supplied_usage: dict[str, int] = {}
    for key, value in actual_usage.items():
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise FreeTeamReservationReconciliationError("reconciliation_usage_invalid", 422)
        supplied_usage[str(key)] = value
    if "total_tokens" not in supplied_usage:
        raise FreeTeamReservationReconciliationError("reconciliation_total_required", 422)

    own_session = session is None
    if session is None:
        session = await get_db_session()
    try:
        from ..models.ecc_models import TokenUsage

        async with session.begin():
            reservation_result = await session.execute(
                select(FreeTeamReservation)
                .where(FreeTeamReservation.id == reservation_uuid)
                .with_for_update()
            )
            reservation = reservation_result.scalar_one_or_none()
            if reservation is None:
                raise FreeTeamReservationReconciliationError("reservation_not_found", 404)

            candidate_result = await session.execute(
                select(FreeTeamCandidateModel)
                .where(FreeTeamCandidateModel.id == reservation.candidate_id)
                .with_for_update()
            )
            candidate = candidate_result.scalar_one_or_none()
            if candidate is None or str(candidate.provider or "").casefold() != "openai":
                raise FreeTeamReservationReconciliationError("openai_reservation_required", 409)
            candidate_model = str(candidate.model or "").strip().casefold()

            evidence = (await session.execute(
                select(
                    cast(TokenUsage.id, String).label("id"),
                    TokenUsage.provider,
                    TokenUsage.model,
                    TokenUsage.requested_model,
                    TokenUsage.resolved_model,
                    cast(TokenUsage.quota_reservation_id, String).label("quota_reservation_id"),
                    TokenUsage.created_at,
                    TokenUsage.input_tokens,
                    TokenUsage.output_tokens,
                    TokenUsage.reasoning_tokens,
                    TokenUsage.total_tokens,
                ).where(cast(TokenUsage.id, String) == str(evidence_uuid))
            )).mappings().one_or_none()
            if evidence is None:
                raise FreeTeamReservationReconciliationError("usage_evidence_not_found", 404)
            if (
                str(evidence["provider"] or "").casefold() != "openai"
                or str(evidence["quota_reservation_id"] or "") != str(reservation_uuid)
            ):
                raise FreeTeamReservationReconciliationError("usage_evidence_reservation_mismatch", 409)
            evidence_models = {
                str(evidence[key] or "").strip().casefold()
                for key in ("model", "requested_model", "resolved_model")
                if evidence[key]
            }
            if not candidate_model or not evidence_models or evidence_models != {candidate_model}:
                raise FreeTeamReservationReconciliationError("usage_evidence_model_mismatch", 409)
            if (
                evidence["created_at"] is None
                or reservation.created_at is None
                or _utc_naive(evidence["created_at"]) < _utc_naive(reservation.created_at)
            ):
                raise FreeTeamReservationReconciliationError("usage_evidence_time_mismatch", 409)

            evidence_usage = {
                "input_tokens": int(evidence["input_tokens"] or 0),
                "output_tokens": int(evidence["output_tokens"] or 0),
                "reasoning_tokens": int(evidence["reasoning_tokens"] or 0),
                "total_tokens": int(evidence["total_tokens"] or 0),
            }
            if evidence_usage["total_tokens"] != evidence_usage["input_tokens"] + evidence_usage["output_tokens"]:
                raise FreeTeamReservationReconciliationError("usage_evidence_total_invalid", 409)
            if any(supplied_usage.get(key, value) != value for key, value in evidence_usage.items()):
                raise FreeTeamReservationReconciliationError("reconciliation_usage_evidence_mismatch", 409)

            fingerprint_payload = {
                "reservation_id": str(reservation_uuid),
                "evidence_ref": str(evidence_uuid),
                "actual_usage": evidence_usage,
            }
            fingerprint = hashlib.sha256(
                json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            stored_actual = reservation.actual_usage if isinstance(reservation.actual_usage, dict) else {}
            prior_event = stored_actual.get("reconciliation_event")
            if str(reservation.status or "") == "committed":
                if isinstance(prior_event, dict) and prior_event.get("request_fingerprint") == fingerprint:
                    previous_result = prior_event.get("result")
                    if isinstance(previous_result, dict):
                        return {**previous_result, "idempotent_replay": True}
                raise FreeTeamReservationReconciliationError("reservation_already_settled", 409)
            if str(reservation.status or "") != "uncertain":
                raise FreeTeamReservationReconciliationError("reservation_not_uncertain", 409)

            accounting = dict(reservation.estimated_usage or {})
            holds_value = accounting.get("remaining_quota_reservations")
            if not isinstance(holds_value, Mapping):
                holds_value = accounting.get("quota_reservations")
            elif not holds_value and allow_actual_over_hold:
                # A capped uncertain hold may legitimately be zero while the
                # original reservation still identifies the OpenAI pool(s)
                # whose exact TokenUsage must be charged.
                original_holds = accounting.get("quota_reservations")
                if isinstance(original_holds, Mapping) and original_holds:
                    holds_value = {str(quota_id): 0 for quota_id in original_holds}
            if not isinstance(holds_value, Mapping) or not holds_value:
                raise FreeTeamReservationReconciliationError("reservation_hold_missing", 409)
            hold_ids = sorted(str(key) for key in holds_value)
            if (
                not hold_ids
                or any(quota_id not in OPENAI_DAILY_QUOTA_GROUPS for quota_id in hold_ids)
                or not set(hold_ids).issubset({str(value) for value in (reservation.quota_pool_ids or [])})
            ):
                raise FreeTeamReservationReconciliationError("openai_quota_hold_required", 409)

            quotas_result = await session.execute(
                select(FreeTeamQuotaPool)
                .where(FreeTeamQuotaPool.id.in_(hold_ids))
                .order_by(FreeTeamQuotaPool.id)
                .with_for_update()
            )
            quotas = {str(item.id): item for item in quotas_result.scalars().all()}
            if set(quotas) != set(hold_ids):
                raise FreeTeamReservationReconciliationError("quota_pool_missing", 409)

            actual_total = Decimal(evidence_usage["total_tokens"])
            normalized_holds: dict[str, Decimal] = {}
            for quota_id in hold_ids:
                quota = quotas[quota_id]
                if str(quota.metric_type or "").casefold() != "total_tokens":
                    raise FreeTeamReservationReconciliationError("openai_token_quota_required", 409)
                hold = _decimal(holds_value[quota_id])
                if (
                    not hold.is_finite()
                    or hold < 0
                    or (hold == 0 and not allow_actual_over_hold)
                    or hold != hold.to_integral_value()
                ):
                    raise FreeTeamReservationReconciliationError("reservation_hold_invalid", 409)
                if actual_total > hold and not allow_actual_over_hold:
                    raise FreeTeamReservationReconciliationError("actual_usage_exceeds_hold", 422)
                if _decimal(quota.reserved) < hold:
                    raise FreeTeamReservationReconciliationError("reservation_hold_mismatch", 409)
                normalized_holds[quota_id] = hold

            settlement_version = int(reservation.settlement_version or 0)
            claim = await session.execute(
                update(FreeTeamReservation)
                .where(
                    FreeTeamReservation.id == reservation_uuid,
                    FreeTeamReservation.status == "uncertain",
                    FreeTeamReservation.settlement_version == settlement_version,
                )
                .values(settlement_version=FreeTeamReservation.settlement_version + 1)
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                raise FreeTeamReservationReconciliationError("reservation_settlement_conflict", 409)

            before_after: dict[str, dict[str, str]] = {}
            quota_actuals: dict[str, int] = {}
            for quota_id in hold_ids:
                quota = quotas[quota_id]
                consumed_before = _decimal(quota.consumed)
                reserved_before = _decimal(quota.reserved)
                hold = normalized_holds[quota_id]
                quota.consumed = consumed_before + actual_total
                quota.reserved = reserved_before - hold
                before_after[quota_id] = {
                    "consumed_before": str(consumed_before),
                    "reserved_before": str(reserved_before),
                    "hold_released": str(hold),
                    "actual_consumed": str(actual_total),
                    "unreserved_consumption": str(max(Decimal(0), actual_total - hold)),
                    "consumed_after": str(quota.consumed),
                    "reserved_after": str(quota.reserved),
                }
                quota_actuals[quota_id] = evidence_usage["total_tokens"]

            now = _utcnow()
            event_result = {
                "reservation_id": str(reservation_uuid),
                "status": "committed",
                "evidence_ref": str(evidence_uuid),
                "idempotency_key": str(reservation_uuid),
                "actual_usage": evidence_usage,
                "quota_pools": before_after,
            }
            reconciliation_event = {
                "event_id": str(uuid.uuid4()),
                "event_type": "free_team.reservation_reconciled",
                "created_at": now.isoformat(),
                "actor_user_id": actor,
                "reservation_id": str(reservation_uuid),
                "evidence_ref": str(evidence_uuid),
                "idempotency_key": str(reservation_uuid),
                "request_fingerprint": fingerprint,
                "previous_finalized_at": (
                    reservation.finalized_at.isoformat() if reservation.finalized_at else None
                ),
                "quota_pools": before_after,
                "actual_usage": evidence_usage,
                "result": event_result,
            }
            reconciled_holds = {key: str(value) for key, value in normalized_holds.items()}
            accounting["remaining_quota_reservations"] = {}
            accounting["reconciled_quota_reservations"] = reconciled_holds
            reservation.estimated_usage = accounting
            reservation.actual_usage = {
                **stored_actual,
                **evidence_usage,
                "requests": 1,
                "quota_actual": quota_actuals,
                "reconciliation_event": reconciliation_event,
            }
            reservation.status = "committed"
            reservation.finalized_at = now
            reservation.settlement_version = settlement_version + 1
            result = {**event_result, "idempotent_replay": False}
        return result
    finally:
        if own_session:
            await session.close()


async def release_expired_reservations(
    *, session: AsyncSession | None = None, config: Any = None
) -> int:
    """Finalize expired leases, then reconcile or release bounded unknown windowed holds."""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    now = _utcnow()
    released = 0
    try:
        reserved_cursor_created_at: datetime | None = None
        reserved_cursor_id: uuid.UUID | None = None
        while True:
            async with session.begin():
                statement = select(
                    FreeTeamReservation.id.label("id"),
                    FreeTeamReservation.created_at.label("created_at"),
                ).where(
                    FreeTeamReservation.status == "reserved",
                    FreeTeamReservation.expires_at <= now,
                )
                if reserved_cursor_created_at is not None and reserved_cursor_id is not None:
                    statement = statement.where(or_(
                        FreeTeamReservation.created_at > reserved_cursor_created_at,
                        and_(
                            FreeTeamReservation.created_at == reserved_cursor_created_at,
                            FreeTeamReservation.id > reserved_cursor_id,
                        ),
                    ))
                reserved_batch = (await session.execute(
                    statement.order_by(
                        FreeTeamReservation.created_at,
                        FreeTeamReservation.id,
                    ).limit(500)
                )).all()
            if not reserved_batch:
                break

            for row in reserved_batch:
                if await _release_proven_quota_429_reservation(
                    row.id, session=session, now=now
                ):
                    released += 1
                    continue
                lease = RouteLease(
                    reservation_id=row.id,
                    provider="",
                    model="",
                    credential_profile_id="",
                    candidate_id="",
                    quota_pool_ids=(),
                    effort="",
                    routing_profile_id=FREE_TEAM_PROFILE_ID,
                    pool_id="",
                    max_output_tokens=1,
                    timeout_seconds=1,
                )
                await finalize_route_lease(
                    lease,
                    success=False,
                    consume_reserved_on_failure=True,
                    error_class="expired",
                    config=config,
                    session=session if not own_session else None,
                )
                released += 1
            reserved_cursor_created_at = reserved_batch[-1].created_at
            reserved_cursor_id = reserved_batch[-1].id

        cursor_created_at: datetime | None = None
        cursor_reservation_id: uuid.UUID | None = None
        while True:
            async with session.begin():
                statement = select(
                    FreeTeamReservation.id.label("id"),
                    FreeTeamReservation.created_at.label("created_at"),
                ).where(
                    FreeTeamReservation.status == "uncertain",
                    FreeTeamReservation.expires_at <= now,
                )
                if cursor_created_at is not None and cursor_reservation_id is not None:
                    statement = statement.where(or_(
                        FreeTeamReservation.created_at > cursor_created_at,
                        and_(
                            FreeTeamReservation.created_at == cursor_created_at,
                            FreeTeamReservation.id > cursor_reservation_id,
                        ),
                    ))
                batch = (await session.execute(
                    statement.order_by(
                        FreeTeamReservation.created_at,
                        FreeTeamReservation.id,
                    ).limit(500)
                )).all()
            if not batch:
                break

            for row in batch:
                reservation_id = row.id
                if await _release_proven_quota_429_reservation(
                    reservation_id, session=session, now=now
                ):
                    released += 1
                    cursor_created_at = row.created_at
                    cursor_reservation_id = row.id
                    continue
                reconciled = await _auto_reconcile_uncertain_openai_reservation(
                    reservation_id, session=session
                )
                if not reconciled:
                    reconciled = await _auto_reconcile_uncertain_metered_reservation(
                        reservation_id, session=session
                    )
                if not reconciled:
                    window_released = await _release_closed_window_uncertain_reservation(
                        reservation_id,
                        session=session,
                        now=now,
                    )
                    if window_released:
                        released += 1
                    else:
                        async with session.begin():
                            await _cap_uncertain_reservation_holds(
                                reservation_id,
                                session=session,
                                usage_tier=config_get(
                                    config, "openai.usage_tier", None
                                ),
                            )
                            # This session factory disables autoflush; make each capped
                            # hold visible before calculating the next aggregate cap.
                            await session.flush()
            cursor_created_at = batch[-1].created_at
            cursor_reservation_id = batch[-1].id

        # Older versions could mark an empty 429 response committed after
        # charging its estimate. Reopen only rows with explicit quota/billing
        # code evidence; a bare error_class=429 is retained and audited.
        committed_cursor_created_at: datetime | None = None
        committed_cursor_id: uuid.UUID | None = None
        while True:
            async with session.begin():
                statement = select(
                    FreeTeamReservation.id.label("id"),
                    FreeTeamReservation.created_at.label("created_at"),
                ).where(
                    FreeTeamReservation.status == "committed",
                    FreeTeamReservation.error_class.in_(
                        {"429", *OPENAI_QUOTA_429_CODES}
                    ),
                    FreeTeamReservation.expires_at <= now,
                )
                if committed_cursor_created_at is not None and committed_cursor_id is not None:
                    statement = statement.where(or_(
                        FreeTeamReservation.created_at > committed_cursor_created_at,
                        and_(
                            FreeTeamReservation.created_at == committed_cursor_created_at,
                            FreeTeamReservation.id > committed_cursor_id,
                        ),
                    ))
                committed_batch = (await session.execute(
                    statement.order_by(
                        FreeTeamReservation.created_at,
                        FreeTeamReservation.id,
                    ).limit(500)
                )).all()
            if not committed_batch:
                break
            for row in committed_batch:
                if await _release_proven_quota_429_reservation(
                    row.id, session=session, now=now
                ):
                    released += 1
                committed_cursor_created_at = row.created_at
                committed_cursor_id = row.id
        return released
    finally:
        if own_session:
            await session.close()


async def get_free_team_state(
    *,
    session: AsyncSession | None = None,
    include_quotas: bool = True,
    include_reservations: bool = True,
    config: Any = None,
) -> dict[str, Any]:
    """秘密を除いた設定・候補・クォータ・台帳状態を返す。"""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    try:
        from . import openai_confirmation_service as confirmation
        async with session.begin():
            _, _, current_config = await confirmation.config_row(session)
            await seed_free_team_defaults(session)
            if include_quotas:
                for quota in (await session.execute(select(FreeTeamQuotaPool).order_by(FreeTeamQuotaPool.id).with_for_update())).scalars():
                    _reset_quota_if_needed(quota, _utcnow())
        credentials = list(
            (
                await session.execute(
                    select(FreeTeamCredentialProfile).order_by(
                        FreeTeamCredentialProfile.id
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates = list(
            (
                await session.execute(
                    select(FreeTeamCandidateModel).order_by(
                        FreeTeamCandidateModel.priority,
                        FreeTeamCandidateModel.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        quotas = (
            list(
                (
                    await session.execute(
                        select(FreeTeamQuotaPool).order_by(FreeTeamQuotaPool.id)
                    )
                )
                .scalars()
                .all()
            )
            if include_quotas
            else []
        )
        reservations = (
            list(
                (
                    await session.execute(
                        select(FreeTeamReservation)
                        .order_by(FreeTeamReservation.created_at.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            if include_reservations
            else []
        )
        safe_credentials = []
        for item in credentials:
            if item.id == "openai-complimentary":
                # Keep the legacy FK row out of public settings/status. It is
                # not a credential input or an independent identity.
                continue
            data = item.to_safe_dict()
            data["configured"] = _credential_configured(item, config)
            if item.id == "openrouter-free":
                data["credential_source"] = openrouter_credential_source(config)
            safe_credentials.append(data)
        verification_reason = complimentary_stop_reason(config)
        if confirmation.managed(current_config):
            config = current_config
            verification_reason = await confirmation.binding_stop_reason(session, config) or complimentary_stop_reason(config)
        candidate_projection = []
        for item in candidates:
            candidate_data = item.to_dict()
            if candidate_data.get("credential_profile_id") == "openai-complimentary":
                candidate_data["credential_profile_id"] = "openai_api_key"
            candidate_projection.append(candidate_data)
        state = {
            "credentials": safe_credentials,
            "candidates": candidate_projection,
            "verification": {"status": "unverified" if verification_reason else "verified",
                "stop_reason": verification_reason,
                "external_usage_visibility": "local_and_manual_observation"},
            **provider_circuit_status(config),
        }
        if include_quotas:
            usage_tier = config_get(config, "openai.usage_tier", None)
            openrouter_free_tier = free_team_profile(config).get(
                "openrouter_free_tier", OPENROUTER_DEFAULT_FREE_DAILY_TIER
            )
            usage_actual_groups: dict[str, dict[str, Any]] = {}
            unknown_usage_models: list[dict[str, Any]] = []
            quota_projection = [
                item.to_dict(
                    openai_usage_tier=usage_tier,
                    openrouter_free_tier=openrouter_free_tier,
                )
                for item in quotas
            ]
            for item in quota_projection:
                if item.get("credential_profile_id") == "openai-complimentary":
                    item["credential_profile_id"] = "openai_api_key"
            state["quota_pools"] = quota_projection
            scope = str(config_get(config, "openai.billing_scope_id", "") or "")
            for model, quota in zip(quotas, state["quota_pools"]):
                if model.id not in OPENAI_DAILY_QUOTA_GROUPS:
                    continue
                central = await _central_unmetered_usage(session, scope, OPENAI_DAILY_QUOTA_GROUPS[model.id], _utcnow()) if scope else Decimal(0)
                balance = _openai_quota_snapshot(
                    model, config, central_usage=central
                )
                group = OPENAI_DAILY_QUOTA_GROUPS[model.id]
                display_group = "sol" if group == "1m" else "luna"
                from .pricing.free_tier import free_tier_limit_for_group

                official_limit = free_tier_limit_for_group(group, usage_tier)
                actual_group = balance.get("actual_usage_group")
                actual_last_observed = balance.get("actual_usage_last_observed")
                external_usage_estimate = balance.get("external_usage_estimate")
                usage_actual_groups[display_group] = {
                    "group": group,
                    "actual_usage": (
                        int(actual_last_observed)
                        if actual_last_observed is not None
                        else None
                    ),
                    "input_tokens": (
                        int(actual_group["input_tokens"])
                        if isinstance(actual_group, Mapping)
                        else None
                    ),
                    "output_tokens": (
                        int(actual_group["output_tokens"])
                        if isinstance(actual_group, Mapping)
                        else None
                    ),
                    "input_cached_tokens": (
                        int(actual_group["input_cached_tokens"])
                        if isinstance(actual_group, Mapping)
                        else None
                    ),
                    "requests": (
                        int(actual_group["requests"])
                        if isinstance(actual_group, Mapping)
                        else None
                    ),
                    "limit": int(official_limit),
                    "effective_cap": int(balance["cap"]),
                    "ratio": (
                        round(int(actual_last_observed) / official_limit, 6)
                        if actual_last_observed is not None and official_limit > 0
                        else None
                    ),
                    "source": balance["actual_usage_source"],
                    "actual_usage_state": balance["actual_usage_state"],
                    "last_synced_at": balance["actual_usage_last_synced_at"],
                    "external_usage_estimate": (
                        int(external_usage_estimate)
                        if external_usage_estimate is not None
                        else None
                    ),
                    "unavailable_margin": float(balance["unavailable_margin"]),
                }
                if not unknown_usage_models:
                    unknown_usage_models = list(balance.get("unknown_models") or [])[:50]
                # Keep the persisted setting separate from the live Tier guard.
                # daily_cap remains the effective value for existing consumers.
                quota.update(daily_cap=float(balance["cap"]), effective_daily_cap=float(balance["cap"]),
                    cap_restriction_reason=(
                        "usage_tier"
                        if balance["cap"] < model.configured_daily_cap
                        else ""
                    ),
                    usage_tier=str(config_get(config, "openai.usage_tier", "") or ""),
                    local_consumed=float(balance["local_consumed"]),
                    consumed=float(balance["consumed"]), central_unmetered_usage=float(balance["central_usage"]),
                    provider_external_delta=float(balance["provider_sync_delta"]),
                    available=float(balance["available"]))
                if model.status == "exhausted" and balance["available"] > 0:
                    quota["status"] = "active"
                credential = next((item for item in credentials if item.id == model.credential_profile_id), None)
                quota["stop_reason"] = verification_reason or (
                    "provider_disabled" if not credential else
                    "billing_scope_unmapped_usage" if await _unmapped_central_usage(session, OPENAI_DAILY_QUOTA_GROUPS[model.id], _utcnow()) else
                    "billing_scope_mismatch" if scope and model.reset_policy.get("billing_scope_id") not in {None, scope} else
                    "billing_scope_unbound_usage" if not model.reset_policy.get("billing_scope_id") and (model.consumed or model.reserved or model.provider_observed_usage) else
                    quota["status"] if quota["status"] != "active" else "budget_exhausted" if quota["available"] <= 0 else "")
            for model, projection in zip(quotas, state["quota_pools"]):
                if model.id != GEMINI_PROMO_MONTHLY_QUOTA_ID:
                    continue
                try:
                    promo_balance = await _gemini_promo_quota_snapshot(
                        session, model, _utcnow()
                    )
                except FreeTeamUnavailableError:
                    projection["available"] = 0.0
                    projection["stop_reason"] = "gemini_promo_ledger_unverified"
                else:
                    projection["available"] = float(promo_balance["available"])
                    projection["external_usage_estimate"] = float(
                        promo_balance["cross_route"]["amount"]
                    )
                    projection["stop_reason"] = (
                        "budget_exhausted"
                        if promo_balance["available"] <= 0
                        else ""
                    )
            state["openai_usage_actual"] = {
                "groups": usage_actual_groups,
                "unknown_models": unknown_usage_models,
            }
        if include_reservations:
            state["reservations"] = [item.to_dict() for item in reservations]
        return state
    finally:
        if own_session:
            await session.close()
