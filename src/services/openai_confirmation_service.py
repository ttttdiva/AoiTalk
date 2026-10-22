"""Owner declarations bound to existing credentials, in the existing config row.

This is local evidence management, never provider attestation or enrollment.
"""
from __future__ import annotations

import copy
import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..app_config_store import GLOBAL_CONFIG_KEY
from ..memory.models import AppConfigSetting
from ..memory.models.free_team import FreeTeamCredentialProfile
from ..security.field_crypto import decrypt_json_secret_leaves, encrypt_json_secret_leaves

SCHEMA = "owner_confirmation_v1"
REFERENCES = ("openai_api_key", "free-team:openai-complimentary")


class ConfirmationError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def managed(config: Any) -> bool:
    from .free_team_service import config_get
    receipt = config_get(config, "openai.complimentary_verification", {})
    return isinstance(receipt, dict) and receipt.get("schema") == SCHEMA


async def config_row(session, *, lock: bool = False):
    query = select(AppConfigSetting).where(AppConfigSetting.key == GLOBAL_CONFIG_KEY)
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        raise ConfirmationError("confirmation_store_not_ready", 503)
    stored = decrypt_json_secret_leaves(copy.deepcopy(row.value), aad_prefix="app_config_settings.value")
    current = copy.deepcopy(stored)
    # Same environment precedence as Config and the canonical worker snapshot.
    from ..config import Config
    Config.__new__(Config)._merge_env_variables(current)
    return row, stored, current


async def credential_identity(session, config: dict, reference: str, *, lock: bool = False) -> dict:
    from .free_team_service import config_get, _credential_api_key
    if reference not in REFERENCES:
        raise ConfirmationError("credential_reference_unsupported", 422)
    if reference == "openai_api_key":
        key = config_get(config, reference, "") or os.getenv("OPENAI_API_KEY", "")
        endpoint = config_get(config, "openai.base_url", "") or config_get(config, "openai_base_url", "") or os.getenv("OPENAI_BASE_URL", "")
    else:
        query = select(FreeTeamCredentialProfile).where(FreeTeamCredentialProfile.id == "openai-complimentary")
        if lock:
            query = query.with_for_update()
        profile = (await session.execute(query)).scalar_one_or_none()
        key = _credential_api_key(profile) if profile is not None else ""
        endpoint = profile.base_url if profile is not None else ""
    return {"credential_reference": reference,
        "api_key_sha256": hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) and key else "",
        "sdk_project_id": os.getenv("OPENAI_PROJECT_ID", ""),
        "sdk_organization_id": os.getenv("OPENAI_ORG_ID", ""),
        "base_url": str(endpoint or "https://api.openai.com/v1").rstrip("/")}


def require_owner(receipt: dict, actor_id: str) -> None:
    owner = receipt.get("owner_user_id")
    if owner and owner != actor_id:
        raise ConfirmationError("confirmation_owner_mismatch", 403)


async def binding_stop_reason(session, config: dict) -> str:
    from .free_team_service import config_get
    receipt = config_get(config, "openai.complimentary_verification", {})
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        return ""
    if receipt.get("revoked_at"):
        return "confirmation_revoked"
    bindings = receipt.get("credentials", [])
    if not isinstance(bindings, list) or len(bindings) != 1:
        return "credential_project_unverified"
    binding = bindings[0]
    if not isinstance(binding, dict):
        return "credential_project_unverified"
    if binding.get("credential_reference") not in REFERENCES:
        return "credential_project_unverified"
    current = await credential_identity(session, config, binding["credential_reference"])
    if not current["api_key_sha256"] or any(binding.get(key) != value for key, value in current.items()):
        return "confirmation_credential_changed"
    return ""


async def authoritative_config(session) -> dict:
    _, _, current = await config_row(session, lock=True)
    reason = await binding_stop_reason(session, current)
    if reason:
        raise ConfirmationError(reason)
    return current


