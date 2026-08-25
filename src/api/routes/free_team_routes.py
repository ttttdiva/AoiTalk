"""無料Teamの設定・状態・同期API。秘密情報は返さない。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select

from ...memory.database import get_db_session
from ...memory.models.free_team import (
    FreeTeamCandidateModel,
    FreeTeamCredentialProfile,
    FreeTeamQuotaPool,
)
from ...services.free_team_service import (
    FREE_TEAM_PROFILE_ID,
    free_team_profile,
    get_free_team_state,
    recover_credential_after_auth_update,
    release_expired_reservations,
    seed_free_team_defaults,
)
from ...services.llm_model_catalog import default_fetch_json
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer


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
    if parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field} は0以上で指定してください")
    return parsed


def _free_openrouter_model(item: dict[str, Any]) -> bool:
    model_id = str(item.get("id") or "").strip()
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    def is_zero_price(key: str) -> bool:
        if key not in pricing:
            return False
        try:
            return Decimal(str(pricing[key])) == 0
        except Exception:
            return False

    return (
        model_id == "openrouter/free"
        or model_id.endswith(":free")
        or (is_zero_price("prompt") and is_zero_price("completion"))
    )


def _dynamic_candidate_id(model_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")[:90]
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
    return f"openrouter-dynamic-{slug}-{digest}"[:140]


_PROVIDER_OPTION_KEYS = {
    "allow_fallbacks",
    "capabilities",
    "concurrency_limit",
    "enable_tools",
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


def register_free_team_routes(app: FastAPI, server: "WebChatServer") -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def free_team_reaper_loop() -> None:
        while True:
            try:
                await release_expired_reservations()
            except Exception:
                # DB一時障害でも次回周期で回復させる。
                pass
            await asyncio.sleep(60)

    async def start_free_team_reaper() -> None:
        try:
            await release_expired_reservations()
        except Exception:
            pass
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
            include_quotas=False, include_reservations=False
        )
        return {
            "routing_profile_id": FREE_TEAM_PROFILE_ID,
            "profile": _safe_config_value(free_team_profile(server.config)),
            **state,
        }

    @app.get("/api/free-team/usage")
    async def get_usage(_: None = Depends(require_auth)):
        state = await get_free_team_state(include_reservations=False)
        return {"quota_pools": state["quota_pools"]}

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
            "llm_profiles",
        }
        for key in body:
            if key not in allowed:
                raise HTTPException(status_code=422, detail=f"更新できない設定です: {key}")
        next_profile = dict(current)
        next_profile.update(body)
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
        llm_profiles = next_profile.get("llm_profiles")
        if not isinstance(llm_profiles, dict):
            raise HTTPException(status_code=422, detail="llm_profiles はobjectで指定してください")
        for profile_id, profile in llm_profiles.items():
            if not isinstance(profile, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id} はobjectで指定してください",
                )
            unsupported = set(profile) - {
                "profile_id",
                "name",
                "target_type",
                "provider",
                "model",
                "effort_policy",
                "effort",
                "pool_id",
                "routing_profile_id",
            }
            if unsupported:
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id} に未対応の項目があります: {sorted(unsupported)[0]}",
                )
            target_type = str(profile.get("target_type") or "inherit").strip().lower()
            if target_type not in {"inherit", "static", "pool"}:
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id}.target_type が不正です",
                )
            effort_policy = str(profile.get("effort_policy") or "same").strip().lower()
            if effort_policy not in {"same", "lower", "explicit", "default"}:
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id}.effort_policy が不正です",
                )
            if target_type == "pool" and str(profile.get("pool_id") or "") not in next_profile["pools"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id}.pool_id がpoolsに存在しません",
                )
            if str(profile.get("routing_profile_id") or FREE_TEAM_PROFILE_ID) != FREE_TEAM_PROFILE_ID:
                raise HTTPException(
                    status_code=422,
                    detail=f"llm_profiles.{profile_id}.routing_profile_id はfree-team固定です",
                )
        next_profile["max_fallbacks"] = max(
            0, min(10, int(next_profile.get("max_fallbacks") or 0))
        )
        _save_config(
            server,
            f"routing_profiles.{FREE_TEAM_PROFILE_ID}",
            next_profile,
        )
        return {"success": True, "profile": _safe_config_value(next_profile)}

    @app.put("/api/free-team/credentials/{profile_id}")
    async def update_credential(
        profile_id: str, request: Request, _: None = Depends(require_auth)
    ):
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
                    candidate.provider_options = _validate_option_mapping(
                        body["provider_options"],
                        allowed=_PROVIDER_OPTION_KEYS,
                        field="provider_options",
                    )
                safe = candidate.to_dict()
            return {"success": True, "candidate": safe}
        finally:
            await session.close()

    @app.patch("/api/free-team/quota-pools/{quota_id}")
    async def update_quota(
        quota_id: str, request: Request, _: None = Depends(require_auth)
    ):
        body = await request.json()
        allowed = {
            "limit",
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
                if "reset_policy" in body:
                    if not isinstance(body["reset_policy"], dict):
                        raise HTTPException(status_code=422, detail="reset_policy はobjectです")
                    policy = body["reset_policy"]
                    if _contains_secret_key(policy):
                        raise HTTPException(
                            status_code=422,
                            detail="reset_policy に秘密情報を含めることはできません",
                        )
                    unsupported = set(policy) - {"kind", "timezone", "hour", "day"}
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
                    quota.reset_policy = policy
                if "status" in body:
                    quota.status = str(body["status"] or "active")
                safe = quota.to_dict()
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
                for candidate_id, candidate in existing.items():
                    if candidate_id not in active_ids:
                        candidate.enabled = False
                        candidate.status = "unavailable"
            return {"success": True, "count": len(items)}
        finally:
            await session.close()

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
                    .with_for_update()
                )
                quotas = {item.id: item for item in result.scalars().all()}
                for quota_id, value in observed.items():
                    quota = quotas.get(quota_id)
                    if quota is None:
                        continue
                    # 遅延した外部値でローカルのハードストップを解除しない。
                    quota.provider_observed_usage = max(
                        Decimal(str(quota.provider_observed_usage or 0)),
                        _non_negative(value, quota_id),
                    )
                    quota.last_provider_sync_at = datetime.utcnow()
                    updated.append(quota_id)
            return {"success": True, "updated": updated}
        finally:
            await session.close()

    @app.get("/api/free-team/ledger")
    async def get_ledger(_: None = Depends(require_auth)):
        state = await get_free_team_state()
        return {"reservations": state["reservations"], "quota_pools": state["quota_pools"]}
