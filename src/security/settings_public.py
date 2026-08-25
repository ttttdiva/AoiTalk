"""Public representation helpers for settings that may contain secrets."""

from __future__ import annotations

import copy
import json
from typing import Any

from .field_crypto import _is_sensitive_key

_CONFIGURED_SUFFIX = "_configured"
_SECRET_LEAF_NAMES = frozenset(
    {
        "api_key",
        "server_command",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
    }
)

_ADMIN_ONLY_SETTING_PREFIXES = (
    "tts.yomi_linter.",
    "mage_vl.",
)


def is_secret_field_name(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    return normalized in _SECRET_LEAF_NAMES or _is_sensitive_key(normalized)


def is_secret_setting_key(key: str) -> bool:
    normalized = str(key or "").strip()
    if not normalized:
        return False
    leaf = normalized.rsplit(".", 1)[-1]
    return is_secret_field_name(leaf) or _is_sensitive_key(normalized)


def is_admin_only_setting_key(key: str) -> bool:
    normalized = str(key or "").strip()
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in _ADMIN_ONLY_SETTING_PREFIXES):
        return True
    if normalized.startswith("model_routing.classes.") and normalized.endswith(
        ".api_key"
    ):
        return True
    return normalized == "mage_vl.api_key"


def _configured_flag_name(field_name: str) -> str:
    if field_name == "api_key":
        return "api_key_configured"
    return f"{field_name}{_CONFIGURED_SUFFIX}"


def mask_secret_dict(value: Any, *, is_admin: bool = False) -> Any:
    """Return a copy with secret leaf values cleared and configured flags added."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if isinstance(raw_value, dict):
                result[key] = mask_secret_dict(raw_value, is_admin=is_admin)
                continue
            if not is_secret_field_name(key):
                result[key] = raw_value
                continue
            configured = bool(str(raw_value or "").strip())
            result[key] = ""
            result[_configured_flag_name(key)] = configured
        return result
    if isinstance(value, list):
        return [mask_secret_dict(item, is_admin=is_admin) for item in value]
    return copy.deepcopy(value)


def mask_model_routing_classes(classes: Any, *, is_admin: bool = False) -> Any:
    if not isinstance(classes, dict):
        return classes
    return {
        str(class_name): mask_secret_dict(class_config, is_admin=is_admin)
        if isinstance(class_config, dict)
        else class_config
        for class_name, class_config in classes.items()
    }


def redact_nested_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if is_secret_field_name(str(key))
                else redact_nested_secrets(nested)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [redact_nested_secrets(item) for item in value]
    return value


def format_setting_log_value(key: str, value: Any) -> str:
    if is_secret_setting_key(key):
        if value in (None, ""):
            return "<empty>"
        return "<redacted>"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(
                redact_nested_secrets(value),
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            return "<unserializable>"
    return repr(value)


def public_setting_patch_payload(
    key: str,
    value: Any,
    *,
    persisted: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "key": key,
        "persisted": persisted,
    }
    if is_secret_setting_key(key):
        payload["configured"] = bool(str(value or "").strip())
    else:
        payload["value"] = value
    return payload
