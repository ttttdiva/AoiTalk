"""無料Teamの設定、候補選択、原子的クォータ予約を管理する。"""

from __future__ import annotations

import asyncio
import os
import hashlib
import inspect
import json
import uuid
from fnmatch import fnmatchcase
from dataclasses import dataclass, field
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select, text, update
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
from .agent_team_service import config_get
from .free_team_defaults import free_team_profile_template
from .decision_receipt_service import DecisionReceipt, finalize_decision_receipt


ROUTING_PROFILE_PROVIDER = "routing-profile"
FREE_TEAM_PROFILE_ID = "free-team"
FREE_TEAM_MODEL_ID = "free-team"
ALLOWED_BILLING_MODES = {
    "complimentary",
    "free_tier",
    "promo_credit",
    "subscription_cli",
}
OPENAI_BILLING_MODES = {"paid", "complimentary"}
RETRYABLE_ERROR_CLASSES = {"429", "402", "5xx", "timeout", "connection"}
CLI_PROVIDERS = {"codex-cli", "antigravity-cli", "grok-cli", "claude-cli"}


class FreeTeamUnavailableError(RuntimeError):
    """安全に予約できる無料候補がない。"""


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
            "credential_profile": self.credential_profile_id,
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
    selects the complimentary incentive.  Free Team remains a complimentary
    route because its candidate credentials and shared quota are owned by that
    profile, even when an older config has no ``openai.billing_mode`` key.

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
    # ``free-team`` owns its own complimentary credential/quota semantics.  A
    # stale global paid setting must not disable those guards for a pool route.
    if mode == "paid" and free_team_selected(config):
        return "complimentary"
    return mode


def free_team_profile(config: Any) -> dict[str, Any]:
    raw = config_get(config, f"routing_profiles.{FREE_TEAM_PROFILE_ID}", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    profile = free_team_profile_template()
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
        elif key == "llm_profiles" and isinstance(value, dict):
            # Free Team may overlay the target of an existing canonical LLM
            # Profile, but it must not copy or replace the Team/Subagent graph.
            merged_profiles = dict(profile["llm_profiles"])
            for profile_id, stored_profile in value.items():
                if not isinstance(stored_profile, dict):
                    continue
                profile_key = str(profile_id).strip()
                if not profile_key:
                    continue
                merged_profiles[profile_key] = {
                    **dict(merged_profiles.get(profile_key) or {}),
                    **stored_profile,
                    "profile_id": str(
                        stored_profile.get("profile_id") or profile_key
                    ),
                    "routing_profile_id": str(
                        stored_profile.get("routing_profile_id")
                        or FREE_TEAM_PROFILE_ID
                    ),
                }
            profile["llm_profiles"] = merged_profiles
        elif key in {"agent_team", "agent_team_enabled"}:
            # A v2 routing overlay is migration input only.  Never expose it
            # back to runtime or persist it as part of the Free Team profile.
            continue
        else:
            profile[key] = value
    return profile


def free_team_llm_profile(
    config: Any, profile_id: str | None
) -> dict[str, Any] | None:
    """Return one Free Team profile overlay without touching topology."""

    clean_id = str(profile_id or "").strip()
    if not clean_id:
        return None
    value = free_team_profile(config).get("llm_profiles", {}).get(clean_id)
    return dict(value) if isinstance(value, dict) else None


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
        selected_profile = free_team_llm_profile(config, llm_profile_id)
    # Legacy callers may still pass a group mapping at the migration boundary.
    if selected_profile is None and isinstance(group, Mapping):
        selected_profile = group
    selected_profile = selected_profile if isinstance(selected_profile, Mapping) else {}
    if str(selected_profile.get("target_type") or "").strip().lower() != "pool":
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
        "openai-complimentary": "OPENAI_FREE_TEAM_API_KEY",
        "gemini-free": "GEMINI_FREE_API_KEY",
        "gemini-promo": "GEMINI_PROMO_API_KEY",
        "openrouter-free": "OPENROUTER_FREE_API_KEY",
    }.get(profile_id)


def _credential_api_key(profile: FreeTeamCredentialProfile) -> str:
    value = str(profile.api_key or "").strip()
    if value:
        return value
    env_name = _credential_env_name(profile.id)
    return str(os.getenv(env_name, "") if env_name else "").strip()


def _credential_configured(profile: FreeTeamCredentialProfile) -> bool:
    if profile.authentication_type == "cli":
        return bool(profile.cli_auth_reference) or profile.provider in {
            "codex-cli",
            "antigravity-cli",
            "grok-cli",
        }
    return bool(_credential_api_key(profile))


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
    return _decimal(usage.get(metric))


