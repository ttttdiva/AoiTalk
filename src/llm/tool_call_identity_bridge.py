"""Bridge the sync tool loop to the durable AgentRun receipt identity check.

The OpenAI-compatible tool loop runs in a worker thread, while
``AgentRunService`` is async and must touch the database on the loop that owns
the run. The owner loop is carried in a ``ContextVar`` set only inside the
worker context (never on a shared client), and the check fails closed whenever
the bridge cannot be proven safe.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextvars import ContextVar, Token
from typing import Any, Callable

from .unified_turn_runtime import _observable_tool_arguments

logger = logging.getLogger(__name__)

IDENTITY_LOOKUP_TIMEOUT_SECONDS = 10.0

_OWNER_LOOP: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "tool_identity_owner_loop", default=None
)


def bind_tool_identity_owner_loop(loop: asyncio.AbstractEventLoop) -> Token:
    return _OWNER_LOOP.set(loop)


def reset_tool_identity_owner_loop(token: Token) -> None:
    _OWNER_LOOP.reset(token)


def build_durable_tool_call_identity_check(
    registry: Any,
    *,
    service: Any | None = None,
    timeout_seconds: float = IDENTITY_LOOKUP_TIMEOUT_SECONDS,
) -> Callable[[str, str, dict[str, Any]], str | None] | None:
    """Return a sync pre-dispatch check bound to the current Run, or ``None``.

    ``None`` means there is no durable Run in this context, so only the
    turn-local fence applies. With a Run, the returned callable yields a safe
    reason code when the call must not dispatch.
    """

    from ..services.agent_run_service import (
        AgentRunService,
        canonical_audit_digest,
        get_current_agent_run_id,
    )

    run_id = str(get_current_agent_run_id() or "").strip()
    if not run_id:
        return None
    owner_loop = _OWNER_LOOP.get()
    run_service = service

    def _on_owner_loop(make_coroutine: Callable[[Any], Any]) -> Any:
        """Run one service coroutine on the owner loop; raise on any failure."""

        nonlocal run_service
        loop = owner_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            raise _BridgeUnavailable("identity_bridge_unavailable")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Waiting on the owner loop from a running loop could deadlock.
            raise _BridgeUnavailable("identity_bridge_same_loop")
        try:
            if run_service is None:
                run_service = AgentRunService()
            future = asyncio.run_coroutine_threadsafe(make_coroutine(run_service), loop)
        except Exception as exc:  # noqa: BLE001 - never echo the cause
            logger.warning(
                "Durable tool identity call not started (error_type=%s)",
                type(exc).__name__,
            )
            raise _BridgeUnavailable("identity_lookup_failed") from None
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise _BridgeUnavailable("identity_lookup_timeout") from None
        except Exception as exc:  # noqa: BLE001 - never echo the cause
            logger.warning(
                "Durable tool identity call failed (error_type=%s)",
                type(exc).__name__,
            )
            raise _BridgeUnavailable("identity_lookup_failed") from None

    def _intent(tool_name: str, arguments: dict[str, Any]) -> tuple[dict, str]:
        # Receipts are recorded from the observable projection; the digest of
        # the restored raw intent travels separately.
        return (
            _observable_tool_arguments(registry, tool_name, arguments),
            canonical_audit_digest(arguments),
        )

    def _check(call_id: str, tool_name: str, arguments: dict[str, Any]) -> str | None:
        try:
            projected, digest = _intent(tool_name, arguments)
            return _on_owner_loop(
                lambda svc: svc.check_tool_call_identity_collision(
                    run_id,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    arguments=projected,
                    arguments_digest=digest,
                )
            )
        except _BridgeUnavailable as exc:
            return exc.reason

    def _claim(call_id: str, tool_name: str, arguments: dict[str, Any]) -> str | None:
        try:
            projected, digest = _intent(tool_name, arguments)
            return _on_owner_loop(
                lambda svc: svc.claim_tool_call(
                    run_id,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    arguments=projected,
                    arguments_digest=digest,
                )
            )
        except _BridgeUnavailable as exc:
            return exc.reason

    def _settle(
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executed: bool,
        success: bool,
        output: Any,
    ) -> bool:
        try:
            _projected, digest = _intent(tool_name, arguments)
            return bool(
                _on_owner_loop(
                    lambda svc: svc.settle_tool_call(
                        run_id,
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        executed=executed,
                        success=success,
                        output=output,
                        arguments_digest=digest,
                    )
                )
            )
        except _BridgeUnavailable:
            return False

    _check.claim = _claim  # type: ignore[attr-defined]
    _check.settle = _settle  # type: ignore[attr-defined]
    return _check


class _BridgeUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
