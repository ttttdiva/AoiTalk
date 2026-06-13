"""Shared task time defaults."""

from __future__ import annotations

from typing import Any


DEFAULT_TASK_TIMEZONE = "Asia/Tokyo"


def normalize_task_timezone(value: Any) -> str:
    timezone = str(value or "").strip()
    if not timezone or timezone.upper() == "UTC":
        return DEFAULT_TASK_TIMEZONE
    return timezone