def _candidate_in_pool(candidate_id: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(candidate_id, str(pattern)) for pattern in patterns)


def _candidate_matches(
    candidate: FreeTeamCandidateModel,
    credential: FreeTeamCredentialProfile,
    *,
    candidate_patterns: tuple[str, ...],
    required_capabilities: set[str],
    now: datetime,
) -> bool:
    from ..llm.deployment_resolver import is_retired_model
    if is_retired_model(candidate.model):
        return False
    if not candidate.enabled or not credential.enabled:
        return False
    if credential.status not in {"ready", "active"}:
        return False
    if credential.billing_mode not in ALLOWED_BILLING_MODES:
        return False
    if credential.allow_paid_overage:
        # 無料Teamでは有料超過を許可した認証も自動選択しない。
        return False
    if credential.billing_mode == "complimentary" and not candidate.quota_pool_ids:
        return False
    if not _credential_configured(credential):
        return False
    if candidate.status not in {"ready", "active"}:
        return False
    if candidate.cooldown_until and candidate.cooldown_until > now:
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
            credential.billing_mode == "complimentary"
            and policy.get("complimentary_tool_calls_allowed") is not True
        ):
            return False
    if candidate.provider == "openrouter":
        options = dict(candidate.provider_options or {})
        if options.get("max_price") not in (0, 0.0, "0", "0.0"):
            return False
        paid_fallback_disabled = (
            options.get("paid_fallback_disabled") is True
            or options.get("allow_fallbacks") is False
        )
        if not paid_fallback_disabled or options.get("require_free") is not True:
            return False
    return True


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
    }.get(str(candidate.credential_profile.billing_mode), 9)
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


