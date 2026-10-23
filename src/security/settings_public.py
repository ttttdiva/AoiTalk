"""Public representation helpers for settings that may contain secrets."""

from __future__ import annotations

import copy
import json
from typing import Any

from .field_crypto import _is_sensitive_key

_CONFIGURED_SUFFIX = "_configured"
_MUSE_REMOTE_TARGET_SETTING_KEY = "remote_targets.muse"
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
    "browser_agent.",
    "tts.yomi_linter.",
    "mage_vl.",
    "remote_targets.muse.",
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
    if _is_muse_remote_target_setting_key(normalized):
        return True
    leaf = normalized.rsplit(".", 1)[-1]
    return is_secret_field_name(leaf) or _is_sensitive_key(normalized)


def _is_muse_remote_target_setting_key(key: str) -> bool:
    normalized = str(key or "").strip()
    return normalized == _MUSE_REMOTE_TARGET_SETTING_KEY or normalized.startswith(
        _MUSE_REMOTE_TARGET_SETTING_KEY + "."
    )


def is_admin_only_setting_key(key: str) -> bool:
    normalized = str(key or "").strip()
    if not normalized:
        return False
    if normalized == _MUSE_REMOTE_TARGET_SETTING_KEY:
        return True
    if any(normalized.startswith(prefix) for prefix in _ADMIN_ONLY_SETTING_PREFIXES):
        return True
    if normalized.startswith("model_routing.classes.") and normalized.endswith(
        ".api_key"
    ):
        return True
    return normalized in {"mage_vl.api_key", "browser_agent"}


def _configured_flag_name(field_name: str) -> str:
    if field_name == "api_key":
        return "api_key_configured"
    return f"{field_name}{_CONFIGURED_SUFFIX}"


def mask_secret_dict(
    value: Any,
    *,
    is_admin: bool = False,
    _path: str = "",
) -> Any:
    """Return a copy with secret leaf values cleared and configured flags added."""

    if _is_muse_remote_target_setting_key(_path):
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            child_path = f"{_path}.{key}" if _path else key
            if _is_muse_remote_target_setting_key(child_path):
                result[key] = "[REDACTED]"
                continue
            if isinstance(raw_value, dict):
                result[key] = mask_secret_dict(
                    raw_value,
                    is_admin=is_admin,
                    _path=child_path,
                )
                continue
            if not is_secret_field_name(key):
                result[key] = raw_value
                continue
            configured = bool(str(raw_value or "").strip())
            result[key] = ""
            result[_configured_flag_name(key)] = configured
        return result
    if isinstance(value, list):
        return [
            mask_secret_dict(item, is_admin=is_admin, _path=_path)
            for item in value
        ]
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


def redact_nested_secrets(value: Any, *, _path: str = "") -> Any:
    if _is_muse_remote_target_setting_key(_path):
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            child_path = f"{_path}.{key}" if _path else key
            if _is_muse_remote_target_setting_key(child_path) or is_secret_field_name(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_nested_secrets(nested, _path=child_path)
        return result
    if isinstance(value, list):
        return [redact_nested_secrets(item, _path=_path) for item in value]
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
    if _is_muse_remote_target_setting_key(key):
        # Target documents are private server configuration.  A successful
        # persisted write reports presence only; validation details and values
        # never cross the public response boundary.
        payload["configured"] = bool(value)
    elif is_secret_setting_key(key):
        payload["configured"] = bool(str(value or "").strip())
    else:
        payload["value"] = value
    return payload
