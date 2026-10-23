"""Async compatibility helper for trusted personal-service guards."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


async def run_personal_guard(guard: Callable[[], Any] | None) -> Any:
    """Run an optional guard and await it when it is asynchronous.

    Existing owner-scoped service callers use synchronous guards. Personal
    Agent controllers also need an asynchronous current-authority check after
    database awaits, so both forms share the same guard call sites.
    """

    if guard is None:
        return None
    result = guard()
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = ["run_personal_guard"]
