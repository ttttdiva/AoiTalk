"""Persistent, quota-only OpenAI provider circuit breaker.

Only explicit OpenAI quota/billing 429 codes trip the circuit. The stored
state contains bounded machine-readable metadata and never retains an error
message, response body, credential, or request payload.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

OPENAI_PROVIDER_CIRCUIT_CONFIG_KEY = "openai_provider_circuit"
DEFAULT_PROBE_INTERVAL_MINUTES = 15
MIN_PROBE_INTERVAL_MINUTES = 5
PROBE_TIMEOUT = timedelta(minutes=10)
OPENAI_QUOTA_429_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "insufficient_quota",
        "organization_quota_exceeded",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "quota_exceeded",
        "spend_limit_exceeded",
        "usage_limit_reached",
    }
)
OPENAI_TRANSIENT_429_CODES = frozenset(
    {"rate_limit_exceeded", "rate_limit_error", "requests", "tokens"}
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_STATE_LOCK = threading.RLock()
_CACHED_STATE: dict[str, Any] | None = None
_ACTIVE_PROBE_ID: str | None = None


def _utc_naive(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current
    return current.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return _utc_naive(parsed)


def _get_config_value(config: Any, dotted_key: str) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(dotted_key, None)
            if value is not None:
                return value
        except Exception:
            pass
    value = config
    for part in dotted_key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _set_config_value(config: Any, dotted_key: str, value: Any) -> None:
    setter = getattr(config, "set", None)
    if callable(setter):
        try:
            setter(dotted_key, dict(value) if isinstance(value, Mapping) else value)
            return
        except Exception:
            pass
    if not isinstance(config, dict):
        return
    parts = dotted_key.split(".")
    current = config
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = dict(value) if isinstance(value, Mapping) else value


def _safe_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SAFE_CODE.fullmatch(normalized) else None


def quota_429_evidence_from_fields(
    *, status_code: Any, code: Any = None, error_type: Any = None
) -> dict[str, Any] | None:
    """Return a safe proof projection for a known pre-processing quota 429."""
    if type(status_code) is not int or status_code != 429:
        return None
    normalized_code = _safe_error_code(code)
    normalized_type = _safe_error_code(error_type)
    # The SDK exposes the API's machine ``code`` and ``type`` separately.
    # Prefer an explicit code, but never let a transient rate-limit code be
    # overridden by a contradictory type field.
    if normalized_code in OPENAI_TRANSIENT_429_CODES:
        return None
    if normalized_code in OPENAI_QUOTA_429_CODES:
        reason = normalized_code
    elif normalized_type in OPENAI_TRANSIENT_429_CODES:
        return None
    elif normalized_type in OPENAI_QUOTA_429_CODES:
        reason = normalized_type
    else:
        return None
    return {
        "http_status": 429,
        "code": normalized_code,
        "type": normalized_type,
        "reason_code": reason,
        "provider_rejected_before_processing": True,
    }


def classify_openai_quota_429(error: BaseException) -> dict[str, Any] | None:
    """Classify only the SDK's explicit RateLimitError quota/billing codes."""
    try:
        from openai import RateLimitError
    except Exception:
        return None
    if not isinstance(error, RateLimitError):
        return None
    return quota_429_evidence_from_fields(
        status_code=getattr(error, "status_code", None),
        code=getattr(error, "code", None),
        error_type=getattr(error, "type", None),
    )


def next_provider_block_deadline(now: datetime | None = None) -> datetime:
    """Block until next UTC midnight, and never for less than one hour."""
    current = _utc_naive(now)
    next_midnight = datetime.combine(current.date() + timedelta(days=1), time.min)
    return max(next_midnight, current + timedelta(hours=1))


