"""無料Teamの設定、候補選択、原子的クォータ予約を管理する。"""

from __future__ import annotations

import os
import uuid
from fnmatch import fnmatchcase
from dataclasses import dataclass, field
from calendar import monthrange
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_db_session
from ..memory.models.free_team import (
    FreeTeamCandidateModel,
    FreeTeamCredentialProfile,
    FreeTeamQuotaPool,
    FreeTeamReservation,
)
from .agent_team_service import config_get
from .free_team_defaults import free_team_profile_template


ROUTING_PROFILE_PROVIDER = "routing-profile"
FREE_TEAM_PROFILE_ID = "free-team"
FREE_TEAM_MODEL_ID = "free-team"
ALLOWED_BILLING_MODES = {
    "complimentary",
    "free_tier",
    "promo_credit",
    "subscription_cli",
}
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
    member_key: str = ""
    group_id: str = ""
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

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "routing_profile": self.routing_profile_id,
            "pool": self.pool_id,
            "credential_profile": self.credential_profile_id,
            "candidate": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
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
        elif key == "agent_team" and isinstance(value, dict):
            merged_team = dict(profile["agent_team"])
            for section in ("model_groups", "members"):
                stored_section = value.get(section)
                if isinstance(stored_section, dict):
                    merged_team[section] = {
                        **dict(merged_team.get(section) or {}),
                        **stored_section,
                    }
            merged_team.update(
                {
                    section: section_value
                    for section, section_value in value.items()
                    if section not in {"model_groups", "members"}
                }
            )
            profile["agent_team"] = merged_team
        else:
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
            member_key="main",
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
    member_key: str,
    group_id: str,
    group: Mapping[str, Any],
) -> RouteIntent | None:
    if str(group.get("target_type") or "").strip().lower() != "pool":
        return None
    pool_id = str(group.get("pool_id") or "").strip()
    if not pool_id:
        return None
    profile = free_team_profile(config)
    pool = (profile.get("pools") or {}).get(pool_id, {}) or {}
    return RouteIntent(
        kind="pool",
        routing_profile_id=str(group.get("routing_profile_id") or FREE_TEAM_PROFILE_ID),
        pool_id=pool_id,
        member_key=member_key,
        group_id=group_id,
        effort_policy=str(group.get("effort_policy") or ""),
        effort=str(group.get("effort") or ""),
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
    if not candidate.enabled or not credential.enabled:
        return False
    if credential.status not in {"ready", "active"}:
        return False
    if credential.billing_mode not in ALLOWED_BILLING_MODES:
        return False
    if credential.allow_paid_overage:
        # 無料Teamでは有料超過を許可した認証も自動選択しない。
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
    if not quota.window_end or quota.window_end > now:
        return
    policy = dict(quota.reset_policy or {})
    quota.consumed = Decimal("0")
    quota.reserved = Decimal("0")
    quota.provider_observed_usage = Decimal("0")
    quota.window_start = now
    quota.window_end = _next_window_end(policy, now)
    if quota.status == "exhausted":
        quota.status = "active"


def _next_window_end(
    reset_policy: Mapping[str, Any], now: datetime
) -> datetime | None:
    kind = str(reset_policy.get("kind") or "none")
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


async def acquire_route_lease(
    intent: RouteIntent,
    *,
    prompt: str,
    required_capabilities: Iterable[str] = ("text",),
    member_key: str = "",
    excluded_candidate_ids: Iterable[str] = (),
    fallback_count: int = 0,
    session: AsyncSession | None = None,
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
                    member_key=member_key or intent.member_key or None,
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
                )
            if lease is not None:
                return lease
        raise FreeTeamUnavailableError(
            f"無料Teamのプール「{intent.pool_id}」で安全に利用できる候補がありません。"
        )
    finally:
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
) -> None:
    """予約を実使用量で確定する。使用量不明の成功は予約最大量を消費扱いにする。"""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    now = _utcnow()
    try:
        async with session.begin():
            reservation_result = await session.execute(
                select(FreeTeamReservation)
                .where(FreeTeamReservation.id == lease.reservation_id)
                .with_for_update()
            )
            reservation = reservation_result.scalar_one_or_none()
            if reservation is None or reservation.status != "reserved":
                return
            candidate_result = await session.execute(
                select(FreeTeamCandidateModel)
                .where(FreeTeamCandidateModel.id == reservation.candidate_id)
                .with_for_update()
            )
            candidate = candidate_result.scalar_one()
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
            reserved = dict(
                (reservation.estimated_usage or {}).get("quota_reservations") or {}
            )
            normalized_actual = {
                key: _decimal(value) for key, value in dict(actual_usage or {}).items()
            }
            for quota_id in quota_ids:
                quota = quotas.get(quota_id)
                if quota is None:
                    continue
                reserved_amount = _decimal(reserved.get(quota_id))
                quota.reserved = max(
                    Decimal("0"), _decimal(quota.reserved) - reserved_amount
                )
                if success or consume_reserved_on_failure:
                    actual_amount = _amount_for_metric(quota.metric_type, normalized_actual)
                    if not actual_usage or actual_amount <= 0:
                        actual_amount = _amount_for_metric(quota.metric_type, estimated)
                    if str(quota.metric_type).lower() != "concurrency":
                        # SDK/providerの報告が事前見積りを超えた場合も実値を台帳へ
                        # 記録する。clampするとpromo上限超過を隠してしまう。
                        quota.consumed = _decimal(quota.consumed) + actual_amount
                        if quota.available <= 0 and quota.status == "active":
                            quota.status = "exhausted"

            reservation.status = (
                "committed" if success or consume_reserved_on_failure else "released"
            )
            reservation.actual_usage = {
                key: float(value) for key, value in normalized_actual.items()
            }
            reservation.error_class = error_class or None
            reservation.finalized_at = now
            if success:
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
    finally:
        if own_session:
            await session.close()


async def release_expired_reservations(
    *, session: AsyncSession | None = None
) -> int:
    """クラッシュ等で残った期限切れ予約を解放する。"""

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
) -> dict[str, Any]:
    """秘密を除いた設定・候補・クォータ・台帳状態を返す。"""

    own_session = session is None
    if session is None:
        session = await get_db_session()
    try:
        async with session.begin():
            await seed_free_team_defaults(session)
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
        state = {
            "credentials": safe_credentials,
            "candidates": [item.to_dict() for item in candidates],
        }
        if include_quotas:
            state["quota_pools"] = [item.to_dict() for item in quotas]
        if include_reservations:
            state["reservations"] = [item.to_dict() for item in reservations]
        return state
    finally:
        if own_session:
            await session.close()
