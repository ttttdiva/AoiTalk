"""Saved preflight fallback policy and trusted failure contract."""

SUPPORTED_FALLBACK_CODES = (
    "model_unavailable",
    "provider_unavailable",
    "rate_limited",
    "connect_timeout",
)


def normalize_fallback_on(value: object, *, missing: bool = False) -> tuple[str, ...] | None:
    if missing or value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("fallback_on must be a list")
    result: list[str] = []
    for code in value:
        if not isinstance(code, str) or code not in SUPPORTED_FALLBACK_CODES:
            raise ValueError("unsupported fallback_on code")
        if code in result:
            raise ValueError("duplicate fallback_on code")
        result.append(code)
    return tuple(result)


class RoutePreflightFailure(Exception):
    """A trusted boundary reports a known failure before a request starts."""

    def __init__(self, code: str, request_started: bool = False) -> None:
        if code not in SUPPORTED_FALLBACK_CODES:
            raise ValueError("unsupported preflight failure code")
        if not isinstance(request_started, bool):
            raise ValueError("request_started must be boolean")
        self.code = code
        self.request_started = request_started
        super().__init__(code)


def should_fallback_after_preflight_failure(
    policy: tuple[str, ...] | None, error: BaseException
) -> bool:
    return (
        isinstance(error, RoutePreflightFailure)
        and not error.request_started
        and (policy is None or error.code in policy)
    )