async def context(session, actor_id: str) -> dict:
    from .free_team_service import config_get, complimentary_stop_reason
    _, _, current = await config_row(session)
    receipt = config_get(current, "openai.complimentary_verification", {})
    if not isinstance(receipt, dict):
        receipt = {}
    require_owner(receipt, actor_id)
    credentials = []
    for reference in REFERENCES:
        identity = await credential_identity(session, current, reference)
        credentials.append({"reference": reference, "configured": bool(identity["api_key_sha256"]),
            "fingerprint": identity["api_key_sha256"], "project_id": identity["sdk_project_id"],
            "sdk_organization_id": identity["sdk_organization_id"], "base_url": identity["base_url"]})
    reason = await binding_stop_reason(session, current) or complimentary_stop_reason(current)
    # Only the owning human-admin route receives declaration/actor metadata.
    public = {key: receipt.get(key) for key in ("record_id", "owner_user_id", "actor_user_id", "recorded_at",
        "source_kind", "source_reference", "eligible", "enrolled", "organization_id", "usage_tier", "revoked_at")}
    public["credential_reference"] = next((binding.get("credential_reference") for binding in receipt.get("credentials", [])
        if isinstance(binding, dict)), None)
    return {"credentials": credentials, "record": public, "stop_reason": reason,
        "billing_scope_id": config_get(current, "openai.billing_scope_id", ""),
        "usage_tier": config_get(current, "openai.usage_tier", "tier_1_2"),
        "provider_attestation": False}


async def save(session, actor_id: str, payload: dict) -> dict:
    from .free_team_service import config_get
    row, stored, current = await config_row(session, lock=True)
    previous = config_get(current, "openai.complimentary_verification", {})
    if not isinstance(previous, dict):
        previous = {}
    require_owner(previous, actor_id)
    if previous.get("record_id") != payload.get("expected_record_id"):
        raise ConfirmationError("confirmation_record_changed")
    identity = await credential_identity(session, current, payload["credential_reference"], lock=True)
    if not identity["api_key_sha256"]:
        raise ConfirmationError("credential_not_configured", 422)
    if identity["api_key_sha256"] != payload["fingerprint"]:
        raise ConfirmationError("confirmation_credential_changed")
    scope = payload["billing_scope_id"]
    if identity["sdk_organization_id"] not in {"", scope}:
        raise ConfirmationError("billing_scope_mismatch", 422)
    if identity["base_url"] != "https://api.openai.com/v1":
        raise ConfirmationError("complimentary_endpoint_unverified", 422)
    source_reference = payload["source_reference"]
    if "sk-" in source_reference or "Bearer " in source_reference:
        raise ConfirmationError("confirmation_source_contains_secret", 422)
    now = datetime.now(timezone.utc).isoformat()
    receipt = {"schema": SCHEMA, "record_id": str(uuid.uuid4()), "owner_user_id": actor_id,
        "actor_user_id": actor_id, "recorded_at": now, "source_kind": "owner_declaration",
        "source_reference": source_reference, "eligible": payload["eligible"], "enrolled": payload["enrolled"],
        "organization_id": scope, "usage_tier": payload["usage_tier"],
        "credentials": [{**identity, "organization_id": scope, "project_enrolled": payload["enrolled"]}]}
    openai = copy.deepcopy(stored.get("openai") or {})
    openai.update(complimentary_verification=receipt, billing_scope_id=scope, usage_tier=payload["usage_tier"],
        data_sharing_incentive_enabled=payload["eligible"] and payload["enrolled"])
    stored["openai"] = openai
    row.value = encrypt_json_secret_leaves(stored, aad_prefix="app_config_settings.value")
    return openai


async def revoke(session, actor_id: str, expected_record_id: str | None) -> dict:
    row, stored, _ = await config_row(session, lock=True)
    receipt = (stored.get("openai") or {}).get("complimentary_verification") or {}
    require_owner(receipt, actor_id)
    if receipt.get("record_id") != expected_record_id:
        raise ConfirmationError("confirmation_record_changed")
    if not managed(stored):
        raise ConfirmationError("confirmation_record_missing")
    revoke_record(stored, reason="owner_revoked")
    row.value = encrypt_json_secret_leaves(stored, aad_prefix="app_config_settings.value")
    return stored["openai"]


def invalidate_config_patch(before: dict, after: dict) -> bool:
    """Invalidate on a binding change under the existing global config lock."""
    from .free_team_service import config_get
    paths = ("openai_api_key", "openai.base_url", "openai_base_url", "openai.billing_scope_id",
             "openai.usage_tier", "openai.data_sharing_incentive_enabled")
    if any(config_get(before, key) != config_get(after, key) for key in paths):
        return revoke_record(after, reason="credential_or_scope_changed")
    return False


def revoke_record(config: dict, *, reason: str) -> bool:
    receipt = (config.get("openai") or {}).get("complimentary_verification")
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA or receipt.get("revoked_at"):
        return False
    receipt["revoked_at"] = datetime.now(timezone.utc).isoformat()
    receipt["revocation_reason"] = reason
    return True