def _reset_quota_if_needed(quota: FreeTeamQuotaPool, now: datetime) -> None:
    now = _utc_naive(now)
    if not quota.window_end or quota.window_end > now:
        return
    policy = dict(quota.reset_policy or {})
    quota.consumed = Decimal("0")
    # In-flight requests can finish in either UTC day. Carry their full hold
    # into the new window; an old finalizer must never subtract a new hold.
    quota.provider_observed_usage = Decimal("0")
    quota.provider_sync_state = None
    quota.provider_sync_version = int(quota.provider_sync_version or 0) + 1
    quota.last_provider_sync_at = None
    quota.window_start = now
    quota.window_end = _next_window_end(policy, now)
    if quota.status == "exhausted":
        quota.status = "active"


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
                enabled=True,
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
            amount = _decimal((item.actual_usage or {}).get(quota.metric_type))
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
    if observed <= previous and (state or previous > 0):
        return  # Duplicate/out-of-order snapshots can never erase a hold.
    external = quota.provider_sync_delta
    guard_overlap = central_overlap = Decimal(0)
    if included_through is not None and quota.id in OPENAI_DAILY_QUOTA_GROUPS:
        if not scope or (quota.reset_policy or {}).get("billing_scope_id") != scope:
            raise FreeTeamUnavailableError("billing_scope_unverified")
        guard_overlap = await _observed_guard_overlap(session, quota, included_through)
        central_overlap = await _central_unmetered_usage(session, scope, OPENAI_DAILY_QUOTA_GROUPS[quota.id], now,
            included_through=included_through)
    external = max(external, observed - guard_overlap - central_overlap, Decimal(0))
    next_state = {"schema_version": 1, "observed_total": str(observed), "external_delta": str(external),
        "guard_watermark": str(guard_overlap), "central_watermark": str(central_overlap),
        "included_through": included_through.isoformat() if included_through else None,
        "utc_day": now.date().isoformat()}
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
    from .pricing.free_tier import free_tier_limits_for
    tier = config_get(config, "openai.usage_tier", "")
    if tier not in {"tier_1_2", "tier_3_plus"}:
        return quota.daily_cap
    limit = Decimal(free_tier_limits_for(tier)[OPENAI_DAILY_QUOTA_GROUPS[quota.id]])
    return min(quota.daily_cap, max(Decimal(0), limit - OPENAI_SAFETY_TOKENS))


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
    # Media URLs/file IDs have remote token cost not bounded by their string
    # length. Only textual payloads and local function schemas are supported.
    def check(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") in {"image_url", "input_image", "input_audio", "input_file", "file", "audio", "video", "item_reference"}:
                raise FreeTeamUnavailableError("complimentary_media_unbounded")
            for item in value.values():
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check(item)
        elif value is not None and type(value) not in {str, int, float, bool}:
            raise FreeTeamUnavailableError("complimentary_payload_unsupported")
    check(payload)
    if payload.get("extra_headers") or payload.get("extra_query"):
        raise FreeTeamUnavailableError("complimentary_credential_override")
    extra_body = payload.get("extra_body") or {}
    if not isinstance(extra_body, dict) or set(extra_body) - {"prompt_cache_key", "prompt_cache_retention"}:
        raise FreeTeamUnavailableError("complimentary_wire_override")
    tools = payload.get("tools") or []
    if any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools):
        raise FreeTeamUnavailableError("complimentary_billable_tool")
    input_bound = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # Byte upper bound plus protocol framing. The output hard limit already
    # includes reasoning; cached inputs and reasoning are not added twice.
    input_bound += 64 * (1 + len(payload.get("messages") or payload.get("input") or []))
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
    quota_id = next(key for key, value in OPENAI_DAILY_QUOTA_GROUPS.items() if value == group)
    scope = str(config_get(config, "openai.billing_scope_id"))
    own_session = session is None
    if own_session:
        from .token_tracking_service import _database_ready_for_usage
        if not _database_ready_for_usage():
            raise FreeTeamUnavailableError("usage_ledger_not_ready")
    session = session or await get_db_session()
    try:
        async with session.begin():
            try:
                current_config = await confirmation.authoritative_config(session)
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
            quota = (await session.execute(select(FreeTeamQuotaPool).where(
                FreeTeamQuotaPool.id == quota_id
            ).with_for_update())).scalar_one()
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
            if credential is None or not credential.enabled or credential.allow_paid_overage or credential.status not in {"ready", "active"}:
                raise FreeTeamUnavailableError("provider_disabled")
            # Import only here so ordinary routing fixtures need no usage DB.
            if await _unmapped_central_usage(session, group, now):
                raise FreeTeamUnavailableError("billing_scope_unmapped_usage")
            central = await _central_unmetered_usage(session, scope, group, now)
            # Local guard consumption and pre-existing/unmetered central usage
            # are disjoint. Observation's fixed external adjustment cannot
            # shrink as new local requests settle.
            local_used = _decimal(quota.consumed) + _decimal(central)
            used = local_used + quota.provider_sync_delta
            cap = _openai_effective_cap(quota, config)
            amount = estimates["total_tokens"]
            if quota.status != "active" or used + _decimal(quota.reserved) + amount > cap:
                raise FreeTeamUnavailableError("budget_insufficient")
            quota.reset_policy = {**policy, "billing_scope_id": scope}
            previous_reserved = _decimal(quota.reserved)
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
                    "model": model, "window_start": now.isoformat()},
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
) -> Any:
    """Quota ownership only; canonical usage persistence stays with callers."""
    model = str(model or "")
    is_openai = provider.strip().lower() == "openai"
    billing_mode = openai_billing_mode(config, model) if is_openai else "paid"
    # Paid OpenAI calls stay inside the already-approved privacy transaction and
    # return to the ordinary sender.  Token/rate-card attribution is performed
    # by the existing caller after the provider response; this helper owns only
    # the complimentary shared quota when that mode is selected.
    guarded = is_openai and billing_mode == "complimentary"
    if not guarded:
        result = sender(payload)
        return await result if inspect.isawaitable(result) else result
    if not isinstance(payload, Mapping):
        raise FreeTeamUnavailableError("complimentary_payload_unsupported")
    lease = await acquire_openai_request_lease(payload, config=config, model=model, identity=identity, session=session)
    try:
        result = sender(payload)
        if inspect.isawaitable(result):
            result = await result
    except BaseException:
        await asyncio.shield(finalize_route_lease(lease, success=False,
            consume_reserved_on_failure=True, error_class="usage_unknown", session=session))
        raise
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
    await asyncio.shield(finalize_route_lease(lease, actual_usage=usage if complete else None,
        success=True, session=session))
    # Preserve canonical attribution while marking this usage as already
    # included in the guard ledger, so reconciliation cannot charge it twice.
    marker = {"quota_reservation_id": str(lease.reservation_id),
              "billing_scope_id": lease.billing_scope_id}
    if isinstance(raw, dict):
        raw.update(marker)
    elif raw is not None:
        for key, value in marker.items():
            setattr(raw, key, value)
    return result


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
            candidates = [
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
                )
                and _cli_quota_contract_safe(item, quota_snapshot)
            ]
            candidates.sort(
                key=lambda item: _candidate_score(item, now, quota_snapshot)
            )

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

        for candidate_id in ordered_candidate_ids:
            lease: RouteLease | None = None
            async with session.begin():
                locked_result = await session.execute(
                    select(FreeTeamCandidateModel)
                    .where(
                        FreeTeamCandidateModel.id == candidate_id,
                        FreeTeamCandidateModel.enabled.is_(True),
                    )
                    .options(
                        selectinload(FreeTeamCandidateModel.credential_profile)
                    )
                    .with_for_update(skip_locked=True)
                )
                candidate = locked_result.scalar_one_or_none()
                if (
                    candidate is None
                    or candidate.credential_profile is None
                    or not _candidate_matches(
                        candidate,
                        candidate.credential_profile,
                        candidate_patterns=intent.candidate_ids,
                        required_capabilities=capabilities,
                        now=now,
                    )
                ):
                    continue
                options = dict(candidate.provider_options or {})
                concurrency_limit = max(1, int(options.get("concurrency_limit") or 64))
                if await _active_candidate_reservations(session, candidate.id, now) >= concurrency_limit:
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
                    continue
                quota_ids = sorted({str(value) for value in (candidate.quota_pool_ids or [])})
                quota_result = await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id.in_(quota_ids))
                    .order_by(FreeTeamQuotaPool.id)
                    .with_for_update()
                )
                quota_by_id = {item.id: item for item in quota_result.scalars().all()}
                if len(quota_by_id) != len(quota_ids):
                    continue
                reservations: dict[str, Decimal] = {}
                can_reserve = True
                for quota_id in quota_ids:
                    quota = quota_by_id[quota_id]
                    _reset_quota_if_needed(quota, now)
                    if quota.status != "active":
                        can_reserve = False
                        break
                    amount = _amount_for_metric(quota.metric_type, estimates)
                    if amount > quota.available:
                        can_reserve = False
                        break
                    # Official OpenAI requests are metered at the privacy
                    # gateway's exact wire boundary, including every retry.
                    # This outer lease only selects the target.
                    if not (meter_per_request and candidate.provider == "openai"):
                        reservations[quota_id] = amount
                if not can_reserve:
                    continue

                for quota_id, amount in reservations.items():
                    quota_by_id[quota_id].reserved = _decimal(
                        quota_by_id[quota_id].reserved
                    ) + amount
                candidate.selection_count = int(candidate.selection_count or 0) + 1
                candidate.last_selected_at = now
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
                    },
                    quota_pool_ids=quota_ids,
                    fallback_count=max(0, int(fallback_count)),
                    expires_at=now
                    + timedelta(seconds=max(30, int(candidate.timeout_seconds or 120) + 30)),
                )
                session.add(reservation)
                await session.flush()
                credential = candidate.credential_profile
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
                    max_output_tokens=int(candidate.max_output_tokens or 1),
                    timeout_seconds=int(candidate.timeout_seconds or 120),
                    base_url=str(credential.base_url or ""),
                    api_key=_credential_api_key(credential),
                    cli_auth_reference=str(credential.cli_auth_reference or ""),
                    provider_options=options,
                    fallback_count=reservation.fallback_count,
                    team_id=team_id or intent.team_id,
                    subagent_id=subagent_id or intent.subagent_id,
                    llm_profile_id=intent.llm_profile_id,
                    billing_mode=str(credential.billing_mode or "paid"),
                )
            if lease is not None:
                leased = True
                for receipt in decision_receipts:
                    lease.decision_receipt = finalize_decision_receipt(receipt, host_action="lease", host_candidate=lease.candidate_id,
                                                                      outcome="leased", validation_result="host_locked_revalidated")
                return lease
        raise FreeTeamUnavailableError(
            f"無料Teamのプール「{intent.pool_id}」で安全に利用できる候補がありません。"
        )
    finally:
        if not leased:
            for receipt in decision_receipts:
                finalize_decision_receipt(receipt, host_action="no_lease", outcome="not_leased", validation_result="host_lease_unavailable")
        if own_session:
            await session.close()


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
) -> None:
    """実績で確定する。不明な使用量のholdは日界・再起動でも解放しない。"""

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
            uncertain = False
            for quota_id in quota_ids:
                quota = quotas.get(quota_id)
                if quota is None:
                    continue
                if quota_id not in reserved:
                    continue
                _reset_quota_if_needed(quota, now)
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
                    actual_metric not in normalized_actual or not actual_amount.is_finite() or actual_amount < 0
                ):
                    # Neither a timeout nor a crash proves that the provider
                    # stopped. Retain the full reservation even after midnight.
                    uncertain = True
                    continue
                quota.reserved = max(
                    Decimal("0"), _decimal(quota.reserved) - reserved_amount
                )
                reserved.pop(quota_id, None)
                if success or consume_reserved_on_failure:
                    actual_amount = _amount_for_metric(quota.metric_type, normalized_actual)
                    if actual_metric not in normalized_actual:
                        actual_amount = _amount_for_metric(quota.metric_type, estimated)
                    if str(quota.metric_type).lower() != "concurrency":
                        # SDK/providerの報告が事前見積りを超えた場合も実値を台帳へ
                        # 記録する。clampするとpromo上限超過を隠してしまう。
                        quota.consumed = _decimal(quota.consumed) + actual_amount
                        if quota.available <= 0 and quota.status == "active":
                            quota.status = "exhausted"

            accounting["remaining_quota_reservations"] = reserved
            reservation.estimated_usage = accounting
            reservation.status = (
                "uncertain" if uncertain else "committed" if success or consume_reserved_on_failure else "released"
            )
            reservation.actual_usage = {**dict(reservation.actual_usage or {}), **{
                key: float(value) for key, value in normalized_actual.items()
            }}
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
                    if credential is not None:
                        credential.status = "needs_attention"
        lease.decision_receipt = finalize_decision_receipt(
            lease.decision_receipt, outcome="execution_succeeded" if success else "execution_failed",
        )
    finally:
        if own_session:
            await session.close()


