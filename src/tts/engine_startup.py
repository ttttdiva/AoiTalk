"""Shared helpers for local TTS engine process startup readiness."""

from __future__ import annotations

import time
from typing import Callable

import requests

DEFAULT_ENGINE_STARTUP_TIMEOUT_SECONDS = 120.0
DEFAULT_ENGINE_STARTUP_POLL_SECONDS = 1.0


def wait_for_http_readiness(
    base_url: str,
    *,
    deadline: float,
    poll_seconds: float = DEFAULT_ENGINE_STARTUP_POLL_SECONDS,
    process_alive: Callable[[], bool] | None = None,
    version_path: str = "/version",
    request_timeout_seconds: float = 2.0,
) -> bool:
    """Poll an HTTP readiness endpoint until success or the absolute deadline."""

    poll_seconds = max(0.1, float(poll_seconds))
    while time.monotonic() < deadline:
        if process_alive is not None and not process_alive():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        timeout = min(request_timeout_seconds, remaining)
        if timeout <= 0:
            break
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}{version_path}",
                timeout=timeout,
            )
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_seconds, remaining))
    return False