def _safe_state(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    blocked_until = _parse_datetime(raw.get("blocked_until"))
    reason = _safe_error_code(raw.get("reason_code"))
    if blocked_until is None or reason not in OPENAI_QUOTA_429_CODES:
        return None
    code = _safe_error_code(raw.get("error_code"))
    error_type = _safe_error_code(raw.get("error_type"))
    blocked_at = _parse_datetime(raw.get("blocked_at"))
    probe_interval = raw.get("probe_interval_minutes")
    if type(probe_interval) is not int or probe_interval < MIN_PROBE_INTERVAL_MINUTES:
        probe_interval = DEFAULT_PROBE_INTERVAL_MINUTES
    next_probe_at = _parse_datetime(raw.get("next_probe_at"))
    # Existing persisted circuit records predate scheduled probes. Preserve
    # their prior behavior by waiting until the old block deadline once.
    if next_probe_at is None:
        next_probe_at = blocked_until
    probe_started_at = _parse_datetime(raw.get("probe_started_at"))
    probe_id = raw.get("probe_id")
    if not isinstance(probe_id, str) or len(probe_id) > 64:
        probe_id = None
    last_probe_result = raw.get("last_probe_result")
    if last_probe_result not in {"quota_rejected", "error", "success", "timed_out", "not_sent"}:
        last_probe_result = None
    persisted = raw.get("persistence") == "app_config"
    return {
        "provider": "openai",
        "reason_code": reason,
        "error_code": code if code in OPENAI_QUOTA_429_CODES else None,
        "error_type": error_type if error_type in OPENAI_QUOTA_429_CODES else None,
        "blocked_at": _iso(blocked_at) if blocked_at else None,
        "blocked_until": _iso(blocked_until),
        "next_probe_at": _iso(next_probe_at),
        "probe_started_at": _iso(probe_started_at) if probe_started_at else None,
        "probe_id": probe_id,
        "last_probe_result": last_probe_result,
        "probe_interval_minutes": probe_interval,
        "persistence": "app_config" if persisted else "memory_only",
    }


def probe_interval_minutes(config: Any) -> int:
    """Read a bounded interval; malformed or absent settings fail closed to default."""
    raw = _get_config_value(
        config, "openai.provider_circuit.probe_interval_minutes"
    )
    if isinstance(raw, bool):
        return DEFAULT_PROBE_INTERVAL_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_PROBE_INTERVAL_MINUTES
    return max(MIN_PROBE_INTERVAL_MINUTES, value)


def _probe_is_stale(state: Mapping[str, Any], current: datetime) -> bool:
    started = _parse_datetime(state.get("probe_started_at"))
    return started is not None and current - started >= PROBE_TIMEOUT


def openai_provider_probe_available(
    config: Any, *, now: datetime | None = None
) -> bool:
    """Whether a Free Team request may claim the scheduled half-open probe."""
    current = _utc_naive(now)
    state = _effective_state(config)
    if state is None:
        return False
    blocked_until = _parse_datetime(state.get("blocked_until"))
    next_probe_at = _parse_datetime(state.get("next_probe_at"))
    if blocked_until is None or next_probe_at is None:
        return False
    if current < next_probe_at:
        return False
    with _STATE_LOCK:
        in_progress = _ACTIVE_PROBE_ID is not None
    return not in_progress or _probe_is_stale(state, current)


def _effective_state(config: Any) -> dict[str, Any] | None:
    configured = _safe_state(
        _get_config_value(config, OPENAI_PROVIDER_CIRCUIT_CONFIG_KEY)
    )
    with _STATE_LOCK:
        cached = _safe_state(_CACHED_STATE)
    if configured is None:
        return cached
    if cached is None:
        return configured
    configured_until = _parse_datetime(configured.get("blocked_until"))
    cached_until = _parse_datetime(cached.get("blocked_until"))
    if cached_until and configured_until and cached_until > configured_until:
        return cached
    return configured


def provider_circuit_status(
    config: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    current = _utc_naive(now)
    state = _effective_state(config)
    with _STATE_LOCK:
        half_open = _ACTIVE_PROBE_ID is not None
    if state is None:
        return {
            "provider_blocked": False,
            "provider_block_reason_code": None,
            "provider_blocked_until": None,
            "provider_half_open": False,
            "provider_block_persistence": None,
            "next_probe_at": None,
            "probe_interval_minutes": probe_interval_minutes(config),
        }
    blocked_until = _parse_datetime(state.get("blocked_until"))
    next_probe_at = _parse_datetime(state.get("next_probe_at"))
    probe_started_at = _parse_datetime(state.get("probe_started_at"))
    probe_in_progress = bool(
        half_open
        or (
            probe_started_at is not None
            and current - probe_started_at < PROBE_TIMEOUT
        )
    )
    post_deadline_wait = bool(
        blocked_until
        and next_probe_at
        and current >= blocked_until
        and current < next_probe_at
    )
    blocked = (
        bool(blocked_until and current < blocked_until)
        or probe_in_progress
        or post_deadline_wait
    )
    probe_due = bool(
        next_probe_at
        and current >= next_probe_at
        and (not probe_in_progress or _probe_is_stale(state, current))
    )
    return {
        "provider_blocked": blocked,
        "provider_block_reason_code": state.get("reason_code"),
        "provider_blocked_until": state.get("blocked_until"),
        "provider_half_open": probe_due or probe_in_progress,
        "provider_block_persistence": state.get("persistence"),
        "next_probe_at": state.get("next_probe_at"),
        "probe_interval_minutes": probe_interval_minutes(config),
    }


def openai_provider_is_blocked(
    config: Any, *, now: datetime | None = None
) -> bool:
    return bool(provider_circuit_status(config, now=now)["provider_blocked"])


def _persist_state(config: Any, state: dict[str, Any]) -> bool:
    try:
        from ..app_config_store import update_app_config_key_sync

        saved = bool(update_app_config_key_sync(OPENAI_PROVIDER_CIRCUIT_CONFIG_KEY, state))
    except Exception as exc:
        logger.warning(
            "OpenAI provider circuit persistence failed: exception_type=%s",
            type(exc).__name__,
        )
        saved = False
    if saved or state:
        _set_config_value(config, OPENAI_PROVIDER_CIRCUIT_CONFIG_KEY, state)
    return saved


def trip_openai_provider_circuit(
    config: Any,
    evidence: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist and publish one bounded quota/billing circuit trip."""
    safe = quota_429_evidence_from_fields(
        status_code=evidence.get("http_status"),
        code=evidence.get("code"),
        error_type=evidence.get("type"),
    )
    if safe is None:
        raise ValueError("openai_quota_429_evidence_required")
    current = _utc_naive(now)
    deadline = next_provider_block_deadline(current)
    existing = _effective_state(config)
    old_deadline = _parse_datetime(existing.get("blocked_until")) if existing else None
    if old_deadline and old_deadline > deadline:
        deadline = old_deadline
    state = {
        "schema_version": 1,
        "provider": "openai",
        "reason_code": safe["reason_code"],
        "error_code": safe["code"],
        "error_type": safe["type"],
        "blocked_at": _iso(current),
        "blocked_until": _iso(deadline),
        "next_probe_at": _iso(
            current + timedelta(minutes=probe_interval_minutes(config))
        ),
        "probe_started_at": None,
        "probe_id": None,
        "last_probe_result": "quota_rejected",
        "probe_interval_minutes": probe_interval_minutes(config),
        "persistence": "app_config",
    }
    saved = _persist_state(config, state)
    if not saved:
        state["persistence"] = "memory_only"
        _set_config_value(config, OPENAI_PROVIDER_CIRCUIT_CONFIG_KEY, state)
        logger.warning(
            "OpenAI provider circuit is memory-only until the configured persistence store recovers"
        )
    with _STATE_LOCK:
        global _CACHED_STATE
        _CACHED_STATE = dict(state)
    return _safe_state(state) or state


def admit_openai_provider_call(
    config: Any, *, now: datetime | None = None, allow_probe: bool = False
) -> dict[str, Any]:
    """Return a normal, blocked, or reserved complimentary half-open probe."""
    current = _utc_naive(now)
    state = _effective_state(config)
    if state is None:
        return {"blocked": False, "probe": False, "state": None}
    blocked_until = _parse_datetime(state.get("blocked_until"))
    if blocked_until is None:
        return {"blocked": True, "probe": False, "state": state}
    next_probe_at = _parse_datetime(state.get("next_probe_at"))
    if (
        not allow_probe
        or next_probe_at is None
        or current < next_probe_at
    ):
        return {"blocked": True, "probe": False, "state": state}
    global _ACTIVE_PROBE_ID, _CACHED_STATE
    with _STATE_LOCK:
        started = _parse_datetime(state.get("probe_started_at"))
        stale = _probe_is_stale(state, current)
        active_in_process = _ACTIVE_PROBE_ID is not None
        if active_in_process and not stale:
            return {"blocked": True, "probe": False, "state": state}
        if started is not None and current - started < PROBE_TIMEOUT:
            return {"blocked": True, "probe": False, "state": state}
        probe_id = uuid.uuid4().hex
        if stale:
            _ACTIVE_PROBE_ID = None
        _ACTIVE_PROBE_ID = probe_id
    claimed = {
        **state,
        "probe_started_at": _iso(current),
        "probe_id": probe_id,
        "last_probe_result": "timed_out" if stale else state.get("last_probe_result"),
    }
    saved = _persist_state(config, claimed)
    if not saved:
        claimed["persistence"] = "memory_only"
    with _STATE_LOCK:
        _CACHED_STATE = dict(claimed)
    return {
        "blocked": False,
        "probe": True,
        "probe_id": probe_id,
        "state": _safe_state(claimed) or claimed,
    }


def finish_openai_provider_probe(
    config: Any,
    *,
    clear: bool,
    result: str | None = None,
    now: datetime | None = None,
    probe_id: str | None = None,
) -> None:
    """Complete or relinquish the one half-open probe slot."""
    global _CACHED_STATE, _ACTIVE_PROBE_ID
    try:
        with _STATE_LOCK:
            active_probe_id = _ACTIVE_PROBE_ID
        if probe_id is not None and active_probe_id != probe_id:
            return
        if clear:
            clear_openai_provider_circuit(config)
        elif result is not None:
            state = _effective_state(config)
            if (
                state is not None
                and state.get("probe_started_at") is not None
                and (probe_id is None or state.get("probe_id") == probe_id)
            ):
                current = _utc_naive(now)
                interval = probe_interval_minutes(config)
                safe_result = (
                    result
                    if result in {"quota_rejected", "error", "success", "timed_out", "not_sent"}
                    else "error"
                )
                deferred = {
                    **state,
                    "next_probe_at": _iso(
                        current + timedelta(minutes=interval)
                    ),
                    "probe_started_at": None,
                    "last_probe_result": safe_result,
                    "probe_interval_minutes": interval,
                }
                saved = _persist_state(config, deferred)
                if not saved:
                    deferred["persistence"] = "memory_only"
                with _STATE_LOCK:
                    _CACHED_STATE = dict(deferred)
    finally:
        with _STATE_LOCK:
            if probe_id is None or _ACTIVE_PROBE_ID == probe_id:
                _ACTIVE_PROBE_ID = None


def clear_openai_provider_circuit(config: Any) -> bool:
    """Persist an administrator-approved early clear of the provider block."""
    global _CACHED_STATE, _ACTIVE_PROBE_ID
    if not _persist_state(config, {}):
        return False
    with _STATE_LOCK:
        _CACHED_STATE = None
        _ACTIVE_PROBE_ID = None
    return True


def reset_openai_provider_circuit_memory_for_tests() -> None:
    """Reset only process-local coordination state for isolated unit tests."""
    global _CACHED_STATE, _ACTIVE_PROBE_ID
    with _STATE_LOCK:
        _CACHED_STATE = None
        _ACTIVE_PROBE_ID = None
