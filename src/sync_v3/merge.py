"""Sync v3 のfield単位3-wayマージ（純関数）。値はログに出さない。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .contract import BASE_UNKNOWN_KEY

FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _is_unknown(value: Any) -> bool:
    return isinstance(value, dict) and value.get(BASE_UNKNOWN_KEY) is True


def _normalize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical(name: str, value: Any) -> str:
    if name.endswith("_ids") and isinstance(value, list):
        return json.dumps(sorted(str(item) for item in value), separators=(",", ":"))
    if isinstance(value, datetime):
        value = _normalize_datetime(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _equal(name: str, left: Any, right: Any) -> bool:
    return _canonical(name, left) == _canonical(name, right)


def validate_field_names(patch: dict[str, Any], base: dict[str, Any]) -> bool:
    return all(
        isinstance(key, str) and FIELD_NAME_RE.match(key)
        for key in (*patch.keys(), *base.keys())
    )


def merge_fields(
    current: dict[str, Any], patch: dict[str, Any], base: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """適用するfieldのdictと、競合したfield名のlistを返す。"""
    applied: dict[str, Any] = {}
    conflicts: list[str] = []
    for name, value in patch.items():
        has_current = name in current
        if has_current and _equal(name, current[name], value):
            continue
        base_value = base.get(name, {BASE_UNKNOWN_KEY: True})
        if not _is_unknown(base_value) and _equal(name, current.get(name), base_value):
            applied[name] = value
        else:
            conflicts.append(name)
    return applied, conflicts
