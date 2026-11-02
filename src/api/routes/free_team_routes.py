"""無料Teamの設定・状態・同期API。秘密情報は返さない。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from ...memory.database import get_db_session
from ...utils.logging_config import FILE_ONLY_LOG_EXTRA
from ...memory.models.free_team import (
    FreeTeamCandidateModel,
    FreeTeamCredentialProfile,
    FreeTeamQuotaPool,
    OPENAI_DAILY_QUOTA_GROUPS,
    OPENAI_SAFETY_TOKENS,
)
from ...services.free_team_service import (
    FREE_TEAM_PROFILE_ID,
    free_team_profile,
    get_free_team_state,
    refresh_openai_usage_actual_if_due,
    recover_credential_after_auth_update,
    FreeTeamReservationReconciliationError,
    reconcile_uncertain_openai_reservation,
    release_expired_reservations,
    seed_free_team_defaults,
    complimentary_model_group,
    _reset_quota_if_needed,
    _credential_api_key as credential_api_key,
    _openrouter_quota_observation_is_current,
    openrouter_credential_source,
    sync_provider_observation,
    openrouter_free_catalog_evidence,
    FreeTeamUnavailableError,
)
from ...services.runtime_policy_utils import config_get
from ...services.free_team_defaults import (
    OPENROUTER_FREE_DAILY_TIERS,
    OPENROUTER_SAFETY_MARGIN_REQUESTS,
)
from ...services.openai_provider_circuit import (
    clear_openai_provider_circuit,
    provider_circuit_status,
)
from ...services import free_team_provider_observer as provider_observer
from ...services.llm_model_catalog import default_fetch_json
from ...llm.deployment_resolver import is_retired_model
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer


logger = logging.getLogger(__name__)


class OpenRouterLocalQuota(BaseModel):
    configured_limit: float
    safety_margin: float
    effective_cap: float
    used: float
    reserved: float


class OpenRouterProviderObservation(BaseModel):
    source: str
    status: Literal["unverified", "observed", "stale", "failed"]
    used: int | None
    limit: int | None
    remaining: int | None
    observed_at: str | None
    failure: str


class OpenRouterQuotaResponse(BaseModel):
    local: OpenRouterLocalQuota | None
    provider_observed: OpenRouterProviderObservation


class FreeTeamUsageResponse(BaseModel):
    quota_pools: list[dict[str, Any]]
    verification: dict[str, Any]
    provider_blocked: bool
    provider_block_reason_code: str | None
    provider_blocked_until: str | None
    provider_half_open: bool
    provider_block_persistence: str | None
    next_probe_at: str | None = None
    probe_interval_minutes: int = 15
    openai_usage_actual: dict[str, Any] = Field(default_factory=dict)


class OpenAIProviderCircuitStatus(BaseModel):
    provider_blocked: bool
    provider_block_reason_code: str | None
    provider_blocked_until: str | None
    provider_half_open: bool
    provider_block_persistence: str | None
    next_probe_at: str | None = None
    probe_interval_minutes: int = 15


_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class FreeTeamActualUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: StrictInt = Field(ge=0)
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    reasoning_tokens: StrictInt | None = Field(default=None, ge=0)


class FreeTeamReservationReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str = Field(pattern=_UUID_PATTERN)
    actual_usage: FreeTeamActualUsageInput
    evidence_ref: str = Field(pattern=_UUID_PATTERN)


class FreeTeamReconciliationUsageResult(BaseModel):
    input_tokens: StrictInt
    output_tokens: StrictInt
    reasoning_tokens: StrictInt
    total_tokens: StrictInt


class FreeTeamReconciliationQuotaBalance(BaseModel):
    consumed_before: str
    reserved_before: str
    hold_released: str
    actual_consumed: str
    consumed_after: str
    reserved_after: str


class FreeTeamReservationReconciliationResponse(BaseModel):
    reservation_id: str
    status: Literal["committed"]
    evidence_ref: str
    idempotency_key: str
    actual_usage: FreeTeamReconciliationUsageResult
    quota_pools: dict[str, FreeTeamReconciliationQuotaBalance]
    idempotent_replay: bool


def _save_config(server: "WebChatServer", key: str, value: Any) -> None:
    if hasattr(server.config, "save_to_file"):
        if not server.config.save_to_file(key, value):
            raise HTTPException(status_code=500, detail="設定を保存できませんでした")
    else:
        server.config.set(key, value)


def _non_negative(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{field} は数値で指定してください") from exc
    if not parsed.is_finite() or parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field} は0以上で指定してください")
    return parsed


def _free_openrouter_model(item: dict[str, Any]) -> bool:
    return openrouter_free_catalog_evidence(item) is not None


def _dynamic_candidate_id(model_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")[:90]
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
    return f"openrouter-dynamic-{slug}-{digest}"[:140]


_PROVIDER_OPTION_KEYS = {
    "allow_fallbacks",
    "capabilities",
    "concurrency_limit",
    "enable_tools",
    "free_catalog_evidence",
    "input_price_per_million",
    "input_usd_per_million",
    "max_price",
    "max_requests_per_turn",
    "max_reasoning_tokens",
    "output_price_per_million",
    "output_usd_per_million",
    "paid_fallback_disabled",
    "require_free",
}
_TOOL_POLICY_KEYS = {
    "allowed",
    "complimentary_tool_calls_allowed",
}


def _secret_like_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return normalized in {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "cookie",
        "access_token",
        "refresh_token",
        "bearer_token",
    } or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_like_key(key):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _safe_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_config_value(item)
            for key, item in value.items()
            if not _secret_like_key(key)
        }
    if isinstance(value, list):
        return [_safe_config_value(item) for item in value]
    return value


def _public_free_team_profile(config: Any) -> dict[str, Any]:
    """Hide the legacy OpenAI key-profile reference from the settings API."""

    profile = copy.deepcopy(free_team_profile(config))
    credential_profiles = dict(profile.get("credential_profiles") or {})
    credential_profiles.pop("openai-complimentary", None)
    profile["credential_profiles"] = credential_profiles
    for collection_name in ("candidates", "quota_pools"):
        collection = profile.get(collection_name)
        if not isinstance(collection, dict):
            continue
        for item in collection.values():
            if (
                isinstance(item, dict)
                and item.get("credential_profile_id") == "openai-complimentary"
            ):
                item["credential_profile_id"] = "openai_api_key"
    return _safe_config_value(profile)


def _validate_option_mapping(
    value: Any, *, allowed: set[str], field: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{field} はobjectです")
    if _contains_secret_key(value):
        raise HTTPException(
            status_code=422, detail=f"{field} に秘密情報を含めることはできません"
        )
    unsupported = set(value) - allowed
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=f"{field} に未対応の項目があります: {sorted(unsupported)[0]}",
        )
    return dict(value)


def _validate_base_url(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="base_url が不正です")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(
            status_code=422,
            detail="base_url に認証情報を含めることはできません",
        )
    return normalized


async def _invalidate_openrouter_quota_observation(
    observation_store: provider_observer.ObservationStore,
    expected_binding: str | None,
    *,
    config: Any,
    force: bool = False,
) -> None:
    """Make DB evidence stale without releasing conservative usage holds."""

    session = await get_db_session()
    try:
        async with session.begin():
            await seed_free_team_defaults(session)
            profile = (
                await session.execute(
                    select(FreeTeamCredentialProfile)
                    .where(FreeTeamCredentialProfile.id == "openrouter-free")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            api_key = credential_api_key(profile, config) if profile else ""
            enabled = bool(profile and profile.enabled)
            base_url = profile.base_url if profile else ""
            revision = str(getattr(profile, "updated_at", None) or "") if profile else ""
            source = openrouter_credential_source(config)
            current_binding = observation_store.binding(
                "openrouter-free", enabled, api_key, base_url, revision, source
            )
            if not force and current_binding != expected_binding:
                return
            quota = (
                await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id == "openrouter-free-rpd")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if quota is None:
                return
            # Reject the snapshot for routing while retaining conservative
            # provider and in-flight holds until a verified refresh or reset.
            quota.last_provider_sync_at = None
            quota.provider_sync_version = int(quota.provider_sync_version or 0) + 1
    finally:
        await session.close()


async def _sync_openrouter_quota_observation(
    observation_store: provider_observer.ObservationStore,
    expected_binding: str | None,
    *,
    config: Any,
) -> bool:
    """Apply only the latest fresh observation for the locked key revision."""

    if expected_binding is None:
        await _invalidate_openrouter_quota_observation(
            observation_store, expected_binding, config=config
        )
        return False
    session = await get_db_session()
    try:
        async with session.begin():
            await seed_free_team_defaults(session)
            profile = (
                await session.execute(
                    select(FreeTeamCredentialProfile)
                    .where(FreeTeamCredentialProfile.id == "openrouter-free")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            api_key = credential_api_key(profile, config) if profile else ""
            enabled = bool(profile and profile.enabled)
            base_url = profile.base_url if profile else ""
            revision = str(getattr(profile, "updated_at", None) or "") if profile else ""
            source = openrouter_credential_source(config)
            current_binding = observation_store.binding(
                "openrouter-free", enabled, api_key, base_url, revision, source
            )
            if current_binding != expected_binding or profile is None:
                return False
            observation = observation_store.status(current_binding)
            if observation.get("status") != "observed":
                quota = (
                    await session.execute(
                        select(FreeTeamQuotaPool)
                        .where(FreeTeamQuotaPool.id == "openrouter-free-rpd")
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if quota is not None:
                    quota.last_provider_sync_at = None
                    quota.provider_sync_version = int(quota.provider_sync_version or 0) + 1
                return False
            quota = (
                await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id == "openrouter-free-rpd")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if quota is None:
                return False
            fingerprint = provider_observer.quota_binding_fingerprint(
                "openrouter-free",
                enabled,
                api_key,
                base_url,
                revision,
                source,
            )
            await sync_provider_observation(
                session,
                quota,
                {
                    "source": "openrouter_key_api",
                    "usage": observation.get("used"),
                    "limit": observation.get("limit"),
                    "credential_binding": fingerprint,
                },
                config=config,
                now=datetime.now(timezone.utc),
            )
            return True
    except FreeTeamUnavailableError:
        await _invalidate_openrouter_quota_observation(
            observation_store, expected_binding, config=config
        )
        return False
    except Exception:
        # Do not leave a previous DB snapshot usable if applying the latest
        # provider observation failed at its persistence boundary.
        await _invalidate_openrouter_quota_observation(
            observation_store, expected_binding, config=config
        )
        return False
    finally:
        await session.close()


def register_free_team_routes(app: FastAPI, server: "WebChatServer") -> None:
    from .openai_confirmation_routes import register_openai_confirmation_routes
    register_openai_confirmation_routes(app, server)
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def require_quota_admin(request: Request, _: None = Depends(require_auth)) -> None:
        if getattr(server, "auth_enabled", True) is False:
            return
        admin_check = getattr(server, "_is_admin_user", None)
        if not callable(admin_check) or not await admin_check(request):
            raise HTTPException(status_code=403, detail="Administrator privileges required")

    @app.post(
        "/api/free-team/reservations/reconcile",
        response_model=FreeTeamReservationReconciliationResponse,
    )
    async def reconcile_reservation(
        body: FreeTeamReservationReconciliationRequest,
        request: Request,
        _: None = Depends(require_quota_admin),
    ):
        """Settle one uncertain OpenAI reservation from its linked TokenUsage row."""
        user_info = await server._get_user_info_from_request(request)
        if not user_info and getattr(server, "auth_enabled", True) is False:
            actor_user_id = "default_user"
        elif not isinstance(user_info, dict) or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        else:
            actor_user_id = str(user_info["id"])
        try:
            return await reconcile_uncertain_openai_reservation(
                body.reservation_id,
                actual_usage=body.actual_usage.model_dump(exclude_none=True),
                evidence_ref=body.evidence_ref,
                actor_user_id=actor_user_id,
            )
        except FreeTeamReservationReconciliationError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.code) from exc

    async def free_team_reaper_loop() -> None:
        while True:
            try:
                processed = await release_expired_reservations(config=server.config)
                usage_refresh = await refresh_openai_usage_actual_if_due(
                    config=server.config,
                    trigger="periodic",
                )
                if usage_refresh.get("status") == "failed":
                    logger.debug(
                        "OpenAI usage observation unavailable: code=%s",
                        usage_refresh.get("failure") or "unknown",
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
                logger.debug(
                    "Free Team reservation reaper pass completed "
                    "(expired_reserved_rows_processed=%s)",
                    processed,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            except Exception as exc:
                # DB一時障害でも次回周期で回復させる。
                logger.warning(
                    "Free Team reservation reaper pass failed (error_class=%s)",
                    type(exc).__name__,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            await asyncio.sleep(60)

    async def start_free_team_reaper() -> None:
        try:
            processed = await release_expired_reservations(config=server.config)
            usage_refresh = await refresh_openai_usage_actual_if_due(
                config=server.config,
                trigger="periodic",
            )
            if usage_refresh.get("status") == "failed":
                logger.debug(
                    "OpenAI startup usage observation unavailable: code=%s",
                    usage_refresh.get("failure") or "unknown",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            logger.debug(
                "Free Team startup reservation recovery completed "
                "(expired_reserved_rows_processed=%s)",
                processed,
                extra=FILE_ONLY_LOG_EXTRA,
            )
        except Exception as exc:
            logger.warning(
                "Free Team startup reservation recovery failed "
                "(error_class=%s)",
                type(exc).__name__,
                extra=FILE_ONLY_LOG_EXTRA,
            )
        task = getattr(server, "_free_team_reaper_task", None)
        if task is None or task.done():
            server._free_team_reaper_task = asyncio.create_task(
                free_team_reaper_loop()
            )

    async def stop_free_team_reaper() -> None:
        task = getattr(server, "_free_team_reaper_task", None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        server._free_team_reaper_task = None

    startup_hooks = getattr(server, "_startup_background_tasks", None)
    shutdown_hooks = getattr(server, "_shutdown_background_tasks", None)
    if isinstance(startup_hooks, list):
        startup_hooks.append(start_free_team_reaper)
    if isinstance(shutdown_hooks, list):
        shutdown_hooks.append(stop_free_team_reaper)

    @app.get("/api/free-team/settings")
    async def get_settings(_: None = Depends(require_auth)):
        state = await get_free_team_state(
            include_quotas=False, include_reservations=False, config=server.config
        )
        return {
            "routing_profile_id": FREE_TEAM_PROFILE_ID,
            "profile": _public_free_team_profile(server.config),
            **state,
        }

    @app.get("/api/free-team/usage", response_model=FreeTeamUsageResponse)
    async def get_usage(_: None = Depends(require_auth)):
        state = await get_free_team_state(include_reservations=False, config=server.config)
        return {
            "quota_pools": state["quota_pools"],
            "verification": state["verification"],
            "provider_blocked": state["provider_blocked"],
            "provider_block_reason_code": state["provider_block_reason_code"],
            "provider_blocked_until": state["provider_blocked_until"],
            "provider_half_open": state["provider_half_open"],
            "provider_block_persistence": state["provider_block_persistence"],
            "next_probe_at": state["next_probe_at"],
            "probe_interval_minutes": state["probe_interval_minutes"],
            "openai_usage_actual": state.get("openai_usage_actual", {}),
        }

    @app.post(
        "/api/free-team/openai-provider-circuit/reset",
        response_model=OpenAIProviderCircuitStatus,
    )
    async def reset_openai_provider_circuit_route(
        request: Request,
        _: None = Depends(require_quota_admin),
    ):
        if not clear_openai_provider_circuit(server.config):
            raise HTTPException(
                status_code=503,
                detail="openai_provider_circuit_reset_unavailable",
            )
        user_info = await server._get_user_info_from_request(request)
        actor = str(user_info.get("id") or "admin") if isinstance(user_info, dict) else "admin"
        logger.info("OpenAI provider circuit cleared by admin (actor_id=%s)", actor)
        return provider_circuit_status(server.config)

    @app.put("/api/free-team/settings")
    async def update_settings(request: Request, _: None = Depends(require_auth)):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="設定はobjectで指定してください")
        if _contains_secret_key(body):
            raise HTTPException(
                status_code=422, detail="無料Team設定に秘密情報を含めることはできません"
            )
        current = free_team_profile(server.config)
        allowed = {
            "display_name",
            "enabled",
            "main_pool_id",
            "max_fallbacks",
            "pools",
            "openrouter_credential_source",
            "openrouter_free_tier",
        }
        for key in body:
            if key not in allowed:
                raise HTTPException(status_code=422, detail=f"更新できない設定です: {key}")
        next_profile = dict(current)
        next_profile.update(body)
        if "openrouter_free_tier" in body:
            requested_tier = body["openrouter_free_tier"]
            if (
                isinstance(requested_tier, bool)
                or not isinstance(requested_tier, int)
                or requested_tier not in OPENROUTER_FREE_DAILY_TIERS
            ):
                raise HTTPException(
                    status_code=422,
                    detail="openrouter_free_tier は50または1000です",
                )
        previous_source = current.get(
            "openrouter_credential_source", "dedicated"
        )
        source = previous_source
        if "openrouter_credential_source" in body:
            requested_source = body["openrouter_credential_source"]
            if (
                not isinstance(requested_source, str)
                or requested_source
                not in {"dedicated", "existing_openrouter"}
            ):
                raise HTTPException(
                    status_code=422,
                    detail="openrouter_credential_source が不正です",
                )
            source = requested_source
        next_profile["openrouter_credential_source"] = source
        if (
            next_profile.get("enabled") is False
            and str(server.config.get("llm_provider", "") or "")
            == "routing-profile"
            and str(server.config.get("llm_model", "") or "") == "free-team"
        ):
            raise HTTPException(
                status_code=409,
                detail="通常モデルへ切り替えてから無料Teamを無効にしてください",
            )
        if not isinstance(next_profile.get("pools"), dict):
            raise HTTPException(status_code=422, detail="pools はobjectで指定してください")
        for pool_id, pool in next_profile["pools"].items():
            if not isinstance(pool, dict):
                raise HTTPException(
                    status_code=422, detail=f"pool {pool_id} はobjectで指定してください"
                )
            if str(pool.get("tool_mode") or "auto") not in {
                "auto",
                "disabled",
                "required",
            }:
                raise HTTPException(
                    status_code=422,
                    detail=f"pool {pool_id} のtool_modeが不正です",
                )
        if str(next_profile.get("main_pool_id") or "") not in next_profile["pools"]:
            raise HTTPException(status_code=422, detail="main_pool_id がpoolsに存在しません")
        next_profile["max_fallbacks"] = max(
            0, min(10, int(next_profile.get("max_fallbacks") or 0))
        )
        _save_config(
            server,
            f"routing_profiles.{FREE_TEAM_PROFILE_ID}",
            next_profile,
        )
        if source != previous_source:
            observation_store.invalidate()
            await _invalidate_openrouter_quota_observation(
                observation_store,
                None,
                config=server.config,
                force=True,
            )
        return {"success": True, "profile": _public_free_team_profile(server.config)}

    @app.put("/api/free-team/credentials/{profile_id}")
    async def update_credential(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
        if profile_id == "openai-complimentary":
            raise HTTPException(
                status_code=410,
                detail="OpenAIは共有clientの通常キーを使用します",
            )
        body = await request.json()
        allowed = {
            "display_name",
            "api_key",
            "clear_api_key",
            "cli_auth_reference",
            "base_url",
            "enabled",
            "privacy_class",
            "allow_paid_overage",
        }
        if not isinstance(body, dict) or any(key not in allowed for key in body):
            raise HTTPException(status_code=422, detail="認証設定に未対応の項目があります")
        session = await get_db_session()
        openrouter_binding_changed = False
        try:
            async with session.begin():
                await seed_free_team_defaults(session)
                result = await session.execute(
                    select(FreeTeamCredentialProfile)
                    .where(FreeTeamCredentialProfile.id == profile_id)
                    .with_for_update()
                )
                profile = result.scalar_one_or_none()
                if profile is None:
                    raise HTTPException(status_code=404, detail="認証プロファイルがありません")
                before_openrouter_identity = (
                    profile.api_key,
                    profile.base_url,
                    bool(profile.enabled),
                )
                for field in (
                    "display_name",
                    "cli_auth_reference",
                    "enabled",
                    "privacy_class",
                ):
                    if field in body:
                        setattr(profile, field, body[field])
                if "base_url" in body:
                    profile.base_url = _validate_base_url(body["base_url"])
                if body.get("clear_api_key") is True:
                    profile.api_key = None
                elif isinstance(body.get("api_key"), str) and body["api_key"].strip():
                    profile.api_key = body["api_key"].strip()
                auth_material_updated = (
                    isinstance(body.get("api_key"), str)
                    and bool(body["api_key"].strip())
                ) or (
                    "cli_auth_reference" in body
                    and bool(str(body.get("cli_auth_reference") or "").strip())
                )
                explicitly_reenabled = body.get("enabled") is True
                if auth_material_updated or explicitly_reenabled:
                    await recover_credential_after_auth_update(session, profile)
                # 有料超過は明示入力でも無料Teamの自動選択対象外になる。
                if "allow_paid_overage" in body:
                    profile.allow_paid_overage = bool(body["allow_paid_overage"])
                safe = profile.to_safe_dict()
                openrouter_binding_changed = bool(
                    profile.id == "openrouter-free"
                    and before_openrouter_identity
                    != (profile.api_key, profile.base_url, bool(profile.enabled))
                )
            if openrouter_binding_changed:
                observation_store.invalidate()
                await _invalidate_openrouter_quota_observation(
                    observation_store,
                    None,
                    config=server.config,
                    force=True,
                )
            return {"success": True, "credential": safe}
        finally:
            await session.close()

    @app.patch("/api/free-team/candidates/{candidate_id}")
    async def update_candidate(
        candidate_id: str, request: Request, _: None = Depends(require_auth)
    ):
        body = await request.json()
        allowed = {
            "enabled",
            "model",
            "priority",
            "weight",
            "effort",
            "max_input_tokens",
            "max_output_tokens",
            "timeout",
            "capabilities",
            "tool_call_policy",
            "provider_options",
        }
        if not isinstance(body, dict) or any(key not in allowed for key in body):
            raise HTTPException(status_code=422, detail="候補設定に未対応の項目があります")
        session = await get_db_session()
        try:
            async with session.begin():
                await seed_free_team_defaults(session)
                result = await session.execute(
                    select(FreeTeamCandidateModel)
                    .where(FreeTeamCandidateModel.id == candidate_id)
                    .with_for_update()
                )
                candidate = result.scalar_one_or_none()
                if candidate is None:
                    raise HTTPException(status_code=404, detail="候補モデルがありません")
                if "model" in body:
                    model = body["model"]
                    if not isinstance(model, str) or not model.strip() or len(model) > 240 or is_retired_model(model):
                        raise HTTPException(status_code=422, detail="model が不正です")
                    model = model.strip()
                    if candidate.provider == "openai":
                        group = complimentary_model_group(model)
                        if group is None or not candidate.quota_pool_ids or any(
                            OPENAI_DAILY_QUOTA_GROUPS.get(pool) != group for pool in candidate.quota_pool_ids
                        ):
                            raise HTTPException(status_code=422, detail="model は既存共有quotaの対象model groupに限定されます")
                    candidate.model = model
                if "enabled" in body:
                    candidate.enabled = bool(body["enabled"])
                    if candidate.enabled and candidate.status in {
                        "needs_attention",
                        "unavailable",
                    }:
                        candidate.status = "ready"
                        candidate.cooldown_until = None
                        candidate.consecutive_failures = 0
                if "priority" in body:
                    candidate.priority = max(0, int(body["priority"]))
                if "weight" in body:
                    candidate.weight = max(1, int(body["weight"]))
                if "effort" in body:
                    candidate.effort = str(body["effort"] or "") or None
                for field in ("max_input_tokens", "max_output_tokens"):
                    if field in body:
                        setattr(candidate, field, max(1, int(body[field])))
                if "timeout" in body:
                    candidate.timeout_seconds = max(1, int(body["timeout"]))
                if "capabilities" in body:
                    if not isinstance(body["capabilities"], list) or not all(
                        isinstance(value, str) for value in body["capabilities"]
                    ):
                        raise HTTPException(
                            status_code=422, detail="capabilities は文字列配列です"
                        )
                    candidate.capabilities = body["capabilities"]
                if "tool_call_policy" in body:
                    candidate.tool_call_policy = _validate_option_mapping(
                        body["tool_call_policy"],
                        allowed=_TOOL_POLICY_KEYS,
                        field="tool_call_policy",
                    )
                if "provider_options" in body:
                    provider_options = _validate_option_mapping(
                        body["provider_options"],
                        allowed=_PROVIDER_OPTION_KEYS,
                        field="provider_options",
                    )
                    # Catalog proof is written only by the official Models
                    # refresh. An admin patch may preserve existing evidence,
                    # but cannot create or replace it by editing JSON.
                    persisted_proof = dict(candidate.provider_options or {}).get(
                        "free_catalog_evidence"
                    )
                    provider_options.pop("free_catalog_evidence", None)
                    if isinstance(persisted_proof, dict):
                        provider_options["free_catalog_evidence"] = persisted_proof
                    candidate.provider_options = provider_options
                safe = candidate.to_dict()
            return {"success": True, "candidate": safe}
        finally:
            await session.close()

    @app.patch("/api/free-team/quota-pools/{quota_id}")
    async def update_quota(
        quota_id: str, request: Request, _: None = Depends(require_quota_admin)
    ):
        body = await request.json()
        allowed = {
            "limit",
            "daily_cap",
            "safety_margin_ratio",
            "safety_margin_units",
            "reset_policy",
            "status",
        }
        if not isinstance(body, dict) or any(key not in allowed for key in body):
            raise HTTPException(status_code=422, detail="クォータ設定に未対応の項目があります")
        session = await get_db_session()
        try:
            async with session.begin():
                await seed_free_team_defaults(session)
                result = await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id == quota_id)
                    .with_for_update()
                )
                quota = result.scalar_one_or_none()
                if quota is None:
                    raise HTTPException(status_code=404, detail="クォータプールがありません")
                if "limit" in body:
                    quota.limit_value = _non_negative(body["limit"], "limit")
                    quota.reset_policy = {k: v for k, v in (quota.reset_policy or {}).items() if k != "local_cap_mode"}
                if "safety_margin_ratio" in body:
                    ratio = _non_negative(
                        body["safety_margin_ratio"], "safety_margin_ratio"
                    )
                    if ratio > 1:
                        raise HTTPException(status_code=422, detail="安全マージン率は1以下です")
                    quota.safety_margin_ratio = ratio
                if "safety_margin_units" in body:
                    quota.safety_margin_units = _non_negative(
                        body["safety_margin_units"], "safety_margin_units"
                    )
                if "daily_cap" in body:
                    cap = _non_negative(body["daily_cap"], "daily_cap")
                    from ...services.pricing.free_tier import free_tier_limit_for_group
                    usage_tier = config_get(server.config, "openai.usage_tier", None)
                    effective_limit = min(
                        Decimal(quota.limit_value),
                        Decimal(
                            free_tier_limit_for_group(
                                OPENAI_DAILY_QUOTA_GROUPS[quota.id], usage_tier
                            )
                        ),
                    ) if quota.id in OPENAI_DAILY_QUOTA_GROUPS else Decimal(quota.limit_value)
                    minimum_safety_margin = (
                        Decimal(OPENROUTER_SAFETY_MARGIN_REQUESTS)
                        if quota.id == "openrouter-free-rpd"
                        else OPENAI_SAFETY_TOKENS
                    )
                    if cap > effective_limit - minimum_safety_margin:
                        detail = (
                            "OpenRouterの日次capはローカル上限から安全余白2件を引いた値以下です"
                            if quota.id == "openrouter-free-rpd"
                            else "daily_cap は有効なtier上限から安全余裕を引いた値以下です"
                        )
                        raise HTTPException(status_code=422, detail=detail)
                    quota.safety_margin_ratio = Decimal("0")
                    quota.safety_margin_units = effective_limit - cap
                if "reset_policy" in body:
                    if not isinstance(body["reset_policy"], dict):
                        raise HTTPException(status_code=422, detail="reset_policy はobjectです")
                    policy = body["reset_policy"]
                    if _contains_secret_key(policy):
                        raise HTTPException(
                            status_code=422,
                            detail="reset_policy に秘密情報を含めることはできません",
                        )
                    allowed_policy_keys = {"kind", "timezone", "hour", "day"}
                    if quota.id == "gemini-promo-monthly-usd":
                        allowed_policy_keys.add("billing_scope_id")
                    if quota.id in {"gemini-free-rpm", "gemini-free-tpm", "gemini-free-rpd"} and quota.credential_profile_id == "gemini-free" and quota.metric_type == quota.id.removeprefix("gemini-free-"):
                        allowed_policy_keys.add("local_cap_mode")
                    if "local_cap_mode" in policy and (policy["local_cap_mode"] != "provider"):
                        raise HTTPException(status_code=422, detail="local_cap_mode が不正です")
                    unsupported = set(policy) - allowed_policy_keys
                    if unsupported:
                        raise HTTPException(
                            status_code=422,
                            detail=f"reset_policy に未対応の項目があります: {sorted(unsupported)[0]}",
                        )
                    if str(policy.get("timezone") or "UTC").upper() != "UTC":
                        raise HTTPException(
                            status_code=422,
                            detail="reset_policy.timezone は現在UTCのみ対応です",
                        )
                    new_scope: str | None = None
                    if "billing_scope_id" in policy:
                        raw_scope = policy["billing_scope_id"]
                        if not isinstance(raw_scope, str):
                            raise HTTPException(
                                status_code=422,
                                detail="reset_policy.billing_scope_id は文字列で指定してください",
                            )
                        new_scope = raw_scope.strip()
                        if not 1 <= len(new_scope) <= 100 or new_scope.lower() in {
                            "default", "unknown", "none",
                        }:
                            raise HTTPException(
                                status_code=422,
                                detail="reset_policy.billing_scope_id が不正です",
                            )
                    existing_raw = (quota.reset_policy or {}).get("billing_scope_id")
                    existing_scope = existing_raw.strip() if isinstance(existing_raw, str) else ""
                    existing_is_valid = (
                        1 <= len(existing_scope) <= 100
                        and existing_scope.lower() not in {"default", "unknown", "none"}
                    )
                    if new_scope is not None and existing_is_valid and new_scope != existing_scope:
                        raise HTTPException(
                            status_code=409,
                            detail="reset_policy.billing_scope_id は登録済みの値から変更できません",
                        )
                    merged_policy = {**policy}
                    if "limit" in body:
                        merged_policy.pop("local_cap_mode", None)
                    if "local_cap_mode" not in policy and "limit" not in body and (quota.reset_policy or {}).get("local_cap_mode") == "provider":
                        merged_policy["local_cap_mode"] = "provider"
                    if new_scope is not None:
                        merged_policy["billing_scope_id"] = new_scope
                    elif "billing_scope_id" in (quota.reset_policy or {}):
                        merged_policy["billing_scope_id"] = existing_raw
                    quota.reset_policy = merged_policy
                if "status" in body:
                    quota.status = str(body["status"] or "active")
                if quota.id in OPENAI_DAILY_QUOTA_GROUPS:
                    from ...services.pricing.free_tier import free_tier_limit_for_group
                    usage_tier = config_get(server.config, "openai.usage_tier", None)
                    official_limit = Decimal(
                        free_tier_limit_for_group(
                            OPENAI_DAILY_QUOTA_GROUPS[quota.id], usage_tier
                        )
                    )
                    if (
                        ("limit" in body and quota.limit_value > official_limit)
                        or quota.safety_margin_units < OPENAI_SAFETY_TOKENS
                    ):
                        raise HTTPException(status_code=422, detail="OpenAI共有quotaは公式上限以下・安全量30000以上です")
                    policy = quota.reset_policy or {}
                    if policy.get("kind") != "daily" or str(policy.get("timezone", "UTC")).upper() != "UTC" or policy.get("hour", 0) != 0:
                        raise HTTPException(status_code=422, detail="OpenAI共有quotaは00:00 UTC日次reset固定です")
                    if quota.status not in {"active", "exhausted", "disabled"}:
                        raise HTTPException(status_code=422, detail="quota status が不正です")
                safe = quota.to_dict(
                    openai_usage_tier=config_get(
                        server.config, "openai.usage_tier", None
                    )
                )
            return {"success": True, "quota_pool": safe}
        finally:
            await session.close()

    @app.post("/api/free-team/openrouter/refresh")
    async def refresh_openrouter(_: None = Depends(require_auth)):
        try:
            payload = default_fetch_json(
                "https://openrouter.ai/api/v1/models", timeout=10.0
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OpenRouter Models API失敗: {exc}")
        items = [
            item
            for item in payload.get("data", [])
            if isinstance(item, dict) and _free_openrouter_model(item)
        ]
        session = await get_db_session()
        try:
            async with session.begin():
                await seed_free_team_defaults(session)
                existing_result = await session.execute(
                    select(FreeTeamCandidateModel).where(
                        FreeTeamCandidateModel.id.like("openrouter-dynamic-%")
                    )
                )
                existing = {item.id: item for item in existing_result.scalars().all()}
                active_ids: set[str] = set()
                for item in items:
                    model_id = str(item["id"])
                    if is_retired_model(model_id):
                        continue
                    proof = openrouter_free_catalog_evidence(item)
                    if proof is None:
                        continue
                    proof["catalogued_at"] = datetime.now(timezone.utc).isoformat()
                    candidate_id = _dynamic_candidate_id(model_id)
                    active_ids.add(candidate_id)
                    candidate = existing.get(candidate_id)
                    if candidate is None:
                        candidate = FreeTeamCandidateModel(
                            id=candidate_id,
                            credential_profile_id="openrouter-free",
                            provider="openrouter",
                            model=model_id,
                            priority=31,
                            enabled=True,
                            quota_pool_ids=["openrouter-free-rpm", "openrouter-free-rpd"],
                            capabilities=["text", "tools", "coding"],
                            quality_class="standard",
                            max_input_tokens=max(1, int(item.get("context_length") or 32000)),
                            max_output_tokens=2048,
                            provider_options={
                                "max_price": 0,
                                "allow_fallbacks": False,
                                "require_free": True,
                                "free_catalog_evidence": proof,
                            },
                            status="ready",
                        )
                        session.add(candidate)
                    else:
                        candidate.model = model_id
                        candidate.enabled = True
                        candidate.status = "ready"
                        candidate.max_input_tokens = max(
                            1, int(item.get("context_length") or 32000)
                        )
                        candidate.provider_options = {
                            **dict(candidate.provider_options or {}),
                            "max_price": 0,
                            "allow_fallbacks": False,
                            "require_free": True,
                            "free_catalog_evidence": proof,
                        }
                for candidate_id, candidate in existing.items():
                    if candidate_id not in active_ids:
                        candidate.enabled = False
                        candidate.status = "unavailable"
            return {"success": True, "count": len(items)}
        finally:
            await session.close()

    observation_store = provider_observer.ObservationStore()

    async def _openrouter_credential() -> tuple[str | None, str, str]:
        """(現在のbinding, キー, 送信不可理由)。キーは明示refreshの送信にだけ使い、応答へ出さない。"""

        session = await get_db_session()
        try:
            profile = await session.get(FreeTeamCredentialProfile, "openrouter-free")
            api_key = credential_api_key(profile, server.config) if profile else ""
            enabled = bool(profile and profile.enabled)
            base_url = profile.base_url if profile else ""
            revision = str(getattr(profile, "updated_at", None) or "") if profile else ""
            source = openrouter_credential_source(server.config)
        finally:
            await session.close()
        reason = observation_store.unusable_reason(enabled, api_key, base_url)
        binding = observation_store.binding(
            "openrouter-free", enabled, api_key, base_url, revision, source
        )
        return binding, api_key, reason

    async def _openrouter_local_quota() -> dict[str, Any] | None:
        state = await get_free_team_state(include_reservations=False, config=server.config)
        pool = next(
            (item for item in state["quota_pools"] if item["id"] == "openrouter-free-rpd"), None
        )
        if not pool:
            return None
        limit = pool["limit"]
        margin = max(pool["safety_margin_units"], limit * pool["safety_margin_ratio"])
        return {
            "configured_limit": limit,
            "safety_margin": margin,
            "effective_cap": pool.get("daily_cap", max(0, limit - margin)),
            "used": pool["consumed"],
            "reserved": pool["reserved"],
        }

    async def _openrouter_quota_view(
        refused_unusable: bool = False,
    ) -> OpenRouterQuotaResponse:
        """応答直前（最後のawaitの後）に現在のcredential bindingで観測を確定する。

        refresh中にcredential・有効状態・endpointが変わっていた場合、古い取得結果は
        破棄して現在bindingの状態（通常はunverified）を返す。Providerは再呼び出ししない。
        """

        local = await _openrouter_local_quota()
        current, _key, reason = await _openrouter_credential()
        # 以降にawaitを置かない。観測は常に現在bindingのstoreから同期的に選ぶ
        # （同bindingでも、取得中に確定した新しいrefreshを古い結果で上書きしない）。
        observation = observation_store.status(current)
        if refused_unusable and current is None and reason:
            # 送信不可のcredentialへ明示refreshした場合のみ。キャッシュはなく、理由だけ固定コードで返す。
            observation = {**observation, "status": "failed", "failure": reason}
        return OpenRouterQuotaResponse(local=local, provider_observed=observation)

    async def _openrouter_db_observation_current() -> bool:
        session = await get_db_session()
        try:
            profile = await session.get(FreeTeamCredentialProfile, "openrouter-free")
            if profile is None:
                return False
            quotas = {
                item.id: item
                for item in (
                    await session.execute(
                        select(FreeTeamQuotaPool).where(
                            FreeTeamQuotaPool.id.in_(
                                ["openrouter-free-rpm", "openrouter-free-rpd"]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            }
            return _openrouter_quota_observation_is_current(
                profile,
                quotas,
                datetime.now(timezone.utc),
                server.config,
            )
        finally:
            await session.close()

    async def _refresh_openrouter_task_quota(
        *, force: bool = False
    ) -> dict[str, Any]:
        if not force and await _openrouter_db_observation_current():
            return {"status": "observed", "failure": ""}
        binding, api_key, reason = await _openrouter_credential()
        observation = await asyncio.to_thread(
            lambda: observation_store.refresh(binding, api_key, reason=reason)
        )
        if observation.get("status") == "observed":
            if await _sync_openrouter_quota_observation(
                observation_store, binding, config=server.config
            ):
                return {"status": "observed", "failure": ""}
            return {"status": "failed", "failure": "sync_failed"}
        await _invalidate_openrouter_quota_observation(
            observation_store, binding, config=server.config
        )
        return {
            "status": str(observation.get("status") or "failed"),
            "failure": str(observation.get("failure") or "refresh_failed"),
        }

    server._free_team_openrouter_task_refresh = _refresh_openrouter_task_quota

    @app.get("/api/free-team/openrouter/quota", response_model=OpenRouterQuotaResponse)
    async def get_openrouter_quota(_: None = Depends(require_auth)):
        return await _openrouter_quota_view()

    @app.post("/api/free-team/openrouter/quota/refresh", response_model=OpenRouterQuotaResponse)
    async def refresh_openrouter_quota(_: None = Depends(require_auth)):
        result = await _refresh_openrouter_task_quota(force=True)
        return await _openrouter_quota_view(
            refused_unusable=result.get("failure")
            in {"missing_key", "unsupported_endpoint"}
        )

    @app.post("/api/free-team/usage/sync")
    async def sync_usage(request: Request, _: None = Depends(require_auth)):
        body = await request.json()
        observed = body.get("provider_observed_usage") if isinstance(body, dict) else None
        if not isinstance(observed, dict):
            raise HTTPException(
                status_code=422,
                detail="provider_observed_usage をobjectで指定してください",
            )
        session = await get_db_session()
        updated: list[str] = []
        try:
            async with session.begin():
                await seed_free_team_defaults(session)
                result = await session.execute(
                    select(FreeTeamQuotaPool)
                    .where(FreeTeamQuotaPool.id.in_(list(observed)))
                    .order_by(FreeTeamQuotaPool.id)
                    .with_for_update()
                )
                quotas = {item.id: item for item in result.scalars().all()}
                for quota_id, value in observed.items():
                    quota = quotas.get(quota_id)
                    if quota is None:
                        continue
                    try:
                        await sync_provider_observation(session, quota, value, config=server.config)
                    except FreeTeamUnavailableError as exc:
                        raise HTTPException(status_code=409 if str(exc) == "provider_observation_conflict" else 422, detail=str(exc)) from exc
                    updated.append(quota_id)
            return {"success": True, "updated": updated}
        finally:
            await session.close()

    @app.get("/api/free-team/ledger")
    async def get_ledger(_: None = Depends(require_auth)):
        state = await get_free_team_state()
        return {"reservations": state["reservations"], "quota_pools": state["quota_pools"]}
