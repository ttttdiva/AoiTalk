from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from openai import APIConnectionError, APIStatusError

from ..services.agent_route_fallback_policy import RoutePreflightFailure
from ..services.provider_failure_metadata import extract_provider_failure


@dataclass
class FrozenTransportAttempt:
    requests: int = 0


_current_attempt: ContextVar[FrozenTransportAttempt | None] = ContextVar(
    "frozen_candidate_transport_attempt", default=None
)


@contextmanager
def frozen_candidate_transport():
    """Restrict retries and classification to one frozen candidate attempt."""
    state = FrozenTransportAttempt()
    token = _current_attempt.set(state)
    try:
        yield state
    finally:
        _current_attempt.reset(token)


def current_frozen_transport() -> FrozenTransportAttempt | None:
    return _current_attempt.get()


def classify_frozen_transport_failure(
    error: BaseException, *, first_request: bool, proven_single_transport: bool
) -> RoutePreflightFailure | None:
    """Only SDK-proven rejection or a proven pre-send timeout is retry-safe."""
    if not first_request:
        return None
    if isinstance(error, APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        nested = body.get("error") if isinstance(body.get("error"), dict) else {}
        codes = (error.code, error.type, body.get("code"), body.get("type"),
                 nested.get("code"), nested.get("type"))
        if any(isinstance(code, str) and code in {
            "authentication_error", "permission_error", "content_policy_violation",
            "safety_violation", "model_refusal", "refusal",
        } for code in codes):
            return None
        if error.status_code == 429:
            metadata = extract_provider_failure(error) or {}
            if not metadata.get("quota_indicated"):
                return RoutePreflightFailure("rate_limited")
        if error.status_code == 503:
            return RoutePreflightFailure("provider_unavailable")
        if error.status_code not in (401, 403) and error.code == "model_not_found":
            return RoutePreflightFailure("model_unavailable")
    if isinstance(error, APIConnectionError) and proven_single_transport:
        cause = error.__cause__
        if isinstance(cause, httpx.ConnectTimeout):
            return RoutePreflightFailure("connect_timeout")
    return None