async def release_expired_reservations(
    *, session: AsyncSession | None = None
) -> int:
    """クラッシュ等で残った予約は使用量不明のholdとして保持する。"""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    now = _utcnow()
    released = 0
    try:
        async with session.begin():
            result = await session.execute(
                select(FreeTeamReservation).where(
                    FreeTeamReservation.status == "reserved",
                    FreeTeamReservation.expires_at <= now,
                )
            )
            reservation_ids = [item.id for item in result.scalars().all()]
        for reservation_id in reservation_ids:
            lease = RouteLease(
                reservation_id=reservation_id,
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
                session=session if not own_session else None,
            )
            released += 1
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
        async with session.begin():
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
            data = item.to_safe_dict()
            data["configured"] = _credential_configured(item)
            safe_credentials.append(data)
        from . import openai_confirmation_service as confirmation
        verification_reason = complimentary_stop_reason(config)
        _, _, current_config = await confirmation.config_row(session)
        if confirmation.managed(current_config):
            config = current_config
            verification_reason = await confirmation.binding_stop_reason(session, config) or complimentary_stop_reason(config)
        state = {
            "credentials": safe_credentials,
            "candidates": [item.to_dict() for item in candidates],
            "verification": {"status": "unverified" if verification_reason else "verified",
                "stop_reason": verification_reason,
                "external_usage_visibility": "local_and_manual_observation"},
        }
        if include_quotas:
            state["quota_pools"] = [item.to_dict() for item in quotas]
            scope = str(config_get(config, "openai.billing_scope_id", "") or "")
            for model, quota in zip(quotas, state["quota_pools"]):
                if model.id not in OPENAI_DAILY_QUOTA_GROUPS:
                    continue
                central = await _central_unmetered_usage(session, scope, OPENAI_DAILY_QUOTA_GROUPS[model.id], _utcnow()) if scope else Decimal(0)
                used = _decimal(model.consumed) + central
                cap = _openai_effective_cap(model, config)
                # Keep the persisted setting separate from the live Tier guard.
                # daily_cap remains the effective value for existing consumers.
                quota.update(daily_cap=float(cap), effective_daily_cap=float(cap),
                    cap_restriction_reason="usage_tier" if cap < model.daily_cap else "",
                    usage_tier=str(config_get(config, "openai.usage_tier", "") or ""),
                    local_consumed=float(_decimal(model.consumed)),
                    consumed=float(used + model.provider_sync_delta), central_unmetered_usage=float(central),
                    provider_external_delta=float(model.provider_sync_delta),
                    available=float(max(Decimal(0), cap - used - model.provider_sync_delta - _decimal(model.reserved))))
                credential = next((item for item in credentials if item.id == model.credential_profile_id), None)
                quota["stop_reason"] = verification_reason or (
                    "provider_disabled" if not credential or not credential.enabled else
                    "billing_scope_unmapped_usage" if await _unmapped_central_usage(session, OPENAI_DAILY_QUOTA_GROUPS[model.id], _utcnow()) else
                    "billing_scope_mismatch" if scope and model.reset_policy.get("billing_scope_id") not in {None, scope} else
                    "billing_scope_unbound_usage" if not model.reset_policy.get("billing_scope_id") and (model.consumed or model.reserved or model.provider_observed_usage) else
                    model.status if model.status != "active" else "budget_exhausted" if quota["available"] <= 0 else "")
        if include_reservations:
            state["reservations"] = [item.to_dict() for item in reservations]
        return state
    finally:
        if own_session:
            await session.close()
