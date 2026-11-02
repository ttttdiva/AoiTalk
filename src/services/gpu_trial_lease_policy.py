"""Startup policy for AoiTalk's bounded cooperative GPU trial leases.

This is a pure policy helper: it performs no I/O, does not read
``os.environ`` itself, starts no process, and does not alter runtime state.
The caller passes an environment mapping explicitly.

The ordinary maximum remains 180 seconds.  Only a runtime that has opted in
at startup as a trial runtime may select a larger instance maximum, and that
maximum can never exceed the absolute 900-second safety ceiling.  This helper
alone does not wire an API route, factory, coordinator, or operator setting.
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_MAX_LEASE_SECONDS = 180.0
HARD_MAX_LEASE_SECONDS = 900.0
ENV_MAX_LEASE_SECONDS = "AOITALK_GPU_TRIAL_MAX_LEASE_SECONDS"

_INVALID_TRIAL_ENABLED = "invalid_trial_enabled"
_INVALID_TRIAL_MAX = "invalid_trial_max_lease_seconds"
_INVALID_MAX = "invalid_max_lease_seconds"
_INVALID_TTL = "invalid_lease_ttl_seconds"

_MISSING = object()


def resolve_trial_max_lease_seconds(
    environ: Mapping[str, str],
    *,
    trial_enabled: bool,
) -> float:
    """Resolve the startup-selected maximum lease TTL in seconds.

    ``trial_enabled=False`` always selects the ordinary 180-second maximum,
    regardless of any stale environment override.  When trial mode is
    enabled, an absent or whitespace-only override also selects 180 seconds.
    A supplied override must be ASCII decimal digits only and must denote an
    integer in the inclusive range [180, 900].
    """
    if type(trial_enabled) is not bool:
        raise ValueError(_INVALID_TRIAL_ENABLED)

    if not trial_enabled:
        return DEFAULT_MAX_LEASE_SECONDS

    raw_value = environ.get(ENV_MAX_LEASE_SECONDS, _MISSING)
    if raw_value is _MISSING:
        return DEFAULT_MAX_LEASE_SECONDS
    if not isinstance(raw_value, str):
        raise ValueError(_INVALID_TRIAL_MAX)

    stripped = raw_value.strip()
    if not stripped:
        return DEFAULT_MAX_LEASE_SECONDS

    # ASCII digits only: no sign, fraction, exponent, NaN/Inf spelling,
    # Unicode decimal digits, or other numeric-looking characters.
    if not all("0" <= character <= "9" for character in stripped):
        raise ValueError(_INVALID_TRIAL_MAX)

    # Avoid converting arbitrarily long digit strings.  After removing
    # leading zeroes, any in-range value has at most three digits.
    significant_digits = stripped.lstrip("0")
    if not significant_digits or len(significant_digits) > 3:
        raise ValueError(_INVALID_TRIAL_MAX)

    parsed = int(significant_digits)
    if not 180 <= parsed <= 900:
        raise ValueError(_INVALID_TRIAL_MAX)

    return float(parsed)


def validate_lease_ttl_seconds(
    value: object,
    *,
    max_seconds: float = DEFAULT_MAX_LEASE_SECONDS,
) -> float:
    """Validate a requested lease TTL against an instance maximum.

    ``max_seconds`` must itself be a real, finite int/float in [180, 900].
    The requested value must be a real, finite int/float satisfying
    ``0 < value <= max_seconds``.  Fractional TTLs are preserved.  Strings
    are not coerced and out-of-range values are never clamped.
    """
    maximum = _validated_max_seconds(max_seconds)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_INVALID_TTL)

    # This comparison also rejects NaN (all comparisons are false) and
    # positive infinity, without first converting a huge int to float.
    if not 0 < value <= maximum:
        raise ValueError(_INVALID_TTL)

    return float(value)


def _validated_max_seconds(max_seconds: object) -> float:
    if isinstance(max_seconds, bool) or not isinstance(
        max_seconds, (int, float)
    ):
        raise ValueError(_INVALID_MAX)

    # NaN fails this range check; infinities fall outside it.  Comparing a
    # huge int directly also avoids an overflowing float conversion.
    if not DEFAULT_MAX_LEASE_SECONDS <= max_seconds <= HARD_MAX_LEASE_SECONDS:
        raise ValueError(_INVALID_MAX)

    return float(max_seconds)
