"""A read-only CrowdWorks scan leases one selected helper target and run.

The extension remains on its existing path. Helper candidate IDs are data keys,
never browser references, and incomplete observations must not mean zero jobs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4


def require_crowdworks_url(url: Any) -> str:
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid = (
            parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname in {"crowdworks.jp", "www.crowdworks.jp"}
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("crowdworks_selected_origin_required")
    return url


def require_complete_inspection(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict) or set(observation) != {"origin", "candidates", "text", "coverage"}:
        raise RuntimeError("crowdworks_inspection_incomplete")
    require_crowdworks_url(observation.get("origin"))
    candidates = observation.get("candidates")
    coverage = observation.get("coverage")
    if (
        not isinstance(candidates, dict)
        or not isinstance(observation.get("text"), str)
        or not isinstance(coverage, dict)
        or coverage.get("complete") is not True
        or coverage.get("frame_scope") != "main"
        or coverage.get("shadow_scope") != "none"
        or coverage.get("truncated") is not False
        or coverage.get("unscanned") is not False
        or type(coverage.get("candidate_count")) is not int
        or coverage["candidate_count"] != len(candidates)
    ):
        raise RuntimeError("crowdworks_inspection_incomplete")
    for key, candidate in candidates.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"cw_[0-9]+", key) is None
            or not isinstance(candidate, dict)
            or set(candidate) != {"href", "label", "context"}
            or any(not isinstance(value, str) for value in candidate.values())
        ):
            raise RuntimeError("crowdworks_inspection_invalid")
    return observation


class BridgeInspectionLease:
    """Keep the connection's shared task lock until verified run cleanup ends."""

    def __init__(self, bridge, *, lock_timeout: float = 5, cleanup_timeout: float = 5,
                 request_timeout: float = 45):
        self.bridge = bridge
        self.browser_backend = getattr(bridge, "browser_backend", "extension")
        self.helper_backend = self.browser_backend == "agent_browser"
        self.run_id = uuid4().hex
        self.lock_timeout = lock_timeout
        self.cleanup_timeout = cleanup_timeout
        self.request_timeout = request_timeout
        self._lock = None

    async def __aenter__(self):
        if not self.helper_backend:
            return self
        lock = getattr(self.bridge, "operation_lock", None)
        if lock is None:
            raise RuntimeError("crowdworks_operation_lock_required")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lock_timeout
        # A worker-thread blocking acquire can leak ownership on cancellation.
        # Polling a nonblocking threading.Lock also works across event loops.
        while not lock.acquire(blocking=False):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError("crowdworks_browser_busy")
            await asyncio.sleep(min(0.05, remaining))
        self._lock = lock
        return self

    async def request(self, command: str, **params):
        if self.helper_backend:
            if self._lock is None:
                raise RuntimeError("crowdworks_inspection_lease_required")
            params["run_id"] = self.run_id
            pending = asyncio.create_task(self._bounded_request(command, params))
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError as cancellation:
                if pending.done() and pending.cancelled() and not asyncio.current_task().cancelling():
                    # Preserve a cancellation raised by the request itself.
                    return pending.result()
                # Drain only within the request deadline. Its timeout still
                # cancels the transport and closes WSS for an unknown outcome.
                # A settled command can instead safely precede end_run.
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not pending.cancelled():
                    pending.exception()  # Retrieve an error without replacing cancellation.
                raise cancellation
        else:
            result = await self.bridge.request(command, **params)
        if self.helper_backend and command == "crowdworks_inspect":
            return require_complete_inspection(result)
        return result

    async def _bounded_request(self, command, params):
        async with asyncio.timeout(self.request_timeout):
            return await self.bridge.request(command, **params)

    async def _release_run(self):
        async with asyncio.timeout(self.cleanup_timeout):
            result = await self.bridge.request("end_run", run_id=self.run_id)
        if not isinstance(result, dict) or any(
            result.get(key) is not True
            for key in ("ended", "refs_invalidated", "connection_retained")
        ):
            raise RuntimeError("crowdworks_run_release_unverified")

    async def __aexit__(self, exc_type, exc, traceback):
        if self._lock is None:
            return False
        cleanup = asyncio.create_task(self._release_run())
        cancellation = exc if isinstance(exc, asyncio.CancelledError) else None
        cleanup_error = None
        try:
            while True:
                try:
                    # Cancellation must not release the shared lock before
                    # cleanup finishes, nor cancel the end_run transport call.
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError as error:
                    if cleanup.done() and cleanup.cancelled():
                        cleanup_error = error
                        break
                    if cancellation is None:
                        cancellation = error
                except Exception as error:
                    cleanup_error = error
                    break
        finally:
            self._lock.release()
            self._lock = None
        original = cancellation if cancellation is not None else exc
        if cleanup_error is not None:
            if original is not None:
                original.add_note("crowdworks_run_release_unverified")
            else:
                raise RuntimeError("crowdworks_run_release_unverified") from cleanup_error
        if cancellation is not None and cancellation is not exc:
            raise cancellation
        return False
