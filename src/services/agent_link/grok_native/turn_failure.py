# MUSE_GROK_TYPED_FAILURE_PARSER_20261101_1544_6F2A
"""Pure classification of an already-dispatched typed turn-failure payload.

The caller owns dispatch from the typed SDK message; this helper only
inspects a Mapping payload. It changes no send/terminal/usage state, and
USAGE_LIMIT here is not asserted to be a weekly limit: it is classified
only as GROK_USAGE_LIMIT_EXCEEDED.
"""
from collections.abc import Mapping

_USAGE_LIMIT_INT = 3
_RATE_LIMIT_INT = 4


def parse_correlated_turn_failure(
    payload,
    *,
    expected_agent_id,
    expected_turn_id,
    expected_session_id=None,
):
    """Return a fixed availability/error code, or None if not correlated.

    The agent and turn must exactly equal the trusted expected
    strings, and `code` must be a non-bool int 3/4 or the exact strings
    'USAGE_LIMIT'/'RATE_LIMIT'. Anything else returns None. Unknown extra
    fields are ignored and never read into the result.
    """
    expected = {
        "agentId": expected_agent_id,
        "turnId": expected_turn_id,
    }
    if expected_session_id is not None:
        expected["sessionId"] = expected_session_id
    for value in expected.values():
        if not isinstance(value, str) or value == "":
            return None
    if not isinstance(payload, Mapping):
        return None
    try:
        for field, expected_value in expected.items():
            actual = payload.get(field)
            if not isinstance(actual, str) or actual != expected_value:
                return None
        code = payload.get("code")
    except Exception:
        # Getter errors on a broken/hostile Mapping classify as no match.
        # BaseException (KeyboardInterrupt/SystemExit) is not caught.
        return None

    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        if code == _USAGE_LIMIT_INT:
            return "GROK_USAGE_LIMIT_EXCEEDED"
        if code == _RATE_LIMIT_INT:
            return "GROK_RATE_LIMITED"
        return None
    if isinstance(code, str):
        if code == "USAGE_LIMIT":
            return "GROK_USAGE_LIMIT_EXCEEDED"
        if code == "RATE_LIMIT":
            return "GROK_RATE_LIMITED"
    return None
